# -*- coding: utf-8 -*-
"""meeting_time 解析 + CLI 输入源校验单测（CLI 没有「点开始录音」时刻，从文件名时间戳取）。

覆盖：① 录音 wav / 流式 jsonl / 带目录路径都能解析；没有时间戳、日期不合法、
空值一律返回 None（交给 generate_minutes 写「未记录」）；② CLI 入口：输入源校验
（都不给 → 中文用法提示 + 退出码 2，不再喷 TypeError 英文栈）、错误包成中文
「失败：」+ 退出码 1、死参数 --file-mode 已删。
"""
import contextlib
import io
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pipeline
from pipeline import meeting_time_from_text


class MeetingTimeTest(unittest.TestCase):
    def test_wav_name(self):
        self.assertEqual(meeting_time_from_text("会议录音_20260902_1954.wav"),
                         "2026-09-02 19:54")

    def test_jsonl_name(self):
        self.assertEqual(meeting_time_from_text("transcript_20260916_1546.jsonl"),
                         "2026-09-16 15:46")

    def test_path_with_dirs(self):
        self.assertEqual(
            meeting_time_from_text(r"D:\数字合伙人\会议记录\录音\会议录音_20260902_2201.wav"),
            "2026-09-02 22:01")

    def test_dash_separator_also_ok(self):
        self.assertEqual(meeting_time_from_text("meeting-20260902-1954.wav"),
                         "2026-09-02 19:54")

    def test_no_timestamp_returns_none(self):
        for text in ("", None, "会议录音.wav", "recordings/audio.wav"):
            self.assertIsNone(meeting_time_from_text(text))

    def test_invalid_date_returns_none(self):
        self.assertIsNone(meeting_time_from_text("会议录音_20261340_1546.wav"))
        self.assertIsNone(meeting_time_from_text("会议录音_20260916_9966.wav"))


class CliTest(unittest.TestCase):
    """② CLI 入口（pipeline.main）：输入源校验 + 中文错误包装。"""

    def _run(self, argv):
        """跑一次 main（不打日志、不读真配置、不碰网络），返回 (退出码, 输出文本)。"""
        buf = io.StringIO()
        with mock.patch.object(pipeline, "setup_logging"), \
                mock.patch.object(pipeline, "load_hotwords", return_value=[]), \
                mock.patch.object(pipeline, "load_config", return_value={}), \
                contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            code = pipeline.main(argv)
        return code, buf.getvalue()

    def test_file_mode_flag_gone(self):
        """死参数 --file-mode 已删（CLI 默认即文件识别路径，验收走 --test-stream-wav）。"""
        buf = io.StringIO()
        with mock.patch.object(pipeline, "setup_logging"), \
                contextlib.redirect_stdout(buf), \
                self.assertRaises(SystemExit) as ctx:
            pipeline.main(["--help"])
        self.assertEqual(ctx.exception.code, 0)
        self.assertNotIn("--file-mode", buf.getvalue())
        self.assertIn("--test-stream-wav", buf.getvalue())

    def test_no_input_source_prints_usage(self):
        """输入源三者都不给 → 中文用法提示 + 退出码 2（原来在深处抛 TypeError）。"""
        code, out = self._run([])
        self.assertEqual(code, 2)
        self.assertIn("没有指定输入音频", out)
        self.assertIn("--test-stream-wav", out)
        self.assertIn("--audio-url", out)
        self.assertNotIn("Traceback", out)

    def test_mock_asr_not_json_gives_chinese_error(self):
        """--mock-asr 指向非 JSON 文件（JSONDecodeError）→ 中文「失败：」+ 退出码 1。"""
        with tempfile.TemporaryDirectory() as d:
            bad = os.path.join(d, "fake.json")
            with open(bad, "w", encoding="utf-8") as f:
                f.write("这不是 JSON")
            code, out = self._run(["--mock-asr", bad])
        self.assertEqual(code, 1)
        self.assertIn("失败：", out)
        self.assertNotIn("Traceback", out)

    def test_stream_test_error_wrapped_in_chinese(self):
        """--test-stream-wav 里抛的异常同样包成「失败：」+ 退出码 1（不喷英文栈）。"""
        for exc in (RuntimeError("流式识别失败：连不上"),
                    pipeline.ConfigError("缺少配置")):
            with self.subTest(exc=type(exc).__name__):
                buf = io.StringIO()
                with mock.patch.object(pipeline, "setup_logging"), \
                        mock.patch.object(pipeline, "_test_stream_wav",
                                          side_effect=exc), \
                        contextlib.redirect_stdout(buf), \
                        contextlib.redirect_stderr(buf):
                    code = pipeline.main(["--test-stream-wav", "x.wav"])
                self.assertEqual(code, 1)
                self.assertIn(f"失败：{exc}", buf.getvalue())
                self.assertNotIn("Traceback", buf.getvalue())


if __name__ == "__main__":
    unittest.main(verbosity=2)
