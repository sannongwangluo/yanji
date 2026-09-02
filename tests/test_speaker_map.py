# -*- coding: utf-8 -*-
"""_map_speakers 纯函数单测：报到映射 + 未报到标签显示「说话人N」。

验收构造：3 个说话人（两人报到、一人未报到）+ 多句后续发言。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from speaker_map import IncrementalSpeakerMap, _display_name, _map_speakers


class SpeakerMapTest(unittest.TestCase):
    def setUp(self):
        self.utterances = [
            {"speaker": "1", "text": "我是张三", "start_time": 0, "end_time": 1500},
            {"speaker": "2", "text": "我叫李四，大家好", "start_time": 2000, "end_time": 4200},
            {"speaker": "1", "text": "今天主要讨论下季度预算怎么分", "start_time": 5000, "end_time": 9000},
            {"speaker": "3", "text": "我同意刚才的说法", "start_time": 9500, "end_time": 12000},
            {"speaker": "2", "text": "张三说得对，我补充一点", "start_time": 12500, "end_time": 15000},
        ]

    def test_mapping(self):
        mapping, _ = _map_speakers(self.utterances)
        self.assertEqual(mapping, {"1": "张三", "2": "李四"})

    def test_renamed_utterances(self):
        _, renamed = _map_speakers(self.utterances)
        self.assertEqual(renamed[0]["speaker_name"], "张三")
        self.assertEqual(renamed[1]["speaker_name"], "李四")
        self.assertEqual(renamed[2]["speaker_name"], "张三")   # 后续分句沿用映射
        self.assertEqual(renamed[3]["speaker_name"], "说话人3")  # 未报到 → 说话人N
        self.assertEqual(renamed[4]["speaker_name"], "李四")

    def test_original_fields_preserved(self):
        _, renamed = _map_speakers(self.utterances)
        self.assertEqual(renamed[0]["speaker"], "1")
        self.assertEqual(renamed[0]["text"], "我是张三")
        self.assertEqual(renamed[0]["start_time"], 0)

    def test_first_match_wins(self):
        """同一标签出现两次报到，第一个生效。"""
        utts = [
            {"speaker": "1", "text": "我是张三"},
            {"speaker": "1", "text": "其实我是张三丰"},
        ]
        mapping, renamed = _map_speakers(utts)
        self.assertEqual(mapping, {"1": "张三"})
        self.assertEqual(renamed[1]["speaker_name"], "张三")

    def test_我叫_variant(self):
        mapping, _ = _map_speakers([{"speaker": "2", "text": "我叫欧阳锋"}])
        self.assertEqual(mapping["2"], "欧阳锋")

    def test_no_checkin(self):
        mapping, renamed = _map_speakers([{"speaker": "1", "text": "没人报到"}])
        self.assertEqual(mapping, {})
        self.assertEqual(renamed[0]["speaker_name"], "说话人1")

    def test_display_name_variants(self):
        self.assertEqual(_display_name("3"), "说话人3")
        self.assertEqual(_display_name("S2"), "说话人2")
        self.assertEqual(_display_name(None), "说话人?")


class IncrementalSpeakerMapTest(unittest.TestCase):
    """流式增量映射：逐句 update，规则与全量 _map_speakers 一致。"""

    def test_incremental_updates(self):
        im = IncrementalSpeakerMap()
        u, mapping = im.update({"speaker": "1", "text": "我是张三"})
        self.assertEqual(u["speaker_name"], "张三")
        self.assertEqual(mapping, {"1": "张三"})
        # 后续同标签分句沿用映射
        u, _ = im.update({"speaker": "1", "text": "下面开始"})
        self.assertEqual(u["speaker_name"], "张三")
        # 未报到标签显示 说话人N
        u, _ = im.update({"speaker": "2", "text": "我同意"})
        self.assertEqual(u["speaker_name"], "说话人2")

    def test_first_match_wins(self):
        im = IncrementalSpeakerMap()
        im.update({"speaker": "1", "text": "我是张三"})
        u, _ = im.update({"speaker": "1", "text": "其实我是张三丰"})
        self.assertEqual(u["speaker_name"], "张三")

    def test_preserves_fields(self):
        im = IncrementalSpeakerMap()
        u, _ = im.update({"speaker": "1", "text": "我是张三",
                          "start_time": 0, "end_time": 1500})
        self.assertEqual(u["speaker"], "1")
        self.assertEqual(u["text"], "我是张三")
        self.assertEqual(u["start_time"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
