# -*- coding: utf-8 -*-
"""纪要/滚动摘要 prompt 单测：钉钉智能纪要模板、时间戳前缀、会议时间、摘要合并。

覆盖：① 七段栏目（总结/后续工作计划/待办/智能章节/关键决策/金句时刻 + 标题）顺序齐全；
② 头部三行（录音主题/录音时间/参会人员）与标题日期；③ 时间戳前缀 mm:ss / hh:mm:ss /
无 start_time 兼容 / 切片时用会议零点；④ meeting_time 格式化与「未记录」；
⑤ 热词白名单段有才加；⑥ 滚动摘要段落只在有 prior_digest 时出现；
⑦ 滚动摘要 prompt 要求保留 [mm:ss] 时间点；⑧ generate_digest 走同一条 DeepSeek 通道。
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import minutes_llm
from minutes_llm import (DIGEST_MERGE_BLOCK, _time_stamp, build_digest_messages,
                         build_transcript, format_meeting_time, generate_digest,
                         generate_minutes, meeting_date)

_CFG = {"deepseek": {"api_key": "test-key", "model": "deepseek-flash",
                     "endpoint": "https://api.deepseek.com/v1/chat/completions"}}
_UTTS = [
    {"speaker": "0", "speaker_name": "徐志龙", "text": "我是徐志龙", "start_time": 912},
    {"speaker": "1", "speaker_name": "飞明", "text": "铂金装先看三天数据",
     "start_time": 62000},
]

_SECTIONS = ["# 智能纪要：", "## 总结", "## 后续工作计划", "## 待办",
             "## 智能章节", "## 关键决策", "## 金句时刻"]


def _user_prompt(utterances=None, **kw):
    """跑一遍 generate_minutes（mock 网络层）拿真正发出去的 messages。"""
    captured = {}

    def fake_post(ds, payload):
        captured["payload"] = payload
        return "# 智能纪要：示例"

    with mock.patch.object(minutes_llm, "_post_chat", fake_post):
        generate_minutes(_CFG, _UTTS if utterances is None else utterances, **kw)
    messages = captured["payload"]["messages"]
    return messages[0]["content"], messages[1]["content"]


class TemplateTest(unittest.TestCase):
    """①② 钉钉智能纪要模板结构（栏目顺序固定）。"""

    def test_all_sections_in_order(self):
        _, user = _user_prompt(meeting_time="2026-09-16 15:46")
        for frag in _SECTIONS:
            self.assertIn(frag, user)
        idx = [user.index(frag) for frag in _SECTIONS]
        self.assertEqual(idx, sorted(idx), f"栏目顺序不对：{idx}")

    def test_header_lines(self):
        _, user = _user_prompt(meeting_time="2026-09-16 15:46")
        self.assertIn("**录音主题**", user)
        self.assertIn("**录音时间**：2026年9月16日（周三） 15:46", user)
        self.assertIn("**参会人员**", user)

    def test_title_carries_date(self):
        _, user = _user_prompt(meeting_time="2026-09-16 15:46")
        self.assertIn("# 智能纪要：<一句话主题> 2026年9月16日", user)

    def test_chapter_rule_mentions_stamp_source(self):
        _, user = _user_prompt()
        self.assertIn("hh:mm:ss", user)          # 超 1 小时的写法
        self.assertIn("不许自己编", user)         # 时间戳只许取自转写
        self.assertIn("时间戳必须从小到大递增", user)

    def test_key_decision_and_quote_shape(self):
        _, user = _user_prompt()
        for frag in ("**关键决策**", "**问题**", "**讨论方案**", "**决策依据**",
                     "**其他决策**", "本场无"):
            self.assertIn(frag, user)

    def test_old_template_gone(self):
        """翻案：上一版飞书风栏目不再出现。"""
        _, user = _user_prompt()
        for gone in ("# 会议纪要", "**会议主题**", "**会议时间**",
                     "一、会议总结", "二、讨论要点", "五、遗留问题"):
            self.assertNotIn(gone, user)

    def test_quality_rules_kept(self):
        _, user = _user_prompt()
        self.assertIn("不编造", user)
        self.assertIn("不要用表格", user)      # 禁表格的旧定版已翻案，改为「其余内容不要用表格」

    def test_tables_allowed_for_comparison(self):
        """翻案：对比类数据允许用 Markdown 表格（旧 prompt 是一刀切禁止）。"""
        _, user = _user_prompt()
        self.assertIn("Markdown 表格", user)
        self.assertIn("| --- |", user)
        self.assertNotIn("不要使用表格", user)

    def test_header_is_a_quote_block(self):
        """头部三行改成引用块（渲染成一张信息卡）。"""
        _, user = _user_prompt(meeting_time="2026-09-16 15:46")
        self.assertIn("> **录音主题**", user)
        self.assertIn("> **录音时间**：2026年9月16日（周三） 15:46", user)
        self.assertIn("> **参会人员**", user)

    def test_chapter_summary_is_a_quote_block(self):
        """智能章节摘要改成引用块（渲染成章节摘要卡），并限定别处不要用。"""
        _, user = _user_prompt()
        self.assertIn("> <本章 2~4 句摘要>", user)
        self.assertIn("别处不要用", user)


class BuildTranscriptTest(unittest.TestCase):
    """③ 时间戳前缀。"""

    def test_mm_ss_prefix(self):
        text = build_transcript(_UTTS)
        self.assertEqual(text, "[00:00] 徐志龙：我是徐志龙\n"
                               "[01:01] 飞明：铂金装先看三天数据")

    def test_hour_prefix(self):
        utts = [{"speaker_name": "张三", "text": "开头", "start_time": 0},
                {"speaker_name": "张三", "text": "晚点说", "start_time": 4_380_000}]
        text = build_transcript(utts)
        self.assertIn("[00:00] 张三：开头", text)
        self.assertIn("[01:13:00] 张三：晚点说", text)

    def test_missing_start_time_has_no_prefix(self):
        utts = [{"speaker_name": "张三", "text": "有时间", "start_time": 5000},
                {"speaker_name": "李四", "text": "没时间", "start_time": None},
                {"speaker_name": "王五", "text": "没这个字段"}]
        text = build_transcript(utts)
        self.assertIn("[00:00] 张三：有时间", text)
        self.assertIn("\n李四：没时间", text)
        self.assertIn("\n王五：没这个字段", text)

    def test_all_without_start_time_is_plain(self):
        utts = [{"speaker_name": "张三", "text": "一"}, {"speaker_name": "李四", "text": "二"}]
        self.assertEqual(build_transcript(utts), "张三：一\n李四：二")

    def test_slice_uses_meeting_base(self):
        """摘要路径只发后半段时，时间戳仍按整场会议的零点算。"""
        text = build_transcript(_UTTS[1:], base_ms=912)
        self.assertTrue(text.startswith("[01:01] 飞明："))

    def test_time_stamp_helper(self):
        self.assertEqual(_time_stamp(0), "00:00")
        self.assertEqual(_time_stamp(59_000), "00:59")
        self.assertEqual(_time_stamp(3_599_000), "59:59")
        self.assertEqual(_time_stamp(3_600_000), "01:00:00")
        self.assertEqual(_time_stamp(-5), "00:00")       # 负数兜底不炸


class MeetingTimeFormatTest(unittest.TestCase):
    """④ 会议时间格式化。"""

    def test_formats_with_weekday(self):
        self.assertEqual(format_meeting_time("2026-09-16 15:46"),
                         "2026年9月16日（周三） 15:46")
        self.assertEqual(format_meeting_time("2026-09-02 10:03"),
                         "2026年9月2日（周三） 10:03")
        self.assertEqual(format_meeting_time("2026/09/17 09:05"),
                         "2026年9月17日（周四） 09:05")

    def test_unknown_passthrough_and_empty(self):
        self.assertEqual(format_meeting_time("下午三点"), "下午三点")
        self.assertEqual(format_meeting_time(None), "未记录")
        self.assertEqual(format_meeting_time("   "), "未记录")

    def test_date_only(self):
        self.assertEqual(meeting_date("2026-09-16 15:46"), "2026年9月16日")
        self.assertEqual(meeting_date(None), "")

    def test_prompt_uses_unrecorded_when_missing(self):
        _, user = _user_prompt()
        self.assertIn("**录音时间**：未记录", user)
        self.assertIn("# 智能纪要：<一句话主题> \n", user.replace("\r", ""))


class HotwordsPromptTest(unittest.TestCase):
    """⑤ 热词白名单段落：有才加。"""

    def test_with_hotwords(self):
        _, user = _user_prompt(hotwords=["徐志龙", "飞明", "老顺昌"])
        self.assertIn("以下专有名词是正确写法", user)
        self.assertIn("徐志龙、飞明、老顺昌", user)

    def test_without_hotwords(self):
        for empty in (None, [], ["", "  "]):
            _, user = _user_prompt(hotwords=empty)
            self.assertNotIn("专有名词", user)


class DigestMergePromptTest(unittest.TestCase):
    """⑥ 散会出稿：摘要段落只在有 prior_digest 时出现。"""

    def test_with_prior_digest(self):
        _, user = _user_prompt(prior_digest="[12:30] 前半场定了三人团报名少报。",
                               meeting_time="2026-09-16 15:46")
        self.assertIn("前半场定了三人团报名少报", user)
        self.assertIn("覆盖整场会议", user)
        self.assertIn("[00:00] 徐志龙：我是徐志龙", user)   # 新转写也在

    def test_without_prior_digest(self):
        _, user = _user_prompt()
        self.assertNotIn("前半场", user)
        self.assertNotIn("覆盖整场会议", user)

    def test_blank_digest_ignored(self):
        _, user = _user_prompt(prior_digest="   ")
        self.assertNotIn(DIGEST_MERGE_BLOCK.split("{")[0].strip(), user)


class DigestPromptTest(unittest.TestCase):
    """⑦ 滚动摘要 prompt：上一版摘要 + 新转写 + 必须保留时间点。"""

    def test_messages_structure(self):
        messages = build_digest_messages("[05:00] 上一版摘要：定价待定", _UTTS)
        self.assertIn("会议记录员", messages[0]["content"])
        user = messages[1]["content"]
        self.assertIn("上一版摘要：定价待定", user)
        self.assertIn("更新后的完整摘要", user)
        self.assertIn("[01:01] 飞明：铂金装先看三天数据", user)

    def test_must_keep_timestamps(self):
        user = build_digest_messages("", _UTTS)[1]["content"]
        self.assertIn("时间点标记", user)
        self.assertIn("[mm:ss]", user)
        self.assertIn("智能章节", user)          # 说明这些时间戳拿去干什么用

    def test_keeps_decision_digits_names_todo_conflicts(self):
        user = build_digest_messages("", _UTTS)[1]["content"]
        for frag in ("决策", "关键数字", "人名", "待办", "分歧"):
            self.assertIn(frag, user)

    def test_no_prior_digest(self):
        user = build_digest_messages("", _UTTS)[1]["content"]
        self.assertNotIn("已有摘要", user)
        self.assertIn("新转写", user)

    def test_generate_digest_uses_chat_channel(self):
        """⑧ generate_digest 走 _chat（同一条重试/截断分类通道）。"""
        captured = {}

        def fake_post(ds, payload):
            captured["payload"] = payload
            return "紧凑摘要：定了三人团报名。"

        with mock.patch.object(minutes_llm, "_post_chat", fake_post), \
                mock.patch.object(minutes_llm, "_chat",
                                  wraps=minutes_llm._chat) as chat_mock:
            text = generate_digest(_CFG, "旧摘要", _UTTS, base_start_ms=912)
        self.assertEqual(text, "紧凑摘要：定了三人团报名。")
        self.assertEqual(chat_mock.call_count, 1)
        payload = captured["payload"]
        self.assertEqual(payload["max_tokens"], minutes_llm.MAX_TOKENS)
        self.assertEqual(payload["model"], "deepseek-flash")

    def test_generate_digest_needs_key(self):
        with self.assertRaises(minutes_llm.ConfigError):
            generate_digest({"deepseek": {"api_key": ""}}, "", _UTTS)


class TruncateSharedTest(unittest.TestCase):
    """摘要/纪要共用同一套 20 万字截断口径。"""

    def test_digest_transcript_truncated(self):
        utts = [{"speaker": "0", "speaker_name": "张三", "text": "字" * 300}
                for _ in range(900)]  # ≈27 万字
        user = build_digest_messages("", utts)[1]["content"]
        self.assertIn("因长度限制被省略", user)


if __name__ == "__main__":
    unittest.main(verbosity=2)
