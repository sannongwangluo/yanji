# -*- coding: utf-8 -*-
"""滚动摘要单测：状态机（触发/快照/回填/落盘）+ 散会合并与失败回退。

覆盖：① 阈值 30 分钟、间隔 20 分钟、新增太少不跑、busy 不重入；
② begin 快照（上一版摘要 + 新增分句 + 覆盖条数）、commit 更新 + 原子落盘、fail 复位；
③ 三段转写模拟：摘要逐段更新、覆盖条数累加、每段一个 digest_*.md；
④ run_pipeline_streaming：有摘要 → 只发摘要之后的新转写（prior_digest=摘要）；
⑤ 摘要路径出稿失败 → 回退整稿路径（不带 prior_digest），出稿不受影响；
⑥ 没有摘要 → 与原来一字不差的整稿路径。
"""
import os
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import digest
import pipeline
from digest import (DIGEST_INTERVAL_SEC, DIGEST_MIN_UTTERANCES,
                    DIGEST_THRESHOLD_SEC, RollingDigest)

import 会议记录


def _utts(n, start=0):
    return [{"speaker": "0", "speaker_name": "张三",
             "text": f"第{i}句发言内容"} for i in range(start, start + n)]


class RollingDigestTest(unittest.TestCase):
    """①②③ 状态机。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.rd = RollingDigest(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_thresholds_are_30min_and_20min(self):
        self.assertEqual(DIGEST_THRESHOLD_SEC, 30 * 60)
        self.assertEqual(DIGEST_INTERVAL_SEC, 20 * 60)
        self.assertEqual(self.rd._next_at, DIGEST_THRESHOLD_SEC)

    def test_not_due_before_threshold(self):
        self.assertFalse(self.rd.due(DIGEST_THRESHOLD_SEC - 1, 100))

    def test_due_at_threshold_with_enough_utterances(self):
        self.assertTrue(self.rd.due(DIGEST_THRESHOLD_SEC, DIGEST_MIN_UTTERANCES))
        self.assertFalse(self.rd.due(DIGEST_THRESHOLD_SEC, DIGEST_MIN_UTTERANCES - 1))

    def test_begin_snapshot_and_busy_guard(self):
        utts = _utts(20)
        snap = self.rd.begin(utts)
        self.assertEqual(snap["prior"], "")
        self.assertEqual(len(snap["new"]), 20)
        self.assertEqual(snap["covered"], 20)
        self.assertTrue(self.rd.busy)
        self.assertIsNone(self.rd.begin(utts))          # busy 不重入
        self.assertFalse(self.rd.due(99999, 999))       # busy 时 due 也是 False

    def test_commit_updates_and_saves(self):
        utts = _utts(20)
        snap = self.rd.begin(utts)
        path = self.rd.commit("摘要第一版：定了三人团。", snap["covered"])
        self.assertEqual(self.rd.text, "摘要第一版：定了三人团。")
        self.assertEqual(self.rd.covered, 20)
        self.assertEqual(self.rd.count, 1)
        self.assertFalse(self.rd.busy)
        self.assertTrue(path and path.endswith(".md"))
        with open(path, encoding="utf-8") as f:
            self.assertEqual(f.read().strip(), "摘要第一版：定了三人团。")
        self.assertEqual(self.rd._next_at, DIGEST_THRESHOLD_SEC + DIGEST_INTERVAL_SEC)
        self.assertEqual([n for n in os.listdir(self.tmp.name) if ".tmp" in n], [])

    def test_next_round_carries_prior_digest_and_only_new_utterances(self):
        utts = _utts(40)
        self.rd.commit("第一版", self.rd.begin(utts)["covered"])
        self.assertIsNone(self.rd.begin(utts))          # 没有新分句 → 不跑
        snap = self.rd.begin(utts + _utts(10, start=40))
        self.assertEqual(snap["prior"], "第一版")
        self.assertEqual(len(snap["new"]), 10)
        self.assertEqual(snap["covered"], 50)

    def test_fail_resets_busy_and_defers_next_round(self):
        utts = _utts(50)
        self.rd.begin(utts)
        self.rd.fail("网络错误")
        self.assertFalse(self.rd.busy)
        self.assertEqual(self.rd.last_error, "网络错误")
        self.assertEqual(self.rd.count, 0)
        self.assertEqual(self.rd.text, "")              # 失败不污染已有摘要
        self.assertFalse(self.rd.due(DIGEST_THRESHOLD_SEC, 50))   # 本轮不重试
        self.assertTrue(self.rd.due(DIGEST_THRESHOLD_SEC + DIGEST_INTERVAL_SEC,
                                    50 + DIGEST_MIN_UTTERANCES))

    def test_save_avoids_overwrite(self):
        self.rd.text = "一"
        first = self.rd.save()
        self.rd.text = "二"
        second = self.rd.save()
        self.assertNotEqual(first, second)
        self.assertTrue(os.path.exists(first) and os.path.exists(second))

    def test_three_segment_simulation(self):
        """③ 把一整场切成 3 段喂进去：摘要逐段更新、覆盖条数累加、3 个文件。"""
        all_utts = _utts(90)
        texts = ["第一段摘要", "第一段摘要 + 第二段新增", "三段合并摘要"]
        elapsed = DIGEST_THRESHOLD_SEC
        for i in range(3):
            seg_end = (i + 1) * 30
            seen = all_utts[:seg_end]
            self.assertTrue(self.rd.due(elapsed, len(seen)), f"第 {i+1} 段应触发")
            snap = self.rd.begin(seen)
            self.assertEqual(len(snap["new"]), 30)
            self.assertEqual(snap["prior"], texts[i - 1] if i else "")
            self.rd.commit(texts[i], snap["covered"])
            elapsed += DIGEST_INTERVAL_SEC
        self.assertEqual(self.rd.count, 3)
        self.assertEqual(self.rd.covered, 90)
        self.assertEqual(self.rd.text, "三段合并摘要")
        files = sorted(n for n in os.listdir(self.tmp.name) if n.endswith(".md"))
        self.assertEqual(len(files), 3)
        # 最后一个文件是最新摘要（崩机后人工补救用）
        with open(os.path.join(self.tmp.name, files[-1]), encoding="utf-8") as f:
            self.assertEqual(f.read().strip(), "三段合并摘要")


class PipelineMergeTest(unittest.TestCase):
    """④⑤⑥ 散会出稿：摘要合并与失败回退。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.utts = _utts(10)
        self.cfg = {"app": {"output_dir": self.tmp.name}}

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, fake_minutes, **kw):
        with mock.patch.object(pipeline, "generate_minutes", fake_minutes), \
                mock.patch.object(pipeline, "save_minutes_pair",
                                  return_value=("x.docx", "x.md")):
            return pipeline.run_pipeline_streaming(self.utts, cfg=self.cfg, **kw)

    def test_digest_path_sends_only_new_utterances(self):
        calls = []

        def fake_minutes(cfg, utterances, **kw):
            calls.append((list(utterances), kw))
            return "# 会议纪要"

        self._run(fake_minutes, digest="前半场摘要", digest_covered=4)
        self.assertEqual(len(calls), 1)
        utterances, kw = calls[0]
        self.assertEqual(len(utterances), 6)                       # 只发摘要之后的新转写
        self.assertEqual(utterances[0]["text"], "第4句发言内容")
        self.assertEqual(kw["prior_digest"], "前半场摘要")

    def test_fallback_when_digest_path_fails(self):
        """摘要路径抛异常 → 回退整稿（不带 prior_digest），出稿照常成功。"""
        calls = []

        def fake_minutes(cfg, utterances, **kw):
            calls.append((list(utterances), kw))
            if kw.get("prior_digest"):
                raise RuntimeError("模拟摘要路径失败")
            return "# 会议纪要（整稿）"

        with self.assertLogs("会议记录", level="ERROR"):
            docx, report = self._run(fake_minutes, digest="前半场摘要", digest_covered=4)
        self.assertEqual(docx, "x.docx")
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(calls[0][0]), 6)                      # 先试摘要路径
        self.assertEqual(len(calls[1][0]), 10)                     # 回退整稿
        self.assertIsNone(calls[1][1].get("prior_digest"))
        self.assertEqual(report["utterance_count"], 10)

    def test_no_digest_is_plain_path(self):
        calls = []

        def fake_minutes(cfg, utterances, **kw):
            calls.append((list(utterances), kw))
            return "# 会议纪要"

        self._run(fake_minutes)
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(calls[0][0]), 10)
        self.assertIsNone(calls[0][1].get("prior_digest"))
        self.assertIsNone(calls[0][1]["meeting_time"])

    def test_digest_covered_zero_falls_back_to_plain(self):
        """digest_covered=0（或 ≥ 总数）时不许走摘要路径。"""
        calls = []

        def fake_minutes(cfg, utterances, **kw):
            calls.append((list(utterances), kw))
            return "# 会议纪要"

        self._run(fake_minutes, digest="摘要", digest_covered=0)
        self._run(fake_minutes, digest="摘要", digest_covered=10)
        for utterances, kw in calls:
            self.assertEqual(len(utterances), 10)
            self.assertIsNone(kw.get("prior_digest"))

    def test_meeting_time_and_hotwords_forwarded(self):
        calls = []

        def fake_minutes(cfg, utterances, **kw):
            calls.append((list(utterances), kw))
            return "# 会议纪要"

        self._run(fake_minutes, meeting_time="2026-09-16 15:46",
                  hotwords=["徐志龙"])
        self.assertEqual(calls[0][1]["meeting_time"], "2026-09-16 15:46")
        self.assertEqual(calls[0][1]["hotwords"], ["徐志龙"])


class GuiDigestProgressTest(unittest.TestCase):
    """GUI 侧接线（只测 _on_progress 的纯逻辑，不开窗口）。

    重点：上一场会议的迟到摘要结果必须被丢弃，不能污染新一场会议的摘要状态。
    """

    def _app_stub(self, rolling):
        return mock.Mock(rolling=rolling)

    def test_digest_ok_commits_on_current_meeting(self):
        rolling = mock.Mock()
        app = self._app_stub(rolling)
        会议记录.App._on_progress(app, "digest_ok", (rolling, "摘要文本", 30))
        rolling.commit.assert_called_once_with("摘要文本", 30)

    def test_stale_digest_ok_dropped(self):
        current, stale = mock.Mock(), mock.Mock()
        app = self._app_stub(current)
        会议记录.App._on_progress(app, "digest_ok", (stale, "上一场的摘要", 30))
        stale.commit.assert_not_called()
        current.commit.assert_not_called()

    def test_digest_fail_resets_only_current(self):
        current, stale = mock.Mock(), mock.Mock()
        app = self._app_stub(current)
        会议记录.App._on_progress(app, "digest_fail", (current, "网络错误"))
        current.fail.assert_called_once_with("网络错误")
        会议记录.App._on_progress(app, "digest_fail", (stale, "网络错误"))
        stale.fail.assert_not_called()


class DigestAtomicSaveTest(unittest.TestCase):
    """B9/B13：save 原子落盘、失败不抛（它在 GUI 主线程里跑）、snapshot 一致性。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.rd = RollingDigest(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_save_uses_temp_file_and_os_replace(self):
        """真断言：确实经同目录临时文件 + os.replace 落盘（而不是直接 open("w")）。"""
        real_replace = os.replace
        calls = []

        def fake_replace(src, dst):
            calls.append((src, dst))
            real_replace(src, dst)

        self.rd.text = "摘要"
        with mock.patch.object(digest.os, "replace", side_effect=fake_replace):
            path = self.rd.save()
        self.assertEqual(len(calls), 1)
        src, dst = calls[0]
        self.assertEqual(dst, path)
        self.assertNotEqual(src, path)
        self.assertTrue(src.endswith(".tmp"))
        self.assertEqual(os.path.dirname(src), self.tmp.name)
        self.assertFalse(os.path.exists(src))
        with open(path, encoding="utf-8") as f:
            self.assertEqual(f.read().strip(), "摘要")

    def test_save_returns_none_when_makedirs_fails(self):
        """makedirs 在 try 之外会穿进 GUI 主线程打死轮询链——必须吞掉返回 None。"""
        with mock.patch("digest.os.makedirs", side_effect=OSError("磁盘满")):
            with self.assertLogs("会议记录", level="WARNING") as cm:
                self.assertIsNone(self.rd.save())
        self.assertIn("落盘失败", "\n".join(cm.output))

    def test_commit_survives_save_failure(self):
        snap = self.rd.begin(_utts(20))
        with mock.patch("digest.os.makedirs", side_effect=OSError("磁盘满")):
            self.assertIsNone(self.rd.commit("摘要", snap["covered"]))
        self.assertEqual(self.rd.text, "摘要")
        self.assertEqual(self.rd.covered, 20)
        self.assertEqual(self.rd.count, 1)
        self.assertFalse(self.rd.busy)

    def test_snapshot_returns_both_fields(self):
        self.assertEqual(self.rd.snapshot(), ("", 0))
        self.rd.begin(_utts(20))
        self.rd.commit("第一版", 20)
        self.assertEqual(self.rd.snapshot(), ("第一版", 20))

    def test_snapshot_never_sees_torn_state(self):
        """并发 commit 时不许读到「新 covered + 旧 text」（那段发言会从终稿丢掉）。"""
        done = threading.Event()

        def writer():
            for i in range(1, 51):
                self.rd.commit(f"第{i}版", i)
            done.set()

        t = threading.Thread(target=writer)
        t.start()
        try:
            while not done.is_set():
                text, covered = self.rd.snapshot()
                self.assertEqual(text, f"第{covered}版" if covered else "")
        finally:
            t.join()


if __name__ == "__main__":
    unittest.main(verbosity=2)
