# -*- coding: utf-8 -*-
"""PDF 导出单测（mock win32com，不真开 WPS/Word）。

覆盖：① 转换成功产出 PDF + 返回路径；② COM 抛异常 → 返回 None、不抛（不炸主流程）；
③ CoInitialize/CoUninitialize 成对且顺序正确（哪怕中途异常也要成对）；
④ pywin32 没装 → 跳过并返回 None；⑤ 转换后没文件/空文件 → None；
⑥ 只读打开 + 不进最近文件 + Visible=False（不在用户屏幕上弹窗口）；
⑦ pipeline 两条路径 report 里有 pdf_path；save_pdf_from_docx 抛异常也不影响出稿。
"""
import os
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import docx_writer
import pipeline
from docx_writer import save_pdf_from_docx


class _FakeDoc:
    def __init__(self, app):
        self.app = app
        self.closed = False
        self.save_args = None

    def SaveAs(self, path, fmt):
        self.save_args = (path, fmt)
        self.app.events.append(("SaveAs", path, fmt))
        if self.app.fail_on == "SaveAs":
            raise RuntimeError("SaveAs 失败（模拟）")
        with open(path, "wb") as f:      # 假装转换出了 PDF
            f.write(b"%PDF-1.4 fake")

    def Close(self, mode):
        self.closed = True
        self.app.events.append(("Close", mode))


class _FakeDocuments:
    def __init__(self, app):
        self.app = app

    def Open(self, path, *args, **kw):
        self.app.events.append(("Open", path, args, kw))
        if self.app.hang:
            self.app.hang_event.wait()       # 模拟 WPS 弹窗/RPC 卡死（永不返回）
        if self.app.fail_on == "Open":
            raise RuntimeError("Open 失败（模拟）")
        return _FakeDoc(self.app)


class _FakeApp:
    def __init__(self, fail_on=None, quit_raises=False, hang=False):
        self.events = []
        self.fail_on = fail_on
        self.quit_raises = quit_raises
        self.hang = hang
        self.hang_event = threading.Event()      # 从不 set：卡死的 COM 调用只能等超时
        self.Documents = _FakeDocuments(self)
        self.Visible = None
        self.DisplayAlerts = None

    def _FlagAsMethod(self, name):
        self.events.append(("FlagAsMethod", name))

    def Quit(self):
        self.events.append(("Quit",))
        if self.quit_raises:
            raise RuntimeError("Quit 失败（模拟）")


def _fake_modules(app=None, dispatch_raises=None, pythoncom=None):
    """构造假的 pythoncom / win32com.client 模块（注入 sys.modules）。"""
    calls = (pythoncom if pythoncom is not None else [])

    class _PC:
        @staticmethod
        def CoInitialize():
            calls.append("CoInitialize")

        @staticmethod
        def CoUninitialize():
            calls.append("CoUninitialize")

    class _Client:
        @staticmethod
        def Dispatch(progid):
            calls.append(("Dispatch", progid))
            if dispatch_raises:
                raise dispatch_raises
            return app

    win32com_mod = type(sys)("win32com")
    win32com_client = type(sys)("win32com.client")
    win32com_client.Dispatch = _Client.Dispatch
    win32com_mod.client = win32com_client
    return {"pythoncom": _PC, "win32com": win32com_mod, "win32com.client": win32com_client}


class SavePdfTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.docx = os.path.join(self.tmp.name, "会议纪要_20260917_1605.docx")
        with open(self.docx, "wb") as f:
            f.write(b"fake docx")

    def tearDown(self):
        self.tmp.cleanup()

    def test_success_creates_pdf(self):
        """① 成功：产出同名 PDF、返回路径、COM 参数正确。"""
        app = _FakeApp()
        calls = []
        with mock.patch.dict(sys.modules, _fake_modules(app, pythoncom=calls)):
            pdf = save_pdf_from_docx(self.docx)
        self.assertEqual(pdf, os.path.join(self.tmp.name, "会议纪要_20260917_1605.pdf"))
        self.assertTrue(os.path.exists(pdf))
        self.assertEqual(app.events[0], ("FlagAsMethod", "Quit"))   # WPS 的 Quit 坑
        open_args = [e for e in app.events if e[0] == "Open"][0]
        self.assertEqual(open_args[2], (False, True, False))        # ReadOnly=True, 不进最近
        self.assertFalse(app.Visible)
        self.assertEqual(app.DisplayAlerts, 0)
        self.assertIn(("SaveAs", pdf, 17), app.events)
        self.assertEqual(calls, ["CoInitialize", ("Dispatch", "Word.Application"), "CoUninitialize"])

    def test_com_initialize_paired_on_success_and_failure(self):
        """③ CoInitialize/CoUninitialize 成对（成功与异常路径都要）。"""
        for fail_on in (None, "Open", "SaveAs"):
            calls = []
            app = _FakeApp(fail_on=fail_on)
            with mock.patch.dict(sys.modules, _fake_modules(app, pythoncom=calls)):
                save_pdf_from_docx(self.docx)
            self.assertEqual(calls.count("CoInitialize"), 1, fail_on)
            self.assertEqual(calls.count("CoUninitialize"), 1, fail_on)
            self.assertEqual(calls[-1], "CoUninitialize", fail_on)

    def test_open_failure_returns_none(self):
        """② COM 报错：返回 None，不抛异常（主流程不受影响）。"""
        calls = []
        app = _FakeApp(fail_on="Open")
        with mock.patch.dict(sys.modules, _fake_modules(app, pythoncom=calls)), \
                self.assertLogs("会议记录", level="ERROR"):
            self.assertIsNone(save_pdf_from_docx(self.docx))

    def test_dispatch_failure_returns_none(self):
        calls = []
        mods = _fake_modules(dispatch_raises=RuntimeError("找不到 ProgID（模拟）"),
                             pythoncom=calls)
        with mock.patch.dict(sys.modules, mods), \
                self.assertLogs("会议记录", level="ERROR"):
            self.assertIsNone(save_pdf_from_docx(self.docx))
        self.assertEqual(calls, ["CoInitialize", ("Dispatch", "Word.Application"),
                                 "CoUninitialize"])

    def test_saveas_failure_no_file_returns_none(self):
        calls = []
        app = _FakeApp(fail_on="SaveAs")
        with mock.patch.dict(sys.modules, _fake_modules(app, pythoncom=calls)), \
                self.assertLogs("会议记录", level="ERROR"):
            self.assertIsNone(save_pdf_from_docx(self.docx))

    def test_quit_failure_ignored(self):
        """⑤ Quit 报错也要算成功（PDF 已经出来了）。"""
        app = _FakeApp(quit_raises=True)
        with mock.patch.dict(sys.modules, _fake_modules(app)):
            pdf = save_pdf_from_docx(self.docx)
        self.assertTrue(pdf and os.path.exists(pdf))

    def test_empty_pdf_returns_none(self):
        app = _FakeApp()
        with mock.patch.dict(sys.modules, _fake_modules(app)):
            with mock.patch.object(_FakeDoc, "SaveAs", lambda self, p, f: open(p, "wb").close()), \
                    self.assertLogs("会议记录", level="WARNING"):
                self.assertIsNone(save_pdf_from_docx(self.docx))

    def test_invalid_pdf_file_is_removed(self):
        """C2-4②：0 字节的无效 PDF 不能留在输出目录里（best-effort os.remove）。"""
        app = _FakeApp()
        pdf = os.path.join(self.tmp.name, "会议纪要_20260917_1605.pdf")
        with mock.patch.dict(sys.modules, _fake_modules(app)), \
                mock.patch.object(_FakeDoc, "SaveAs", lambda self, p, f: open(p, "wb").close()), \
                mock.patch.object(docx_writer.os, "remove", wraps=os.remove) as rm, \
                self.assertLogs("会议记录", level="WARNING"):
            self.assertIsNone(save_pdf_from_docx(self.docx))
        rm.assert_called_once_with(pdf)              # 确实去删了那个空文件
        self.assertFalse(os.path.exists(pdf))

    def test_close_failure_is_logged(self):
        """C2-4①：Close 失败不再完全静默（记 warning，PDF 仍算成功）。"""
        app = _FakeApp()
        with mock.patch.dict(sys.modules, _fake_modules(app)), \
                mock.patch.object(_FakeDoc, "Close", side_effect=RuntimeError("模拟 Close 失败")), \
                self.assertLogs("会议记录", level="WARNING") as cm:
            pdf = save_pdf_from_docx(self.docx)
        self.assertTrue(pdf and os.path.exists(pdf))
        self.assertIn("关闭文档失败", "\n".join(cm.output))

    def test_com_hang_times_out(self):
        """C2-2：COM 永久阻塞（WPS 弹窗/RPC 卡死）时最多等 PDF_TIMEOUT_SEC 就返回 None。"""
        app = _FakeApp(hang=True)
        with mock.patch.dict(sys.modules, _fake_modules(app)), \
                mock.patch.object(docx_writer, "PDF_TIMEOUT_SEC", 0.3), \
                self.assertLogs("会议记录", level="WARNING") as cm:
            started = time.monotonic()
            self.assertIsNone(save_pdf_from_docx(self.docx))
            elapsed = time.monotonic() - started
        self.assertLess(elapsed, 5)
        self.assertGreaterEqual(elapsed, 0.3)
        self.assertIn("放弃等待", "\n".join(cm.output))

    def test_missing_pywin32_returns_none(self):
        """④ pywin32 没装（win32com 为 None）→ 跳过、返回 None、不抛。"""
        with mock.patch.dict(sys.modules, {"win32com.client": None}), \
                self.assertLogs("会议记录", level="WARNING") as cm:
            self.assertIsNone(save_pdf_from_docx(self.docx))
        self.assertIn("pywin32", "\n".join(cm.output))

    def test_docx_untouched(self):
        app = _FakeApp()
        with mock.patch.dict(sys.modules, _fake_modules(app)):
            save_pdf_from_docx(self.docx)
        with open(self.docx, "rb") as f:
            self.assertEqual(f.read(), b"fake docx")

    def test_explicit_pdf_path(self):
        app = _FakeApp()
        target = os.path.join(self.tmp.name, "自定义.pdf")
        with mock.patch.dict(sys.modules, _fake_modules(app)):
            self.assertEqual(save_pdf_from_docx(self.docx, target), target)


class PipelinePdfReportTest(unittest.TestCase):
    """⑦ pipeline 两条路径：report 带 pdf_path；PDF 出问题也不影响出稿。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = {"app": {"output_dir": self.tmp.name}}
        self.utts = [{"speaker": "0", "text": "我是张三", "start_time": 0},
                     {"speaker": "0", "text": "定个事", "start_time": 5000}]

    def tearDown(self):
        self.tmp.cleanup()

    def test_streaming_report_has_pdf_path(self):
        with mock.patch.object(pipeline, "generate_minutes", return_value="# 智能纪要"), \
                mock.patch.object(pipeline, "save_minutes_pair",
                                  return_value=("a.docx", "a.md")), \
                mock.patch.object(pipeline, "save_pdf_from_docx",
                                  return_value="a.pdf"):
            docx, report = pipeline.run_pipeline_streaming(self.utts, cfg=self.cfg)
        self.assertEqual(report["pdf_path"], "a.pdf")
        self.assertEqual(docx, "a.docx")

    def test_file_mode_report_has_pdf_path(self):
        fake_client = mock.Mock()
        with mock.patch.object(pipeline, "AsrClient", return_value=fake_client), \
                mock.patch.object(pipeline, "parse_utterances", return_value=self.utts), \
                mock.patch.object(pipeline, "generate_minutes", return_value="# 智能纪要"), \
                mock.patch.object(pipeline, "save_minutes_pair",
                                  return_value=("b.docx", "b.md")), \
                mock.patch.object(pipeline, "save_pdf_from_docx",
                                  return_value="b.pdf"), \
                mock.patch.object(pipeline, "_out_dir", return_value=self.tmp.name):
            docx, report = pipeline.run_pipeline(wav_path="x.wav", cfg=self.cfg)
        self.assertEqual(report["pdf_path"], "b.pdf")
        fake_client.transcribe.assert_called_once()

    def test_pdf_failure_does_not_break_output(self):
        """红线：PDF 转换抛异常也必须照常出稿（docx/md 已生成）。"""
        with mock.patch.object(pipeline, "generate_minutes", return_value="# 智能纪要"), \
                mock.patch.object(pipeline, "save_minutes_pair",
                                  return_value=("c.docx", "c.md")), \
                mock.patch.object(pipeline, "save_pdf_from_docx",
                                  side_effect=RuntimeError("模拟 COM 崩了")), \
                self.assertLogs("会议记录", level="ERROR"):
            docx, report = pipeline.run_pipeline_streaming(self.utts, cfg=self.cfg)
        self.assertEqual(docx, "c.docx")
        self.assertIsNone(report["pdf_path"])
        self.assertEqual(report["md_path"], "c.md")

    def test_pdf_none_recorded(self):
        with mock.patch.object(pipeline, "generate_minutes", return_value="# 智能纪要"), \
                mock.patch.object(pipeline, "save_minutes_pair",
                                  return_value=("d.docx", "d.md")), \
                mock.patch.object(pipeline, "save_pdf_from_docx", return_value=None):
            _, report = pipeline.run_pipeline_streaming(self.utts, cfg=self.cfg)
        self.assertIsNone(report["pdf_path"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
