# -*- coding: utf-8 -*-
"""单实例 + 僵尸回收单测（全部走可注入依赖，不真开窗口、不真杀进程）。

覆盖：① pid 文件读写/删除（缺失、乱内容、0/负数都当没有旧实例）；② 判定纯函数
existing_instance_action 三分支真值表；③ _handle_previous_instance 的正常启动 /
提前台 / 僵尸回收三条路径（find_window / focus_window / terminate_process 注入假
实现）；④ 真进程探测（自己的 pid 活着、已退出进程的 pid 不活）；⑤ main() 在
「已有窗口」时直接退出 0、启动异常时弹中文错误框并返回 1、启动成功写 pid、
_on_close 删 pid；⑥ **僵尸回收前的映像名核实（防 pid 复用误杀）**——是我们自己
（会议记录.exe / python.exe / pythonw.exe）才杀，别的程序或查不到一律不杀。
"""
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import 会议记录
from 会议记录 import (
    ACTION_FOCUS, ACTION_KILL, ACTION_NONE, PID_FILE_NAME,
    _handle_previous_instance, _pid_file_path, _process_alive,
    _process_image_basename, _read_pid, _remove_pid, _write_pid,
    existing_instance_action)


class PidFileTest(unittest.TestCase):
    """① pid 文件读写：正常回读、缺失/乱内容返回 None、删除幂等。"""

    def test_write_read_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, PID_FILE_NAME)
            _write_pid(path, 4321)
            self.assertEqual(_read_pid(path), 4321)
            with open(path, encoding="utf-8") as f:
                self.assertEqual(f.read().strip(), "4321")

    def test_write_default_is_own_pid(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, PID_FILE_NAME)
            _write_pid(path)
            self.assertEqual(_read_pid(path), os.getpid())

    def test_missing_or_garbage_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, PID_FILE_NAME)
            self.assertIsNone(_read_pid(path))  # 文件不存在
            for text in ("", "  \n", "abc", "0", "-5", "12ab"):
                with open(path, "w", encoding="utf-8") as f:
                    f.write(text)
                self.assertIsNone(_read_pid(path), text)

    def test_remove_pid_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, PID_FILE_NAME)
            _write_pid(path, 123)
            _remove_pid(path)
            self.assertFalse(os.path.exists(path))
            _remove_pid(path)  # 再删一次不报错

    def test_pid_path_is_under_base_dir(self):
        self.assertEqual(_pid_file_path(), os.path.join(会议记录.BASE_DIR, PID_FILE_NAME))
        self.assertEqual(_pid_file_path(r"D:\别的目录"),
                         os.path.join(r"D:\别的目录", PID_FILE_NAME))


class ActionJudgementTest(unittest.TestCase):
    """② 判定纯函数：进程死 → 正常启动；活着有窗口 → 提前台；活着没窗口 → 僵尸回收。"""

    def test_truth_table(self):
        self.assertEqual(existing_instance_action(pid_alive=False, window_found=False),
                         ACTION_NONE)
        self.assertEqual(existing_instance_action(pid_alive=False, window_found=True),
                         ACTION_NONE)
        self.assertEqual(existing_instance_action(pid_alive=True, window_found=True),
                         ACTION_FOCUS)
        self.assertEqual(existing_instance_action(pid_alive=True, window_found=False),
                         ACTION_KILL)


class HandlePreviousInstanceTest(unittest.TestCase):
    """③ 守卫三分支：依赖全部注入，窗口/进程都是假实现。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.pid_path = os.path.join(self.tmp.name, PID_FILE_NAME)

    def tearDown(self):
        self.tmp.cleanup()

    def _handle(self, **kw):
        calls = {"focus": [], "kill": [], "image": []}
        defaults = dict(
            process_alive=lambda pid: True,          # 默认旧进程活着
            find_window=lambda: 1001,                # 默认找得到窗口
            focus_window=lambda hwnd: calls["focus"].append(hwnd),
            terminate_process=lambda pid: calls["kill"].append(pid) or True,
            image_basename=lambda pid: (calls["image"].append(pid),
                                        "会议记录.exe")[1],  # 默认是我们自己
        )
        defaults.update(kw)
        action = _handle_previous_instance(self.pid_path, **defaults)
        return action, calls

    def assertCalls(self, calls, focus=(), kill=(), image=()):
        self.assertEqual(calls["focus"], list(focus))
        self.assertEqual(calls["kill"], list(kill))
        self.assertEqual(calls["image"], list(image))

    def test_no_pid_file_starts_normally(self):
        """pid 文件不存在 → 正常启动，不找窗口、不杀进程。"""
        action, calls = self._handle(find_window=lambda: self.fail("不该找窗口"))
        self.assertEqual(action, ACTION_NONE)
        self.assertCalls(calls)

    def test_dead_pid_starts_normally(self):
        """pid 文件在但进程已退出（上次被 taskkill / 正常关掉）→ 正常启动。"""
        _write_pid(self.pid_path, 999999)
        action, calls = self._handle(
            process_alive=lambda pid: self.assertEqual(pid, 999999) or False,
            find_window=lambda: self.fail("进程已死不该找窗口"))
        self.assertEqual(action, ACTION_NONE)
        self.assertCalls(calls)

    def test_live_pid_with_window_focuses(self):
        """旧进程活着且有窗口 → 提前台，不杀进程、不查映像名（真·重复双击）。"""
        _write_pid(self.pid_path, 777)
        action, calls = self._handle()
        self.assertEqual(action, ACTION_FOCUS)
        self.assertCalls(calls, focus=[1001])

    def test_live_pid_without_window_is_killed(self):
        """僵尸且映像名是我们自己 → TerminateProcess 回收后继续启动。"""
        _write_pid(self.pid_path, 888)
        action, calls = self._handle(find_window=lambda: 0)
        self.assertEqual(action, ACTION_KILL)
        self.assertCalls(calls, kill=[888], image=[888])

    def test_own_image_variants_allowed(self):
        """允许回收的映像名：会议记录.exe / python.exe / pythonw.exe（大小写不敏感）。"""
        for name in ("会议记录.exe", "PYTHON.EXE", "pythonw.exe"):
            _write_pid(self.pid_path, 888)
            action, calls = self._handle(find_window=lambda: 0,
                                         image_basename=lambda pid, n=name: n)
            self.assertEqual(action, ACTION_KILL, name)
            self.assertCalls(calls, kill=[888])

    def test_other_program_image_is_not_killed(self):
        """僵尸但映像名是别的程序（pid 被复用）→ 不杀、ACTION_NONE、正常启动。"""
        _write_pid(self.pid_path, 888)
        seen = []
        with self.assertLogs("会议记录", level="WARNING") as cm:
            action, calls = self._handle(
                find_window=lambda: 0,
                image_basename=lambda pid: (seen.append(pid), "notepad.exe")[1])
        self.assertEqual(action, ACTION_NONE)
        self.assertCalls(calls)      # 查了身份，但没有 terminate 调用
        self.assertEqual(seen, [888])
        output = "\n".join(cm.output)
        self.assertIn("pid 被其他程序占用", output)
        self.assertIn("按无旧实例处理", output)

    def test_image_lookup_failure_is_not_killed(self):
        """映像名查不到（""，权限不足/进程已退出）→ 不杀、ACTION_NONE。"""
        _write_pid(self.pid_path, 888)
        seen = []
        with self.assertLogs("会议记录", level="WARNING") as cm:
            action, calls = self._handle(
                find_window=lambda: 0,
                image_basename=lambda pid: seen.append(pid) or "")
        self.assertEqual(action, ACTION_NONE)
        self.assertCalls(calls)
        self.assertEqual(seen, [888])
        self.assertIn("查询失败", "\n".join(cm.output))


class ProcessProbeTest(unittest.TestCase):
    """④ 真进程探测（Windows OpenProcess/WaitForSingleObject 真调用，不 mock）。"""

    def test_own_pid_alive(self):
        self.assertTrue(_process_alive(os.getpid()))

    def test_exited_process_not_alive(self):
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait()
        self.assertFalse(_process_alive(proc.pid))

    def test_own_image_basename(self):
        """真调用 QueryFullProcessImageNameW：自己 = 解释器文件名。"""
        self.assertEqual(_process_image_basename(os.getpid()).lower(),
                         os.path.basename(sys.executable).lower())

    def test_dead_pid_image_basename_empty(self):
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait()
        self.assertEqual(_process_image_basename(proc.pid), "")


class MainWiringTest(unittest.TestCase):
    """⑤ main() 接线：已有窗口 → 退出 0 不开界面；启动异常 → 弹框 + 返回 1。"""

    def test_focus_exits_without_opening_window(self):
        with mock.patch.object(会议记录, "setup_logging", return_value="fake.log"), \
                mock.patch.object(会议记录, "_pid_file_path", return_value="fake.pid"), \
                mock.patch.object(会议记录, "_handle_previous_instance",
                                  return_value=ACTION_FOCUS), \
                mock.patch.object(会议记录.tk, "Tk") as tk_mock:
            self.assertEqual(会议记录.main(), 0)
            tk_mock.assert_not_called()

    def test_startup_error_shows_box_and_returns_1(self):
        with mock.patch.object(会议记录, "setup_logging", return_value="fake.log"), \
                mock.patch.object(会议记录, "_pid_file_path", return_value="fake.pid"), \
                mock.patch.object(会议记录, "_handle_previous_instance",
                                  return_value=ACTION_NONE), \
                mock.patch.object(会议记录.tk, "Tk",
                                  side_effect=RuntimeError("没有显示环境")), \
                mock.patch.object(会议记录, "_startup_error_box") as box:
            self.assertEqual(会议记录.main(), 1)
        box.assert_called_once()
        self.assertEqual(box.call_args[0][0], "fake.log")   # 日志路径透传给弹框
        self.assertIn("没有显示环境", str(box.call_args[0][1]))

    def test_pid_written_on_success(self):
        """启动成功（界面建起来）写 pid 文件。"""
        written = []
        with mock.patch.object(会议记录, "setup_logging", return_value="fake.log"), \
                mock.patch.object(会议记录, "_pid_file_path", return_value="fake.pid"), \
                mock.patch.object(会议记录, "_handle_previous_instance",
                                  return_value=ACTION_NONE), \
                mock.patch.object(会议记录, "_write_pid",
                                  side_effect=lambda p: written.append(p)), \
                mock.patch.object(会议记录.tk, "Tk", return_value=mock.Mock()), \
                mock.patch.object(会议记录, "App", return_value=mock.Mock()):
            self.assertEqual(会议记录.main(), 0)
        self.assertEqual(written, ["fake.pid"])

    def _fake_app(self, recording=False, worker=None):
        """App.__new__ + 桩：这样 self._unregister_pid/_finish_session 还是真方法
        （用 mock.Mock 当 self 的话，方法名会被自动桩掉，测不到真逻辑）。"""
        app = 会议记录.App.__new__(会议记录.App)
        app.recorder = mock.Mock(recording=recording)
        app.session = None
        app.worker = worker
        app.root = mock.Mock()
        return app

    def test_on_close_removes_pid_file(self):
        """_on_close 正常退出时删 pid 文件（假 self，不开真窗口）。"""
        app = self._fake_app()
        with mock.patch.object(会议记录, "_pid_file_path", return_value="fake.pid"), \
                mock.patch.object(会议记录, "_read_pid",
                                  return_value=os.getpid()), \
                mock.patch.object(会议记录, "_remove_pid") as rm:
            会议记录.App._on_close(app)
        app.root.destroy.assert_called_once()
        rm.assert_called_once_with("fake.pid")

    def test_on_close_keeps_pid_file_of_another_process(self):
        """pid 文件已被第二个实例覆写 → 退出时不能误删别人的登记。"""
        app = self._fake_app()
        with mock.patch.object(会议记录, "_pid_file_path", return_value="fake.pid"), \
                mock.patch.object(会议记录, "_read_pid", return_value=999999), \
                mock.patch.object(会议记录, "_remove_pid") as rm:
            会议记录.App._on_close(app)
        app.root.destroy.assert_called_once()
        rm.assert_not_called()

    def test_on_close_asks_before_killing_finishing_worker(self):
        """出稿线程还活着 → 先确认；取消则不关窗、不收尾、不删 pid。"""
        worker = mock.Mock()
        worker.is_alive.return_value = True
        app = self._fake_app(worker=worker)
        with mock.patch.object(会议记录, "messagebox") as mb, \
                mock.patch.object(会议记录, "_pid_file_path", return_value="fake.pid"), \
                mock.patch.object(会议记录, "_read_pid",
                                  return_value=os.getpid()), \
                mock.patch.object(会议记录, "_remove_pid") as rm:
            mb.askyesno.return_value = False
            会议记录.App._on_close(app)
            app.root.destroy.assert_not_called()
            rm.assert_not_called()
            mb.askyesno.return_value = True
            会议记录.App._on_close(app)
        app.root.destroy.assert_called_once()
        rm.assert_called_once_with("fake.pid")


if __name__ == "__main__":
    unittest.main(verbosity=2)
