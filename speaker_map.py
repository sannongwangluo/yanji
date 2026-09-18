# -*- coding: utf-8 -*-
"""报到映射：从识别分句里匹配「我是X / 我叫X」，建立 说话人标签→姓名 映射。

纯函数、不依赖任何外部服务，方便单测。规则（定版，改前先读项目 AGENTS.md）：
- 按分句出现顺序扫描，每个说话人标签取第一个命中「我是X/我叫X」的分句建映射；
- 未报到的标签显示为「说话人N」（N 取标签里的数字部分；实测（2026-09-02）流式
  definite 分句的 speaker_id 是 0 基字符串，单人音频为 "0"）。
"""
import re

# 报到模板：我是X / 我叫X。候选收紧到 2~4 字（人名一般就这么长；原来的 2~8 会把
# 「我是同意的」整句当姓名），并且候选后面必须紧跟标点/空白/句尾——「我是张三大家
# 好」这种连读不算报到。
_CHECKIN_RE = re.compile(
    r"我(?:是|叫)\s*([一-龥A-Za-z]{2,4})"
    r"(?=[\s，。！？、；：,.!?;:）)】\]｝}\"'”’]|$)")
_TAG_DIGITS_RE = re.compile(r"\d+")

# 姓名首字排除表：常见动词/副词/连接词，真人名不会这么起头
# （「我是同意的」→ 同意的、「我是负责这一块的」→ 负责、「我是想…」→ 想…）。
_CHECKIN_NAME_STOP_CHARS = frozenset("说同负觉想要在做看来去不也就还")


def _checkin_name(text):
    """从一句转写里提取报到的姓名；不像姓名返回 None。

    _map_speakers 与 IncrementalSpeakerMap 共用这一份（单一来源），
    实时出字与散会终稿的口径必须完全一致。
    """
    m = _CHECKIN_RE.search(text or "")
    if not m:
        return None
    name = m.group(1)
    if name[0] in _CHECKIN_NAME_STOP_CHARS:
        return None
    return name


def _display_name(speaker):
    """标签 → 显示名。报到的用姓名，未报到的用「说话人N」。"""
    tag = str(speaker) if speaker is not None else ""
    m = _TAG_DIGITS_RE.search(tag)
    n = m.group() if m else (tag or "?")
    return f"说话人{n}"


def _map_speakers(utterances):
    """utterances: [{speaker, text, ...}, ...] → (mapping, renamed_utterances)

    mapping: {标签字符串: 姓名}；
    renamed_utterances: 原字段不变，另加 speaker_name（姓名或「说话人N」）。
    """
    mapping = {}
    renamed = []
    for u in utterances:
        sp = str(u.get("speaker") if u.get("speaker") is not None else "0")
        text = u.get("text") or ""
        if sp not in mapping:
            name = _checkin_name(text)
            if name:
                mapping[sp] = name
        item = dict(u)
        item["speaker_name"] = mapping.get(sp, _display_name(sp))
        renamed.append(item)
    return mapping, renamed


class IncrementalSpeakerMap:
    """流式增量报到映射：每来一句新 utterance 调 update(u)，适配实时出字场景。

    与 _map_speakers 同一套规则（我是X/我叫X、首现即定、同标签后出现不覆盖），
    只是把状态挂在实例上逐句推进。散会出稿前管线仍会用全量 _map_speakers
    对收集好的分句重算一遍（单一来源，保证终稿口径一致）。
    """

    def __init__(self):
        self.mapping = {}

    def update(self, utterance):
        """单句 utterance → (renamed_utterance, mapping)。

        renamed_utterance 原字段不变、另加 speaker_name（姓名或「说话人N」）。
        """
        sp = str(utterance.get("speaker")
                 if utterance.get("speaker") is not None else "0")
        text = utterance.get("text") or ""
        if sp not in self.mapping:
            name = _checkin_name(text)
            if name:
                self.mapping[sp] = name
        item = dict(utterance)
        item["speaker_name"] = self.mapping.get(sp, _display_name(sp))
        return item, self.mapping
