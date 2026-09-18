# -*- coding: utf-8 -*-
"""GUI 契约测试：_on_progress / _finish / _poll_queues（不开真 Tk 窗口）。

用 `App.__new__(App)` + 桩掉 messagebox / os.startfile / 状态与按钮，直接调方法。
覆盖两条被回归的契约：
① 工作线程的完成事件是 4 元组 (docx, md, pdf, image)：_finish 必须能解包，
   信息图路径（非 None 时）要进完成文案，并打开输出目录；信息图为 None 时不提；
② 管线出稿末尾自己发的 ("done", "") 只是过场事件，不是结果——必须被忽略，
   不能弹完成框、不能复位按钮；随后真正的 4 元组结果照样完成。
另覆盖：error 分支弹中文错误框；上一场会议迟到的 stream_error/stream_ok 被丢弃；
_poll_queues 回调抛异常时轮询链（root.after）不断。
"""
import os
import queue
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import 会议记录


def _app():
    """只搭 _on_progress / _finish / _poll_queues 用得到的桩。"""
    app = 会议记录.App.__new__(会议记录.App)
    app.root = mock.Mock()
    app.status = mock.Mock()
    app.elapsed = mock.Mock()
    app.level = mock.Mock()
    app.btn_start = mock.Mock()
    app.btn_stop = mock.Mock()
    app.recorder = mock.Mock()
    app.recorder.level_queue = queue.Queue()
    app.recorder.recording = False
    app.recorder.elapsed = 0.0
    app.utter_q = queue.Queue()
    app.progress_q = queue.Queue()
    app.speaker_map = mock.Mock()
    app.speaker_map.update.side_effect = (
        lambda u: (dict(u, speaker_name="张三"), {}))
    app.session = None
    app.rolling = None
    app.worker = None
    app.cfg = {}
    app.meeting_time = None
    app.hotwords = []
    app.stream_connect_failed = False
    app.utterances = []
    app._got_text = True
    app._error_box_shown = False
    return app


class DoneEventContractTest(unittest.TestCase):
    """① 完成事件解包 + ② 过场 done 事件。"""

    def setUp(self):
        self.app = _app()
        self.detail = (r"C:\输出\会议纪要.docx", r"C:\输出\会议纪要.md",
                       r"C:\输出\会议纪要.pdf", r"C:\输出\会议纪要_总结图.png")

    def test_done_with_result_tuple_finishes(self):
        """4 元组结果事件：不抛异常、四个路径都在提示里、自动打开输出目录。"""
        with mock.patch.object(会议记录, "messagebox") as mb, \
                mock.patch.object(会议记录.os, "startfile", create=True) as start:
            会议记录.App._on_progress(self.app, "done", self.detail)
        mb.showinfo.assert_called_once()
        text = mb.showinfo.call_args[0][1]
        for path in self.detail:
            self.assertIn(path, text)
        start.assert_called_once_with(r"C:\输出")
        # 完成即复位按钮
        self.app.btn_start.config.assert_called_with(state="normal")
        self.app.btn_stop.config.assert_called_with(state="disabled")

    def test_done_without_image_omits_image_line(self):
        detail = (r"C:\输出\m.docx", r"C:\输出\m.md", r"C:\输出\m.pdf", None)
        with mock.patch.object(会议记录, "messagebox") as mb, \
                mock.patch.object(会议记录.os, "startfile", create=True):
            会议记录.App._on_progress(self.app, "done", detail)
        text = mb.showinfo.call_args[0][1]
        self.assertNotIn("总结信息图", text)
        self.assertIn(r"C:\输出\m.pdf", text)

    def test_done_without_pdf_still_mentions_pdf_hint(self):
        detail = (r"C:\输出\m.docx", r"C:\输出\m.md", None, None)
        with mock.patch.object(会议记录, "messagebox") as mb, \
                mock.patch.object(会议记录.os, "startfile", create=True):
            会议记录.App._on_progress(self.app, "done", detail)
        self.assertIn("PDF 这次没生成", mb.showinfo.call_args[0][1])

    def test_pipeline_done_event_is_ignored(self):
        """管线自己在出稿末尾发的 ("done", "")：不是结果，不能当完成处理。"""
        with mock.patch.object(会议记录, "messagebox") as mb, \
                mock.patch.object(会议记录.App, "_finish") as fin:
            会议记录.App._on_progress(self.app, "done", "")
        fin.assert_not_called()
        mb.showinfo.assert_not_called()
        self.app.btn_start.config.assert_not_called()

    def test_result_after_pipeline_done_event_still_finishes(self):
        """真实顺序：先过场 done("")、后 4 元组结果 → 只完成一次。"""
        with mock.patch.object(会议记录, "messagebox") as mb, \
                mock.patch.object(会议记录.os, "startfile", create=True):
            会议记录.App._on_progress(self.app, "done", "")
            会议记录.App._on_progress(self.app, "done", self.detail)
        self.assertEqual(mb.showinfo.call_count, 1)

    def test_short_tuple_is_not_a_result(self):
        with mock.patch.object(会议记录, "messagebox") as mb, \
                mock.patch.object(会议记录.App, "_finish") as fin:
            会议记录.App._on_progress(self.app, "done", ("a.docx", "a.md"))
        fin.assert_not_called()
        mb.showinfo.assert_not_called()

    def test_error_branch_shows_chinese_box(self):
        with mock.patch.object(会议记录, "messagebox") as mb:
            会议记录.App._on_progress(self.app, "error", "这次会议没有实时转写出任何内容。")
        mb.showerror.assert_called_once()
        self.assertIn("没有实时转写", mb.showerror.call_args[0][1])


class StreamSessionFenceTest(unittest.TestCase):
    """上一场会议迟到的流式连接结果必须丢弃（B3）。"""

    def test_stale_stream_error_dropped(self):
        app = _app()
        current, stale = mock.Mock(), mock.Mock()
        app.session = current
        会议记录.App._on_progress(app, "stream_error", (stale, "连接超时"))
        self.assertFalse(app.stream_connect_failed)
        会议记录.App._on_progress(app, "stream_error", (current, "连接超时"))
        self.assertTrue(app.stream_connect_failed)

    def test_stale_stream_ok_dropped(self):
        app = _app()
        current, stale = mock.Mock(), mock.Mock()
        app.session = current
        with self.assertLogs("会议记录", level="INFO") as cm:
            会议记录.App._on_progress(app, "stream_ok", (stale, None))
        self.assertIn("丢弃上一场会议", "\n".join(cm.output))


class PollQueuesResilienceTest(unittest.TestCase):
    """_poll_queues 里的回调抛异常也不能让轮询链断掉（B2）。"""

    def test_survives_utterance_callback_exception(self):
        app = _app()
        app.utter_q.put({"speaker": "0", "text": "我是张三"})
        app.speaker_map.update.side_effect = RuntimeError("映射炸了")
        with mock.patch.object(会议记录.App, "_append_utterance"), \
                self.assertLogs("会议记录", level="ERROR"):
            会议记录.App._poll_queues(app)
        app.root.after.assert_called_once()   # 心跳照排
        self.assertEqual(app.utterances, [])  # 出错的这句不进摘要数据源

    def test_survives_progress_callback_exception(self):
        app = _app()
        app.progress_q.put(("done", ("a.docx", "a.md", None, None)))
        with mock.patch.object(会议记录.App, "_on_progress",
                               side_effect=RuntimeError("结果处理炸了")), \
                self.assertLogs("会议记录", level="ERROR"):
            会议记录.App._poll_queues(app)
        app.root.after.assert_called_once()

    def test_normal_round_reschedules(self):
        app = _app()
        app.utter_q.put({"speaker": "0", "text": "我是张三"})
        with mock.patch.object(会议记录.App, "_append_utterance") as append:
            会议记录.App._poll_queues(app)
        append.assert_called_once()
        self.assertEqual(len(app.utterances), 1)
        app.root.after.assert_called_once()


class TickHeartbeatTest(unittest.TestCase):
    """_tick 的重新排期在 finally 里：刷新炸了也不能停心跳（B2）。"""

    def test_tick_keeps_heartbeat_when_refresh_raises(self):
        app = _app()
        app.recorder.recording = True
        app.recorder.elapsed = 5.0
        with mock.patch.object(会议记录.App, "_maybe_start_digest",
                              side_effect=RuntimeError("摘要炸了")), \
                self.assertLogs("会议记录", level="ERROR"):
            会议记录.App._tick(app)
        app.root.after.assert_called_once()
        self.assertEqual(app.root.after.call_args[0][0], 500)

    def test_tick_stops_after_recording_ends(self):
        app = _app()
        app.recorder.recording = False
        会议记录.App._tick(app)
        app.root.after.assert_not_called()          # 停录后不再排期
        app.elapsed.config.assert_called_with(text="")


class SessionLifecycleTest(unittest.TestCase):
    """B3：收尾失败也要关流式会话；工作线程绝不静默死亡；roll 摘要用原子快照。"""

    def test_on_stop_failure_still_finishes_session(self):
        app = _app()
        app.recorder.recording = False
        app.recorder.stop.side_effect = 会议记录.RecorderError("录音太短（0.3 秒）")
        session = mock.Mock()
        app.session = session
        with mock.patch.object(会议记录, "messagebox") as mb:
            会议记录.App.on_stop(app)
        session.finish.assert_called_once()         # 流式线程/连接不残留
        self.assertIsNone(app.session)              # 引用摘掉，不污染下一场
        mb.showwarning.assert_called_once()
        self.assertIsNone(app.worker)               # 没起出稿线程
        app.btn_start.config.assert_called_with(state="normal")
        app.btn_stop.config.assert_called_with(state="disabled")

    def test_worker_reports_error_instead_of_dying_silently(self):
        app = _app()
        with mock.patch.object(会议记录.App, "_run_pipeline",
                               side_effect=RuntimeError("收尾炸了")), \
                self.assertLogs("会议记录", level="ERROR"):
            会议记录.App._worker_run(app)
        state, detail = app.progress_q.get_nowait()
        self.assertEqual(state, "error")
        self.assertIn("日志", detail)               # 中文提示，界面不会停在「收尾中」

    def test_run_pipeline_uses_atomic_snapshot(self):
        app = _app()
        app.rolling = mock.Mock()
        app.rolling.snapshot.return_value = ("前半场摘要", 4)
        with mock.patch.object(会议记录, "run_pipeline_streaming",
                               return_value=("a.docx",
                                             {"md_path": "a.md"})) as run:
            会议记录.App._run_pipeline(app)
        app.rolling.snapshot.assert_called_once()   # 不分成两次读 text/covered
        self.assertEqual(run.call_args[1]["digest"], "前半场摘要")
        self.assertEqual(run.call_args[1]["digest_covered"], 4)

    def test_run_pipeline_passes_four_tuple_result(self):
        app = _app()
        with mock.patch.object(会议记录, "run_pipeline_streaming",
                               return_value=("a.docx",
                                             {"md_path": "a.md",
                                              "pdf_path": "a.pdf",
                                              "image_path": "a.png"})):
            会议记录.App._run_pipeline(app)
        state, detail = app.progress_q.get_nowait()
        self.assertEqual(state, "done")
        self.assertEqual(detail, ("a.docx", "a.md", "a.pdf", "a.png"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
