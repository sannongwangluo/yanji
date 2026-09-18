# -*- coding: utf-8 -*-
"""热词表：hotwords.txt 读写 + 服务端 token 上限估算。

热词按官方文档走流式识别 `request.corpus.context` 的「热词直传」形态
（见 `streaming_asr.corpus_context`），专治人名/产品名/行话的同音误识别。

- 文件：BASE_DIR/hotwords.txt，UTF-8、一行一词、忽略空行和 `#` 注释；
  文件不存在时用几个示例词创建（帮用户看懂格式），保存走临时文件 + os.replace 原子写；
- 上限：官方额度是双向流式 100 tokens，但额度算的是整份 corpus.context 的 JSON
  包装（每个词外面还套着 `{"word": ...}`），所以实用上限收紧到 60，留足包装开销；
- 估算公式（保守上界，宁多不少）：中文字数×1.5 + 英文/数字词数×1.5 + 其他非空白字符×1.0；
- 两道防线：保存入口（GUI）超限拒存并提示；注入入口（建连）超限截断 + warning，
  绝不让服务端拒掉整次识别（那是「录了一小时全是空的」级别的故障）。
"""
import logging
import os
import re
import threading
import uuid

from config_loader import app_base_dir

log = logging.getLogger("会议记录")

BASE_DIR = app_base_dir()
HOTWORDS_PATH = os.path.join(BASE_DIR, "hotwords.txt")

# 首次运行写入的示例词（只作演示，用户自行替换）
DEFAULT_HOTWORDS = ("项目代号", "产品名称", "负责人", "周会", "复盘")

# 服务端 corpus.context 热词额度：官方双向流式 100 tokens，扣掉 JSON 包装开销后实用 60
HOTWORDS_MAX_TOKENS = 60

_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_ALNUM_RUN_RE = re.compile(r"[A-Za-z0-9]+")
_NON_WORD_RE = re.compile(r"[\u4e00-\u9fffA-Za-z0-9\s]")

_LOCK = threading.RLock()


def parse_hotwords(text):
    """热词文本（一行一词）→ 词列表；忽略空行和 # 注释。"""
    words = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        words.append(line)
    return words


def estimate_tokens(text):
    """粗估一段文本的 token 数（服务端上限校验用，故意高估）。

    中文字数 × 1.5 + 英文/数字词数 × 1.5 + 其他非空白字符数 × 1.0。
    宁可提前拦住用户，也不让服务端拒绝整次识别请求。
    """
    cjk = len(_CJK_RE.findall(text))
    en_words = len(_ALNUM_RUN_RE.findall(text))
    others = len(_NON_WORD_RE.sub("", text))
    return cjk * 1.5 + en_words * 1.5 + others


def hotwords_tokens(words):
    """整份热词表的 token 粗估（按一行一词拼起来算，与落盘/注入形式一致）。"""
    return estimate_tokens("\n".join(words))


def limit_message(words):
    """超限时给用户看的中文提示（GUI 保存用）。"""
    return ("热词太多，超出服务器上限，请删减后再保存。\n\n"
            f"当前 {len(words)} 个词、约 {hotwords_tokens(words):.0f} tokens，"
            f"服务器上限约 {HOTWORDS_MAX_TOKENS} tokens"
            f"（大约三四十个汉字，够放十来个短词）。\n"
            "挑最容易被听错的人名、产品名、行话放进来就够了。")


def _read_text(path):
    """读热词文件文本；不存在返回 None，编码不是 UTF-8 按空表处理并告警。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return None
    except UnicodeDecodeError:
        log.warning("[热词] %s 不是 UTF-8 编码，本次按空表处理；请用 UTF-8 保存",
                    os.path.basename(path))
        return ""
    except OSError as e:
        log.warning("[热词] 读 %s 失败：%s", os.path.basename(path), e)
        return ""


def save_hotwords(words, path=None):
    """原子写 hotwords.txt（临时文件 + os.replace）。返回是否写成功。

    超出服务端 token 上限直接拒写（返回 False），保证磁盘上的热词永远是服务端
    能接受的版本。
    """
    path = path or HOTWORDS_PATH
    with _LOCK:
        tokens = hotwords_tokens(words)
        if tokens > HOTWORDS_MAX_TOKENS:
            log.warning("[热词] 拒绝写入：%d 个词约 %.0f tokens，超出上限 %d",
                        len(words), tokens, HOTWORDS_MAX_TOKENS)
            return False
        tmp = f"{path}.{uuid.uuid4().hex}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8", newline="\n") as f:
                f.write("\n".join(words) + ("\n" if words else ""))
            os.replace(tmp, path)
        except OSError as e:
            log.warning("[热词] 写 %s 失败：%s", os.path.basename(path), e)
            return False
        finally:
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass
        return True


def load_hotwords(path=None):
    """读热词表；文件不存在用示例词初始化创建（创建失败也不影响启动）。"""
    path = path or HOTWORDS_PATH
    with _LOCK:
        text = _read_text(path)
        if text is None:
            save_hotwords(list(DEFAULT_HOTWORDS), path)
            return list(DEFAULT_HOTWORDS)
        return parse_hotwords(text)


def clamp_hotwords(words, max_tokens=HOTWORDS_MAX_TOKENS):
    """注入前的防御：超限就按顺序截断到上限内（返回可能变短的列表）。

    保存入口已经拦过一次，这里是第二道保险——任何路径都不让服务端收到超限的
    corpus.context（超限会被拒或整次识别为空）。连第一个词都超限时返回空表：
    宁可这轮不带热词（识别照常），也不发一份超限的语料把整场识别搞成空的。
    """
    kept = []
    for w in words:
        if hotwords_tokens(kept + [w]) > max_tokens:
            break
        kept.append(w)
    return kept
