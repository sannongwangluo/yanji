# -*- coding: utf-8 -*-
"""报到映射：从识别分句里匹配「我是X / 我叫X」，建立 说话人标签→姓名 映射。

纯函数、不依赖任何外部服务，方便单测。规则：
- 按分句出现顺序扫描，每个说话人标签取第一个命中「我是X/我叫X」的分句建映射；
- 未报到的标签显示为「说话人N」（N 取标签里的数字部分；实测（2026-09-02）流式
  definite 分句的 speaker_id 是 0 基字符串，单人音频为 "0"）。
"""
import re

_CHECKIN_RE = re.compile(r"我(?:是|叫)\s*([一-龥A-Za-z]{2,8})")
_TAG_DIGITS_RE = re.compile(r"\d+")


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
            m = _CHECKIN_RE.search(text)
            if m:
                mapping[sp] = m.group(1)
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
            m = _CHECKIN_RE.search(text)
            if m:
                self.mapping[sp] = m.group(1)
        item = dict(utterance)
        item["speaker_name"] = self.mapping.get(sp, _display_name(sp))
        return item, self.mapping
