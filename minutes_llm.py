# -*- coding: utf-8 -*-
"""DeepSeek 生成会议纪要。

- 端点 https://api.deepseek.com/v1/chat/completions，模型 deepseek-v4-flash（config.toml 可改）；
- 故意不禁用 thinking（不传 thinking=disabled）：纪要是要质量的活，一次调用慢点没关系；
- 网络层 urllib + 空 ProxyHandler 强制直连：避免系统代理残留导致连接失败（代理内核停了但
  系统代理还开着时，读系统代理的库会 WinError 10061），api.deepseek.com 国内可直连；
- 自动重试：空内容 / 网络错误 / HTTP 5xx 视为瞬态，最多重试 2 次（间隔 3 秒）；
  401/403（key 无效/没额度）不重试，直接抛中文错误。
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


class _TransientError(RuntimeError):
    """DeepSeek 瞬态失败（空内容/网络错误/HTTP 5xx）：值得自动重试。"""


class _AuthError(RuntimeError):
    """DeepSeek 鉴权失败（401/403，key 无效或没额度）：重试无意义，直接抛给用户。"""

SYSTEM_PROMPT = (
    "你是一名专业会议纪要整理助手。输入是会议语音转写（已标注发言人姓名），"
    "输出一份正式、简洁、可直接分发的会议纪要 Markdown。"
    "不编造转写里没有的信息；保留关键数字、时间、人名；拿不准的按转写原样。"
)

USER_TEMPLATE = """下面是某次会议的语音转写（每行开头是发言人姓名）。请整理成会议纪要，直接输出 Markdown，结构固定如下：

# 会议主题
## 参会人
## 发言实录摘要
## 决议事项
## 待办
## 分歧与遗留问题

要求：
- 会议主题：一句话概括这次会议是为什么开的。
- 参会人：只列报到或发言中出现过的姓名，用顿号分隔。
- 发言实录摘要：按人归纳各自的观点与承诺，别写成流水账。
- 决议事项：逐条列出会上定下来的事。
- 待办：逐条列出，每条写清责任人和期限；会上没提期限的，期限写「未定」。
- 分歧与遗留问题：会上没谈拢的、留到下次再议的。
- 全文只用标题、列表和正文，不要使用表格。

转写全文：

{transcript}"""

MAX_TRANSCRIPT_CHARS = 30000  # 超长时按发言人轮次从头截断（报到都在头部，必须保留）


def build_transcript(renamed_utterances):
    """带姓名的转写文本，一行一句：「张三：……」。"""
    return "\n".join(f"{u['speaker_name']}：{u['text']}" for u in renamed_utterances)


def truncate_utterances(utterances, max_chars=MAX_TRANSCRIPT_CHARS):
    """按发言人轮次从头截断。返回 (保留的分句列表, 被截掉的分句数)。"""
    total = 0
    kept = []
    for u in utterances:
        size = len(u["speaker_name"]) + len(u["text"]) + 2
        if total + size > max_chars and kept:
            break
        kept.append(u)
        total += size
    return kept, len(utterances) - len(kept)


def generate_minutes(cfg, utterances):
    """utterances（已带 speaker_name）→ 纪要 Markdown。缺 key/调用失败给中文错误。

    调用失败自动重试：空内容 / 网络错误 / HTTP 5xx 最多共 3 次尝试（间隔 3 秒），
    401/403 不重试；重试耗尽后仍抛中文 RuntimeError。
    """
    ds = cfg["deepseek"]
    if not ds["api_key"]:
        raise ConfigError(
            "还没有配置 DeepSeek 的 API Key。\n"
            "请把 Key 填到 config.toml 的 [deepseek] api_key 一项"
            "（或设置环境变量 DEEPSEEK_API_KEY），然后重新点「结束并生成纪要」。")

    kept, dropped = truncate_utterances(utterances)
    transcript = build_transcript(kept)
    if dropped:
        transcript += f"\n\n（说明：后续还有 {dropped} 句发言因长度限制被省略）"
    log.info("[纪要] 转写 %d 句、%d 字，交给 DeepSeek…", len(kept), len(transcript))

    payload = {
        "model": ds["model"],
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": USER_TEMPLATE.format(transcript=transcript)},
        ],
        "temperature": 0.3,
        "max_tokens": 4096,
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
                log.warning("[纪要] 第 %d/%d 次调用失败（%s），%.0f 秒后重试…",
                            attempt, total_attempts, e, RETRY_DELAY)
                time.sleep(RETRY_DELAY)
            else:
                raise RuntimeError(
                    f"{e}（已自动重试 {RETRY_TIMES} 次仍失败）") from e
    # 循环内必然 return 或 raise，走到这里只是防御性兜底
    raise RuntimeError("调用 DeepSeek 失败。")


def _post_chat(ds, payload):
    """单次调用 DeepSeek chat completions，成功返回纪要 Markdown。

    失败抛中文 RuntimeError（子类 _TransientError / _AuthError 供重试层分类）：
    - _AuthError：401/403，key 无效或没额度，不重试；
    - _TransientError：空内容、网络错误（URLError/超时等）、HTTP 5xx，可重试；
    - RuntimeError：其他 HTTP 状态码 / 返回内容无法解析，不重试。
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
        content = (data["choices"][0]["message"]["content"] or "").strip()
    except (KeyError, IndexError, ValueError) as e:
        raise RuntimeError(f"DeepSeek 返回内容无法解析：{e}") from e
    if not content:
        raise _TransientError("DeepSeek 返回了空内容。")
    log.info("[纪要] 生成耗时 %.1f 秒，%d 字", time.monotonic() - t0, len(content))
    return content
