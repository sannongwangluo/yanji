# -*- coding: utf-8 -*-
"""DeepSeek 生成会议纪要（含长会议的滚动摘要）。

- 端点 https://api.deepseek.com/v1/chat/completions，模型 deepseek-flash
  （DeepSeek V4.1 Flash 的正式 ID，config.toml 可改）；
- 纪要模板 = 钉钉「智能纪要」（通义妙记）导出结构 1:1 复刻（2026-09-17 翻案，上一版
  「飞书通用风」作废）：`# 智能纪要：<主题> <日期>` + **录音主题/录音时间/参会人员**
  三行头部（样例里的卡片简化成正文）+ 「总结」（层级大纲）/「后续工作计划」/「待办」/
  「智能章节」（`mm:ss` 或 `hh:mm:ss` + 章节摘要）/「关键决策」（关键决策→问题→讨论方案→
  决策依据 + 其他决策）/「金句时刻」；`build_transcript` 每句加 `[mm:ss]` 前缀
  （start_time 相对会议开始，无 start_time 的句子不加），章节时间戳只能取自这里；
- 热词（hotwords）有值时在 user message 里加一段白名单，让模型按正确写法纠正
  同音/近音错写；没有热词就不出现这段话；
- 滚动摘要（generate_digest）：长会议中途把「上一版摘要 + 新增转写」压成紧凑中文
  摘要，散会时 generate_minutes(prior_digest=…) 把它当前半场，只发摘要之后的新转写，
  出稿更快也不丢内容；摘要失败绝不影响出稿；
- 故意不禁用 thinking（不传 thinking=disabled）：纪要是要质量的活，一次调用慢点没关系；
  代价是思考内容也占输出额度，所以 max_tokens 给足 65536——2026-09-17 实测
  4096 会被思考吃光（finish_reason=length、content 为空），纪要一个字都出不来；
  2026-09-17 晚再翻案 32768→65536：输入变长思考也变长，5 小时级会议（转写约 12
  万字）+ 信息图 JSON 同走这条通道，32768 余量偏紧；上限调大不用不花钱（按实际
  用量计费），V4.1 Flash 服务端输出上限 384K，65536 仍很保守；
- 转写上限 MAX_TRANSCRIPT_CHARS = 20 万字：V4.1 Flash 上下文 100 万 token，
  3 小时会议转写约 7 万字，20 万字余量充足（超长按发言人轮次从头截断，一轮 =
  连续同一个发言人的若干句，行长含行首 `[mm:ss] ` 前缀）；
- 网络层 urllib + 空 ProxyHandler 强制直连：本机有系统代理残留踩坑史（代理内核停了但系统
  代理还开着时，读系统代理的库会 WinError 10061），api.deepseek.com 国内可直连；
- 自动重试：空内容 / 网络错误 / HTTP 5xx 视为瞬态，最多重试 2 次（间隔 3 秒）；
  401/403（key 无效/没额度）不重试，直接抛中文错误；输出被 max_tokens 截断
  （finish_reason=length 且 content 为空）是确定性失败，同样不重试，直接报中文错误。
"""
import json
import logging
import time
import urllib.error
import urllib.request

from config_loader import ConfigError

log = logging.getLogger("会议记录")

RETRY_TIMES = 2      # 失败后最多再重试 2 次（共 3 次尝试）
RETRY_DELAY = 3.0    # 重试间隔（秒）

MAX_TOKENS = 65536   # 输出上限：thinking 也占这个额度，给足（4096 实测会被思考吃光；
                     # 32768 对 5 小时级长会偏紧；服务端上限 384K，不用不花钱）

# finish_reason=length 且 content 为空：思考把输出额度占满，模型一个字都没来得及写。
# 这是确定性失败，重试只会再截断一次，所以直接抛给用户。
_TRUNCATED_MSG = (
    "DeepSeek 输出达到 max_tokens 上限被截断（思考内容占满了输出额度），"
    "没生成出任何纪要正文。\n"
    "这不是网络问题，重试无效；请把 minutes_llm.py 里的 MAX_TOKENS 调大，"
    "或改 config.toml 的 [deepseek] model 换一个不带思考的模型。")


class _TransientError(RuntimeError):
    """DeepSeek 瞬态失败（空内容/网络错误/HTTP 5xx）：值得自动重试。"""


class _AuthError(RuntimeError):
    """DeepSeek 鉴权失败（401/403，key 无效或没额度）：重试无意义，直接抛给用户。"""

SYSTEM_PROMPT = (
    "你是一名专业会议纪要整理助手，输出风格对齐钉钉「智能纪要」（通义妙记）的导出格式。"
    "输入是会议语音转写（每行开头是 [mm:ss] 时间戳和发言人姓名）。"
    "严格按用户消息里给的栏目结构输出 Markdown，不增删栏目。"
    "不编造转写里没有的信息；保留关键数字、时间、人名；拿不准的按转写原样。"
)

# 智能纪要模板（钉钉「智能纪要」（通义妙记）导出结构 1:1 复刻，2026-09-17 翻案；
# 上一版「飞书通用风」作废。栏目顺序固定，docx 里头部卡片简化为三行正文）
USER_TEMPLATE = """下面是某次会议的语音转写，每行开头是 [mm:ss]（该句相对会议开始的时间）和发言人姓名。请整理成「智能纪要」，直接输出 Markdown，栏目顺序固定如下：

# 智能纪要：<一句话主题> {meeting_date}

> **录音主题**：<一句话主题，与标题里的一致>
> **录音时间**：{meeting_time}
> **参会人员**：<只列报到或发言中出现过的姓名，用顿号分隔>

## 总结
第一段先用「音频围绕……展开研讨，明确……，内容如下：」的句式总述这场会在谈什么、谈出什么结论（3~5 句）。
然后按层级列大纲，议题用三级标题、议题下的小标题加粗、要点用列表：
### <一级议题>
**<小标题>**
- <具体要点，保留数字、人名、结论>

## 后续工作计划
分方面写，每条都要带期限（会上没提期限的写「未定」）：
**<方面小标题>**
- <计划内容>（期限：<期限>）

## 待办
逐条列出行动项，每条写成「名称：具体描述」：
- <名称>：<具体描述>

## 智能章节
按话题把整场会议切成若干章。每章一个三级标题，紧跟一段 `> ` 引用块写本章摘要（引用块会渲染成一张卡片）：
### <mm:ss> <章节标题>
> <本章 2~4 句摘要>
- 时间戳必须取自转写行首 [mm:ss] 里的值，写该章第一条发言的时间，**不许自己编**；
- 超过 1 小时的会议时间戳写成 hh:mm:ss（如 01:13:23）；时间戳必须从小到大递增；
- 章节数量按会议长度自然决定：20 分钟以内的短会 3~5 章即可，长会按话题多切几章。

## 关键决策
先写 1~3 个主决策，每个决策按四行写（「讨论方案」下逐条写明是谁提出的）：
**关键决策**：<一句话>
**问题**：<要解决什么问题>
**讨论方案**：
- <谁>提出<方案>
**决策依据**：<为什么这么定>
再收尾「其他决策：」，逐条列出其余定下来的事：
**其他决策**：
- <逐条>

## 金句时刻
0~3 句最值得留住的原话，原话加引号、下一行接一句点评；没有就写「本场无」：
「<原话>」
—— <一句点评>

要求：
- **录音时间**这一行原样照抄上面给出的值；**参会人员**只写转写里出现过的姓名。
- 智能章节的时间戳只能来自转写行首的 [mm:ss]，拿不到时间戳的句子不要编时间。
- 不编造转写里没有的信息；关键数字、时间、人名按转写保留；拿不准的按转写原样。
- 总结按层级归纳，别写成流水账；待办、后续工作计划、关键决策各归各位，不要互相串。
- 格式只用标题（#/##/###）、加粗（**…**）、列表（- ）和正文；**要对比多个对象的数据
  （不同渠道/不同产品的价格、销量、费比等）时，用 Markdown 表格呈现**（表头行、
  `| --- |` 分隔行、数据行），其余内容不要用表格；引用块（`> `）只用在上面指定的
  头部信息和每章摘要两处，别处不要用。
{digest_block}{hotwords_block}转写全文（每行开头是该句相对会议开始的时间戳）：

{transcript}"""

# 滚动摘要（长会议中途预写，散会合并进纪要）用的 prompt
DIGEST_SYSTEM_PROMPT = (
    "你是一名会议记录员，负责把长会议的转写滚动压缩成紧凑的中文摘要，"
    "供最后写正式纪要时使用。只压缩、不扩写，不编造；拿不准的按转写原样。"
)

DIGEST_TEMPLATE = """{prior_block}下面是这次会议新一段的语音转写，每行开头是 [mm:ss]（该句相对会议开始的时间）和发言人姓名。请把它们并进摘要，输出**更新后的完整摘要**：

必须保留：
- 关键发言的**时间点标记**（把 [mm:ss] 原样抄在对应内容前面，如「[12:30] 定了三人团报名少报」）——散会后要用这些时间点切分「智能章节」，丢了就对不上；
- 已经明确的决策、关键数字（金额/数量/比例/时间）、人名与公司/产品名、待办与责任人、还没谈拢的分歧。
不要写成纪要格式（不要标题、不要分节），就用紧凑的中文短句或「- 」列表，总长控制在 1000 字以内。
只压缩、不扩写，不编造转写里没有的信息。

新转写：

{transcript}"""

DIGEST_PRIOR_BLOCK = "已有摘要（本次会议前半场，可能为空）：\n{digest}\n\n"

# 散会出稿时：把滚动摘要作为「前半场」喂给纪要模板
DIGEST_MERGE_BLOCK = (
    "补充说明：这场会议很长，前半场已经压缩成下面的摘要（内容同样来自本次会议转写，"
    "可信、不要丢）：\n\n{digest}\n\n"
    "下面的「转写全文」是这份摘要之后的新发言。请把摘要和转写合起来，"
    "输出一份**覆盖整场会议**的完整纪要——摘要里的决议、数字、人名、待办都要体现。\n\n")

HOTWORDS_BLOCK = (
    "补充说明：以下专有名词是正确写法，转写里如出现同音/近音错写，请按此纠正："
    "{words}\n\n")

MAX_TRANSCRIPT_CHARS = 200000  # 超长时按发言人轮次从头截断（报到都在头部，必须保留）

_WEEKDAYS = "一二三四五六日"


def _time_stamp(ms):
    """毫秒偏移 → 「mm:ss」（超过 1 小时用 hh:mm:ss）。"""
    total = int(max(0, ms) // 1000)
    hours, rem = divmod(total, 3600)
    minutes, seconds = divmod(rem, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


def meeting_base_ms(utterances, base_ms=None):
    """会议开始的时间零点（第一句有 start_time 的毫秒值）；没有就返回 None。"""
    if base_ms is not None:
        return base_ms
    for u in utterances:
        start = u.get("start_time")
        if start is not None:
            return start
    return None


def format_meeting_time(meeting_time):
    """会议时间 → 钉钉样例口径「2026年9月16日（周三） 15:46」。

    只认 `%Y-%m-%d %H:%M` / `%Y/%m/%d %H:%M`（GUI 和 CLI 都产出这个形状），
    认不出的（人工 --meeting-time 自由文本）原样返回；空值写「未记录」。
    """
    text = (meeting_time or "").strip()
    if not text:
        return "未记录"
    for fmt in ("%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M"):
        try:
            parsed = time.strptime(text, fmt)
        except ValueError:
            continue
        return (f"{parsed.tm_year}年{parsed.tm_mon}月{parsed.tm_mday}日"
                f"（周{_WEEKDAYS[parsed.tm_wday]}） "
                f"{parsed.tm_hour:02d}:{parsed.tm_min:02d}")
    return text


def meeting_date(meeting_time):
    """标题里的日期「2026年9月16日」；拿不到返回空串（标题就不写日期）。"""
    text = (meeting_time or "").strip()
    for fmt in ("%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M"):
        try:
            parsed = time.strptime(text, fmt)
        except ValueError:
            continue
        return f"{parsed.tm_year}年{parsed.tm_mon}月{parsed.tm_mday}日"
    return ""


def _hotwords_block(hotwords):
    """热词白名单段落（无热词返回空串，prompt 里就不出现这段话）。"""
    words = [str(w).strip() for w in (hotwords or []) if str(w).strip()]
    if not words:
        return ""
    return HOTWORDS_BLOCK.format(words="、".join(words))


def _digest_block(digest):
    """滚动摘要段落（没有摘要返回空串 = 走原来的整稿路径）。"""
    text = (digest or "").strip()
    if not text:
        return ""
    return DIGEST_MERGE_BLOCK.format(digest=text)


def _line_prefix(start_time, base):
    """转写一行的行首时间戳前缀（无 start_time / 无零点时不加）；成文与截断共用。"""
    if start_time is None or base is None:
        return ""
    return f"[{_time_stamp(start_time - base)}] "


def _transcript_line(u, base):
    """转写里的一句话：「[mm:ss] 张三：……」——build_transcript 每行的唯一来源。"""
    return f"{_line_prefix(u.get('start_time'), base)}{u['speaker_name']}：{u['text']}"


def _line_size(u, base):
    """该句在转写里占的长度（含行首时间戳前缀 + 行尾换行）；截断口径用它。

    前缀长度按 build_transcript 实际加的那个算（`[mm:ss] ` 约 7~10 字符/句），
    与成文共用同一个 helper，免得截断口径和真正发出去的字数漂移。
    """
    return len(_transcript_line(u, base)) + 1


def build_transcript(renamed_utterances, base_ms=None):
    """带时间戳和姓名的转写文本，一行一句：「[mm:ss] 张三：……」。
    （行长/截断口径共用 _transcript_line，见 truncate_utterances）

    - 时间戳 = 该句 start_time 相对**会议开始**的偏移（base_ms 或第一句有
      start_time 的值）；超过 1 小时写成 hh:mm:ss——「智能章节」的时间戳就取自这里；
    - 没有 start_time 字段（或整批都没有时间）的句子不加前缀，只写「张三：……」
      （文件识别路径的 parse_utterances 也带 start_time，只不过可能是 None）。
    """
    base = meeting_base_ms(renamed_utterances, base_ms)
    return "\n".join(_transcript_line(u, base) for u in renamed_utterances)


def truncate_utterances(utterances, max_chars=MAX_TRANSCRIPT_CHARS, base_ms=None):
    """按发言人轮次从头截断。返回 (保留的分句列表, 被截掉的分句数)。

    - 一轮 = 连续同一个 speaker_name 的若干句；**整轮**放不下才停，不在谁的一轮
      发言中间截断（原来遇第一条超限句就 break，可能停在半轮话中间）；
    - 头部保留（报到都在头部）；单轮自身就超过上限时（整场只有一个人说话这类
      极端情况）保住这轮的开头，绝不返回空列表；
    - 行长口径 = build_transcript 实际写出去的长度（含行首 `[mm:ss] ` 前缀和
      行尾换行），所以要按同一零点算前缀，base_ms 与 build_transcript 同一含义；
    - 阈值 20 万字（2026-09-17 翻案，原 3 万）：V4.1 Flash 上下文 100 万 token，
      3 小时会议转写约 7 万字，20 万字余量充足。
    """
    base = meeting_base_ms(utterances, base_ms)
    n = len(utterances)
    kept = 0
    total = 0
    while kept < n:
        speaker = utterances[kept].get("speaker_name")
        end = kept
        while end < n and utterances[end].get("speaker_name") == speaker:
            end += 1
        turn_size = sum(_line_size(u, base) for u in utterances[kept:end])
        if kept and total + turn_size > max_chars:
            break  # 这一轮放不下：整轮不要，不切在人家话中间
        if turn_size > max_chars:
            # 单轮自身就超限（走到这里 kept 必为 0）：保住这轮开头到接近上限，
            # 至少留一句，别返回空
            for idx, u in enumerate(utterances[kept:end]):
                size = _line_size(u, base)
                if idx and total + size > max_chars:
                    break
                total += size
                kept += 1
            break
        kept = end
        total += turn_size
    return utterances[:kept], n - kept


def _require_key(cfg):
    ds = cfg["deepseek"]
    if not ds["api_key"]:
        raise ConfigError(
            "还没有配置 DeepSeek 的 API Key。\n"
            "请把 Key 填到 config.toml 的 [deepseek] api_key 一项"
            "（或设置环境变量 DEEPSEEK_API_KEY），然后重新点「结束并生成纪要」。")
    return ds


def _chat(ds, messages, what, temperature=0.3, max_tokens=MAX_TOKENS):
    """统一的 DeepSeek 调用通道（纪要 / 滚动摘要共用）：重试 + 失败分类。

    空内容 / 网络错误 / HTTP 5xx 视为瞬态，最多共 3 次尝试（间隔 3 秒）；
    401/403 与「被 max_tokens 截断」是确定性失败，不重试，直接抛中文错误。
    """
    payload = {
        "model": ds["model"],
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        # 注意：故意不传 thinking=disabled——纪要是要质量的活，让模型慢慢想
    }
    total_attempts = RETRY_TIMES + 1
    for attempt in range(1, total_attempts + 1):
        try:
            return _post_chat(ds, payload)
        except _AuthError:
            raise  # 401/403（key 无效/没额度）：重试无意义，直接抛中文错误
        except _TransientError as e:
            if attempt < total_attempts:
                log.warning("[%s] 第 %d/%d 次调用失败（%s），%.0f 秒后重试…",
                            what, attempt, total_attempts, e, RETRY_DELAY)
                time.sleep(RETRY_DELAY)
            else:
                raise RuntimeError(
                    f"{e}（已自动重试 {RETRY_TIMES} 次仍失败）") from e
    # 循环内必然 return 或 raise，走到这里只是防御性兜底
    raise RuntimeError("调用 DeepSeek 失败。")


def generate_minutes(cfg, utterances, hotwords=None, meeting_time=None,
                     prior_digest=None, base_start_ms=None):
    """utterances（已带 speaker_name）→ 智能纪要 Markdown。缺 key/调用失败给中文错误。

    - hotwords：专有名词白名单，有值时在 user message 里加一段「按此纠正同音错写」；
    - meeting_time：开会时间（GUI 记点「开始录音」时刻 / CLI 从文件名解析），
      取不到写「未记录」；
    - prior_digest：长会议的滚动摘要。有值时 utterances 视为「摘要之后的新发言」，
      prompt 里把摘要当作前半场一起交给模型，最终纪要仍覆盖整场会议；
    - base_start_ms：会议开始的时间零点（毫秒）——走摘要路径时只传后半段转写，
      必须由调用方把整场会议的第一句时间传进来，否则「智能章节」的时间戳会从
      后半段的开头重新数（章节时间戳就错了）。

    调用失败自动重试：空内容 / 网络错误 / HTTP 5xx 最多共 3 次尝试（间隔 3 秒），
    401/403 不重试；重试耗尽后仍抛中文 RuntimeError。输出被 max_tokens 截断
    （finish_reason=length 且 content 为空）是确定性失败，不重试，直接抛。
    """
    ds = _require_key(cfg)

    kept, dropped = truncate_utterances(utterances, base_ms=base_start_ms)
    transcript = build_transcript(kept, base_ms=base_start_ms)
    if dropped:
        transcript += f"\n\n（说明：后续还有 {dropped} 句发言因长度限制被省略）"
    digest_block = _digest_block(prior_digest)
    log.info("[纪要] %s转写 %d 句、%d 字，交给 DeepSeek…",
             "（带滚动摘要，只发摘要后的新转写）" if digest_block else "",
             len(kept), len(transcript))

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": USER_TEMPLATE.format(
            meeting_date=meeting_date(meeting_time),
            meeting_time=format_meeting_time(meeting_time),
            digest_block=digest_block,
            hotwords_block=_hotwords_block(hotwords),
            transcript=transcript)},
    ]
    return _chat(ds, messages, "纪要")


def build_digest_messages(prior_digest, utterances, base_start_ms=None):
    """滚动摘要的 messages（与纪要同一套转写/截断/时间戳口径）。"""
    kept, dropped = truncate_utterances(utterances, base_ms=base_start_ms)
    transcript = build_transcript(kept, base_ms=base_start_ms)
    if dropped:
        transcript += f"\n\n（说明：后续还有 {dropped} 句发言因长度限制被省略）"
    prior = (prior_digest or "").strip()
    return [
        {"role": "system", "content": DIGEST_SYSTEM_PROMPT},
        {"role": "user", "content": DIGEST_TEMPLATE.format(
            prior_block=DIGEST_PRIOR_BLOCK.format(digest=prior) if prior else "",
            transcript=transcript)},
    ]


def generate_digest(cfg, prior_digest, utterances, base_start_ms=None):
    """滚动摘要：上一版摘要 + 新增转写 → 更新后的摘要（紧凑中文，不是纪要格式）。

    摘要里必须带上关键发言的 [mm:ss] 时间点（否则散会走摘要路径时，
    「智能章节」早期章节的时间戳就没了）。失败原样抛中文异常，由调用方（GUI
    后台线程）吞掉记日志——滚动摘要绝不能影响出稿主路径。
    """
    ds = _require_key(cfg)
    kept, dropped = truncate_utterances(utterances, base_ms=base_start_ms)
    log.info("[摘要] 上一版 %d 字 + 新转写 %d 句（省略 %d 句），交给 DeepSeek…",
             len((prior_digest or "").strip()), len(kept), dropped)
    return _chat(ds, build_digest_messages(prior_digest, utterances,
                                          base_start_ms=base_start_ms), "摘要")


def _post_chat(ds, payload):
    """单次调用 DeepSeek chat completions，成功返回纪要 Markdown。

    失败抛中文 RuntimeError（子类 _TransientError / _AuthError 供重试层分类）：
    - _AuthError：401/403，key 无效或没额度，不重试；
    - _TransientError：空内容、网络错误（URLError/超时等）、HTTP 5xx，可重试；
    - RuntimeError：其他 HTTP 状态码 / 返回内容无法解析 / 输出被 max_tokens 截断
      （finish_reason=length 且 content 为空，确定性失败），都不重试。
    """
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(
        ds["endpoint"],
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {ds['api_key']}",
        },
        method="POST",
    )
    t0 = time.monotonic()
    try:
        with opener.open(req, timeout=300) as resp:
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise _AuthError(
                "DeepSeek API Key 无效或没额度了。\n"
                "请检查 config.toml 的 [deepseek] api_key（或环境变量 DEEPSEEK_API_KEY）。") from e
        if e.code >= 500:
            raise _TransientError(f"调用 DeepSeek 失败（HTTP {e.code}）。") from e
        raise RuntimeError(f"调用 DeepSeek 失败（HTTP {e.code}）。") from e
    except Exception as e:
        raise _TransientError(f"调用 DeepSeek 网络失败：{e}") from e

    try:
        data = json.loads(body)
        choice = data["choices"][0]
        content = (choice["message"]["content"] or "").strip()
        finish_reason = choice.get("finish_reason")
    except (KeyError, IndexError, ValueError) as e:
        raise RuntimeError(f"DeepSeek 返回内容无法解析：{e}") from e
    if not content:
        if finish_reason == "length":
            # 思考占满了 max_tokens 额度：同样的请求只会同样截断，重试没意义
            log.error("[纪要] finish_reason=length 且 content 为空，判定为输出被截断")
            raise RuntimeError(_TRUNCATED_MSG)
        raise _TransientError("DeepSeek 返回了空内容。")
    log.info("[纪要] 生成耗时 %.1f 秒，%d 字", time.monotonic() - t0, len(content))
    return content
