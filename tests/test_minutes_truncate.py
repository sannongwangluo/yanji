# -*- coding: utf-8 -*-
"""minutes_llm 转写截断阈值与请求参数单测（2026-09-17 翻案后的定版）。

覆盖：① MAX_TRANSCRIPT_CHARS = 20 万字（原 3 万被翻案）；② 3 万~20 万字之间的
转写一句不丢（旧阈值会误截）；③ 超过 20 万字从头截断并报出被省略句数；④ 请求
payload 的 max_tokens = 65536（原 4096 被 thinking 吃光；32768 对 5 小时级长会
偏紧，2026-09-17 晚再翻案）；⑤ 轮次口径：按发言人
轮次整轮截断（不切在某人一轮话中间）、单轮自身超限时不返回空、行长含行首
`[mm:ss] ` 前缀（与 build_transcript 共用同一 helper）。
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import minutes_llm
from minutes_llm import (MAX_TOKENS, MAX_TRANSCRIPT_CHARS, build_transcript,
                         truncate_utterances)

_CFG = {"deepseek": {"api_key": "test-key", "model": "deepseek-flash",
                     "endpoint": "https://api.deepseek.com/v1/chat/completions"}}


def _utts(total_chars, chunk=300):
    """造一组分句，正文合计约 total_chars 字（每句 chunk 字）。"""
    n = max(1, total_chars // chunk)
    return [{"speaker": "0", "speaker_name": "张三", "text": "字" * chunk}
            for _ in range(n)]


class ThresholdTest(unittest.TestCase):
    """① 阈值定版：20 万字。"""

    def test_threshold_is_200k(self):
        self.assertEqual(MAX_TRANSCRIPT_CHARS, 200000)

    def test_max_tokens_is_65536(self):
        self.assertEqual(MAX_TOKENS, 65536)


class TruncateTest(unittest.TestCase):
    """②③ 截断行为：20 万字以内不丢句，超出才从头截断。"""

    def test_long_transcript_under_threshold_kept_whole(self):
        """3 万字（旧阈值边界）~20 万字之间的转写全部保留——旧 3 万阈值会误截。"""
        utts = _utts(120000)
        kept, dropped = truncate_utterances(utts)
        self.assertEqual(dropped, 0)
        self.assertEqual(len(kept), len(utts))

    def test_over_threshold_truncates_tail(self):
        """超过 20 万字：从头保留（报到都在头部），尾部整句丢弃并报出句数。"""
        utts = _utts(260000)
        kept, dropped = truncate_utterances(utts)
        self.assertGreater(dropped, 0)
        self.assertEqual(len(kept) + dropped, len(utts))
        self.assertEqual(kept[0], utts[0])          # 头部保留
        self.assertEqual(kept[-1], utts[len(kept) - 1])
        size = sum(len(u["speaker_name"]) + len(u["text"]) + 2 for u in kept)
        self.assertLessEqual(size, MAX_TRANSCRIPT_CHARS)
        self.assertGreater(size, MAX_TRANSCRIPT_CHARS - 400)  # 确实填到接近上限

    def test_explicit_max_chars_still_works(self):
        """显式传 max_chars（测试/调参用）仍按参数截断。"""
        utts = _utts(3000, chunk=100)
        kept, dropped = truncate_utterances(utts, max_chars=500)
        self.assertGreater(dropped, 0)
        self.assertLessEqual(
            sum(len(u["speaker_name"]) + len(u["text"]) + 2 for u in kept), 500)


def _turn(name, count, text_chars=10):
    """连续同一个发言人说 count 句（= 一轮）。"""
    return [{"speaker": "0", "speaker_name": name, "text": "字" * text_chars}
            for _ in range(count)]


class TurnTruncateTest(unittest.TestCase):
    """⑤ 轮次截断口径（2026-09-17 修正，原实现会切在半轮话中间）。"""

    def test_does_not_cut_mid_turn(self):
        """一轮整体放不下 → 这一轮整轮不要（旧实现会留下李四的半轮话）。"""
        utts = _turn("张三", 3) + _turn("李四", 3)
        kept, dropped = truncate_utterances(utts, max_chars=60)
        self.assertEqual([u["speaker_name"] for u in kept], ["张三"] * 3)
        self.assertEqual(dropped, 3)

    def test_single_oversized_turn_keeps_head(self):
        """单轮自身就超限（整场只有一个人说）：保住开头，不返回空。"""
        utts = _turn("张三", 5, text_chars=100)
        kept, dropped = truncate_utterances(utts, max_chars=50)
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0], utts[0])
        self.assertEqual(dropped, 4)

    def test_timestamp_prefix_counted_in_line_size(self):
        """行长计行首 `[mm:ss] ` 前缀：保留下来的转写实际长度不超上限。

        旧口径只算「姓名+正文」不算前缀，同样 60 字上限会多留一句（真正发出去
        的转写比上限长）。
        """
        utts = [{"speaker": "0", "speaker_name": "张三", "text": "字" * 10,
                 "start_time": i * 60000} for i in range(6)]
        kept, _ = truncate_utterances(utts, max_chars=60)
        self.assertEqual(len(kept), 2)
        self.assertLessEqual(len(build_transcript(kept)), 60)
        self.assertTrue(build_transcript(kept).startswith("[00:00] 张三："))

    def test_line_size_matches_rendered_line(self):
        """截断口径 = build_transcript 实际写出去的一整行（含前缀和换行）。"""
        u = {"speaker": "0", "speaker_name": "张三", "text": "你好",
             "start_time": 65000}
        rendered = build_transcript([u], base_ms=0)
        self.assertIn("[01:05] 张三：你好", rendered)
        self.assertEqual(minutes_llm._line_size(u, 0), len(rendered) + 1)


class PayloadTest(unittest.TestCase):
    """④ 请求参数：max_tokens 给足 65536（thinking 也占额度）。"""

    def _captured_payload(self):
        captured = {}

        def fake_post(ds, payload):
            captured["payload"] = payload
            return "# 会议主题"

        with mock.patch.object(minutes_llm, "_post_chat", fake_post):
            minutes_llm.generate_minutes(_CFG, _utts(600))
        return captured["payload"]

    def test_max_tokens_in_payload(self):
        payload = self._captured_payload()
        self.assertEqual(payload["max_tokens"], MAX_TOKENS)
        self.assertEqual(payload["model"], "deepseek-flash")
        self.assertNotIn("thinking", payload)  # 仍然不禁 thinking


if __name__ == "__main__":
    unittest.main(verbosity=2)
