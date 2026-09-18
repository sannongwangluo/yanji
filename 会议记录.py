# -*- coding: utf-8 -*-
"""会议记录 —— 桌面小工具（试用版）。python 会议记录.py 启动。

主流程：点「开始录音」→ 录音 + 长连接流式识别边录边出字（主区域实时滚动
「姓名/说话人N：文本」）→ 散会点「结束并生成纪要」→ 已收集的转写稿直接给
DeepSeek → docx。识别不再等散会后上传，散会后只需等纪要生成。

线程模型（五线互不阻塞）：
  1. tkinter 主线程：只管界面。after() 每 200ms 轮询 电平/转写/进度 三个队列；
  2. PortAudio 回调线程（sounddevice 内部）：录音写盘 + 推电平 + feed 流式会话；
  3. 流式 reader 线程（MeetingStreamSession 内部）：读服务端响应、收 definite
     分句、断线自动重连；
  4. 流式 sender 线程（MeetingStreamSession 内部）：从音频缓冲队列取帧发给服务端；
  5. 管线线程：散会后单开工作线程跑 流式收尾→报到映射→纪要→docx，进度经
     progress_q 传回主线程——工作线程里绝不直接碰 tkinter；转写回调只把
     utterance 塞队列，报到映射在主线程里做（IncrementalSpeakerMap）。

错误处理约定：给用户的中文提示直接展示；完整栈 trace 只写日志文件（日志/ 目录）。

单实例（纯 ctypes，无第三方依赖）：BASE_DIR 下「会议记录.pid」登记自己的 pid；
再次双击时把已有窗口提前台并退出（不产生第二个实例）；pid 还活着但窗口没了
（上次崩溃留的僵尸）则先核实映像名（防 pid 复用误杀无关程序）再 TerminateProcess
回收，回收后正常启动。main() 全程 try/except，启动异常弹中文错误框（含日志路径），
绝不静默。
"""
import ctypes
import logging
import os
import queue
import re
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from config_loader import (
    ConfigError, app_base_dir, load_config, save_config_value, save_output_dir)
from digest import RollingDigest
from hotwords import (HOTWORDS_MAX_TOKENS, hotwords_tokens, limit_message,
                      load_hotwords, parse_hotwords, save_hotwords)
from minutes_llm import generate_digest, meeting_base_ms

BASE_DIR = app_base_dir()
sys.path.insert(0, BASE_DIR)
from pipeline import STATE_TEXT, _out_dir, run_pipeline_streaming, setup_logging
from recorder import Recorder, RecorderError
from speaker_map import IncrementalSpeakerMap
from streaming_asr import MeetingStreamSession

log = logging.getLogger("会议记录")

WINDOW_TITLE = "会议记录（试用版）"

PID_FILE_NAME = "会议记录.pid"

ACTION_NONE = "none"    # 没有活着的旧实例：正常启动
ACTION_FOCUS = "focus"  # 旧实例活着且有窗口：把它提到前台，本进程退出
ACTION_KILL = "kill"    # 旧进程活着但没窗口（僵尸）：杀掉后继续正常启动

# Win32 常量与权限位（OpenProcess / ShowWindow 用）
_SW_RESTORE = 9
_SYNCHRONIZE = 0x00100000
_PROCESS_TERMINATE = 0x0001
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_WAIT_TIMEOUT = 0x00000102
_IS_WINDOWS = os.name == "nt"

# 允许被回收的映像名（小写比较）：冻结态是 exe 自己，源码方式跑是 python/pythonw。
# 僵尸回收前必须核实这个——pid 会被系统复用，只认 pid 就会误杀无辜进程。
_OWN_IMAGE_NAMES = frozenset({"会议记录.exe", "python.exe", "pythonw.exe"})

_IMAGE_PATH_BUF_CHARS = 32768  # 长路径兜底（MAX_PATH 260 不够）


def _kernel32():
    """kernel32 句柄相关函数的原型声明（每次调用重设，幂等）。

    HANDLE 在 64 位下是指针：不声明 argtypes/restype，ctypes 默认按 c_int 传参和
    接收，句柄会被截断——CloseHandle 关错句柄、TerminateProcess 杀错进程都比
    「句柄泄漏」严重得多，所以这里统一显式声明。
    """
    k = ctypes.windll.kernel32
    k.OpenProcess.restype = ctypes.c_void_p
    k.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    k.CloseHandle.restype = ctypes.c_int
    k.CloseHandle.argtypes = [ctypes.c_void_p]
    k.WaitForSingleObject.restype = ctypes.c_uint32
    k.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    k.TerminateProcess.restype = ctypes.c_int
    k.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    k.QueryFullProcessImageNameW.restype = ctypes.c_int
    k.QueryFullProcessImageNameW.argtypes = [
        ctypes.c_void_p, ctypes.c_uint32, ctypes.c_wchar_p,
        ctypes.POINTER(ctypes.c_uint32)]
    return k


def _pid_file_path(base_dir=None):
    """pid 文件路径：BASE_DIR 下「会议记录.pid」（冻结态 = exe 旁）。"""
    return os.path.join(base_dir or BASE_DIR, PID_FILE_NAME)


def _read_pid(path):
    """读 pid 文件里的 pid；文件缺失/内容不是正整数 → None（当作没有旧实例）。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read().strip()
    except OSError:
        return None
    try:
        pid = int(text)
    except ValueError:
        return None
    return pid if pid > 0 else None


def _write_pid(path, pid=None):
    """写入自己的 pid（默认当前进程）。"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(str(pid if pid is not None else os.getpid()))


def _remove_pid(path):
    """删 pid 文件；不存在或删不掉都忽略（正常退出时调）。"""
    try:
        os.remove(path)
    except OSError:
        pass


def _process_alive(pid):
    """OpenProcess + WaitForSingleObject 探测 pid 还活着没（纯 ctypes）。

    SYNCHRONIZE 权限就够等待；进程已退出 / pid 不存在 → OpenProcess 返回 0，
    或句柄已受信（WaitForSingleObject 立即返回 WAIT_OBJECT_0），都算不活着。
    """
    if not _IS_WINDOWS:
        return False
    kernel32 = _kernel32()
    handle = kernel32.OpenProcess(_SYNCHRONIZE, False, pid)
    if not handle:
        return False
    try:
        return kernel32.WaitForSingleObject(handle, 0) == _WAIT_TIMEOUT
    finally:
        kernel32.CloseHandle(handle)


def _terminate_process(pid):
    """僵尸实例回收：TerminateProcess 强杀。返回是否成功。

    调用方（_handle_previous_instance）必须先核实映像名，确认是自己人再调这里。
    """
    if not _IS_WINDOWS:
        return False
    kernel32 = _kernel32()
    handle = kernel32.OpenProcess(_PROCESS_TERMINATE, False, pid)
    if not handle:
        return False
    try:
        return bool(kernel32.TerminateProcess(handle, 1))
    finally:
        kernel32.CloseHandle(handle)


def _process_image_basename(pid):
    """取进程映像文件名的 basename（如 "会议记录.exe" / "notepad.exe"）。

    QueryFullProcessImageNameW 拿完整路径后只留文件名——给僵尸回收做身份核实用：
    pid 会被系统复用，光凭「pid 还活着」不足以断定它是我们自己的进程。
    取不到（权限不足/进程已退出/非 Windows）一律返回 ""，调用方按「查不到就不杀」处理。
    """
    if not _IS_WINDOWS:
        return ""
    kernel32 = _kernel32()
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        size = ctypes.c_uint32(_IMAGE_PATH_BUF_CHARS)
        buf = ctypes.create_unicode_buffer(_IMAGE_PATH_BUF_CHARS)
        if not kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return ""
        return os.path.basename(buf.value)
    finally:
        kernel32.CloseHandle(handle)


def _find_main_window():
    """按 WINDOW_TITLE 找主窗口句柄；找不到返回 0。

    HWND 是 64 位指针，必须显式声明 restype——ctypes 默认 c_int 会截断句柄，
    截断后的假句柄 SetForegroundWindow 会静默失败。
    """
    if not _IS_WINDOWS:
        return 0
    find = ctypes.windll.user32.FindWindowW
    find.restype = ctypes.c_void_p
    find.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p]
    return find(None, WINDOW_TITLE) or 0


def _focus_main_window(hwnd):
    """把已有窗口还原（SW_RESTORE，可能最小化了）并提到前台。"""
    if not _IS_WINDOWS:
        return
    user32 = ctypes.windll.user32
    user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
    user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
    user32.ShowWindow(hwnd, _SW_RESTORE)
    user32.SetForegroundWindow(hwnd)


def existing_instance_action(pid_alive, window_found):
    """纯判定：旧实例状态 → 处理动作（三分支的唯一来源，便于单测）。

    pid 已死 → 正常启动；pid 活着且有窗口 → 提前台；活着但没窗口 → 僵尸，回收。
    """
    if not pid_alive:
        return ACTION_NONE
    return ACTION_FOCUS if window_found else ACTION_KILL


def _handle_previous_instance(pid_path=None, *, read_pid=_read_pid,
                              process_alive=_process_alive,
                              find_window=_find_main_window,
                              focus_window=_focus_main_window,
                              terminate_process=_terminate_process,
                              image_basename=_process_image_basename):
    """启动时的单实例守卫，返回 ACTION_*（依赖可注入，便于单测）。

    读 BASE_DIR 下「会议记录.pid」→ 旧进程还活着就找它的窗口：
    - 有窗口 = 真的已经开着一个（朋友双击了两次），提到前台，调用方拿到
      ACTION_FOCUS 后直接退出，不再开第二个实例；
    - 没窗口 = 可能是上次崩溃留下的僵尸进程（windowed exe 崩了没人收尸，pid 文件
      还在），**核实映像名确认是自己人之后**才 TerminateProcess 回收，然后继续正常
      启动——不然新进程会以为「已经有实例」而默默退走，表现就是「双击打不开」。
    - 没窗口 + 映像名不是自己人 = pid 被系统复用给了别的程序（崩溃后隔了很久/重启过），
      按无旧实例处理、正常启动、稍后覆写 pid 文件，**绝不杀无辜进程**。
    pid 文件缺失、内容不合法、进程已退出 → 一律正常启动，不阻塞。
    """
    path = pid_path or _pid_file_path()
    pid = read_pid(path)
    if pid is None:
        return ACTION_NONE
    if not process_alive(pid):
        log.info("[单实例] 上次记录的 pid %s 已退出，正常启动", pid)
        return ACTION_NONE
    hwnd = find_window()
    action = existing_instance_action(True, bool(hwnd))
    if action == ACTION_FOCUS:
        focus_window(hwnd)
        log.info("[单实例] 已有实例在运行（pid %s），已把窗口提到前台", pid)
        return action
    # 杀之前核实身份：pid 会被系统复用，光凭「pid 还活着」就 TerminateProcess
    # 有误杀无辜进程的风险（blast radius 不可接受）。映像名查不到也按不杀处理。
    image = image_basename(pid)
    if image.lower() not in _OWN_IMAGE_NAMES:
        log.warning("[单实例] pid %s 还活着但没有窗口，映像名 %s 不是本程序"
                    "（pid 被其他程序占用），按无旧实例处理、不杀",
                    pid, image or "(查询失败)")
        return ACTION_NONE
    log.warning("[单实例] pid %s 还活着但没有窗口（僵尸，映像名 %s），强制结束它",
                pid, image)
    terminate_process(pid)
    return action


def _startup_error_box(log_path, exc):
    """启动失败兜底：弹一个中文错误框（含日志路径），保证「打不开」不静默。

    windowed exe 没有控制台，异常只写日志的话用户只会看到「双击没反应」。
    """
    where = (f"\n\n详细错误已写进日志：\n{log_path}\n请把这个日志文件发给开发者。"
             if log_path else "\n\n（连日志文件都没能创建，请把程序目录是否可写告诉开发者）")
    try:
        root = tk.Tk()
        root.withdraw()  # messagebox 自带窗口，隐藏这个空壳
        messagebox.showerror("会议记录启动失败", f"会议记录启动失败：\n{exc}{where}")
        root.destroy()
    except Exception:
        log.exception("[启动] 错误提示框也没弹出来")


class App:
    def __init__(self, root):
        self.root = root
        self.cfg = load_config()
        self.recorder = Recorder()
        self.progress_q = queue.Queue()
        self.utter_q = queue.Queue()   # 流式 definite 分句（reader 线程 → 主线程）
        self.worker = None
        self.session = None            # 流式会话（MeetingStreamSession）
        self.speaker_map = None        # 增量报到映射（IncrementalSpeakerMap）
        self.stream_connect_failed = False  # 流式首连失败（录音继续，散会提示备用路径）
        self.rec_duration = 0.0        # 本次录音时长
        self.rec_path = None
        self.meeting_time = None       # 点「开始录音」的时刻（写进纪要「会议时间」）
        self.hotwords = load_hotwords()      # 热词表（开会时重读一次再注入）
        self.utterances = []           # 主线程已收到的（带姓名）分句，滚动摘要的数据源
        self.rolling = RollingDigest(os.path.join(BASE_DIR, "日志"))
        self._error_box_shown = False   # 回调异常提示框的重入保护
        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        # tkinter 默认把回调异常打到 stderr：windowed exe 没控制台 = 静默消失。
        self.root.report_callback_exception = self._report_callback_exception
        self.root.after(200, self._poll_queues)

    def _report_callback_exception(self, exc_type, exc_value, exc_tb):
        """Tk 回调里漏出来的异常：写日志 + 弹中文框，绝不静默。

        弹窗自己也在 Tk 回调里跑（模态嵌套事件循环），所以失败要兜住并防重入。
        """
        log.error("[界面] 回调异常未捕获", exc_info=(exc_type, exc_value, exc_tb))
        if self._error_box_shown:
            return
        self._error_box_shown = True
        try:
            messagebox.showerror(
                "出错了",
                f"界面操作出错：{exc_value}\n\n"
                "程序会尽量继续运行；详细信息已写进「日志」文件夹。")
        except Exception:
            log.exception("[界面] 错误提示框也没弹出来")
        finally:
            self._error_box_shown = False

    # ---------------- 界面 ----------------
    def _build_ui(self):
        self.root.title(WINDOW_TITLE)
        self.root.geometry("920x780")
        self.root.minsize(760, 640)
        self.root.resizable(True, True)
        pad = {"padx": 18, "pady": 4}

        ttk.Label(self.root, text="会议记录",
                  font=("Microsoft YaHei UI", 18, "bold")).pack(pady=(14, 2))
        ttk.Label(
            self.root,
            text="开始录音后，请参会人依次说：我是+姓名（发言实时出字）",
            foreground="#666666",
            font=("Microsoft YaHei UI", 10),
        ).pack(**pad)

        btns = ttk.Frame(self.root)
        btns.pack(pady=10)
        self.btn_start = ttk.Button(btns, text="开始录音", width=16,
                                    command=self.on_start)
        self.btn_start.pack(side="left", padx=10)
        self.btn_stop = ttk.Button(btns, text="结束并生成纪要", width=16,
                                   command=self.on_stop, state="disabled")
        self.btn_stop.pack(side="left", padx=10)

        self.status = ttk.Label(
            self.root, text="待机：点「开始录音」，散会时点「结束并生成纪要」",
            font=("Microsoft YaHei UI", 11, "bold"), foreground="#222222")
        self.status.pack(**pad)
        self.elapsed = ttk.Label(self.root, text="", foreground="#888888")
        self.elapsed.pack()
        self.level = ttk.Progressbar(self.root, maximum=100, length=420)
        self.level.pack(pady=(6, 2))
        ttk.Label(self.root, text="（横条是麦克风音量，说话时应该有跳动）",
                  foreground="#999999").pack()

        # 纪要保存目录（GUI 可改，持久化到 config.toml [app] output_dir）
        out_row = ttk.Frame(self.root)
        out_row.pack(pady=(6, 0))
        self.out_dir_label = ttk.Label(out_row, text="", foreground="#666666",
                                       font=("Microsoft YaHei UI", 9))
        self.out_dir_label.pack(side="left", padx=(0, 8))
        ttk.Button(out_row, text="更改…", width=8,
                   command=self._choose_out_dir).pack(side="left", padx=(0, 6))
        ttk.Button(out_row, text="设置…", width=8,
                   command=self._open_settings).pack(side="left")
        self._refresh_out_dir_label()

        # 实时转写区（只读滚动文本）
        box = ttk.Frame(self.root)
        box.pack(fill="both", expand=True, padx=18, pady=(6, 12))
        self.transcript = tk.Text(
            box, state="disabled", wrap="word", relief="flat",
            font=("Microsoft YaHei UI", 11), background="#fbfbf6",
            foreground="#1a1a1a", padx=10, pady=8)
        scroll = ttk.Scrollbar(box, command=self.transcript.yview)
        self.transcript.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.transcript.pack(side="left", fill="both", expand=True)
        self._reset_transcript()

        self._tick_job = None

    def _refresh_out_dir_label(self):
        """刷新「纪要保存到：<当前路径>」显示（单一来源 pipeline._out_dir）。"""
        self.out_dir_label.config(text=f"纪要保存到：{_out_dir(self.cfg)}")

    def _choose_out_dir(self):
        """弹目录选择框：可写性检查通过才生效，并持久化写回 config.toml。"""
        chosen = filedialog.askdirectory(
            initialdir=_out_dir(self.cfg), title="选择纪要保存目录")
        if not chosen:
            return  # 用户取消
        # 可写性检查：试写一个临时文件再删掉
        probe = os.path.join(chosen, f".outdir_probe_{int(time.time() * 1000)}.tmp")
        try:
            with open(probe, "w", encoding="utf-8") as f:
                f.write("ok")
            os.remove(probe)
        except Exception as e:
            log.exception("[输出目录] %s 不可写：%s", chosen, e)
            messagebox.showwarning("目录不可写", "这个目录没有写入权限，请换一个。")
            return
        # 持久化写回 config.toml（tomllib 只读，save_output_dir 手写回写）
        try:
            save_output_dir(chosen)
        except Exception as e:
            log.exception("[输出目录] 写 config.toml 失败")
            messagebox.showerror("保存失败", f"写入 config.toml 失败：\n{e}")
            return
        self.cfg["app"]["output_dir"] = chosen
        self._refresh_out_dir_label()
        log.info("[输出目录] 纪要保存目录改为 %s", chosen)

    def _open_settings(self):
        """设置窗：两个 API Key（写回 config.toml）+ 热词表（写 hotwords.txt）。"""
        win = tk.Toplevel(self.root)
        win.title("设置 —— API Key 与热词")
        win.geometry("620x600")
        win.resizable(False, False)
        win.transient(self.root)
        win.grab_set()
        pad = {"padx": 14, "pady": 5}

        ttk.Label(win, text="API Key 设置（保存后写回 config.toml，下次启动生效）",
                  font=("Microsoft YaHei UI", 10, "bold")).pack(pady=(12, 4))

        ttk.Label(win, text="豆包语音 API Key（UUID 格式）").pack(anchor="w", **pad)
        volc_entry = ttk.Entry(win, show="*", width=62)
        volc_entry.pack(**pad)
        volc_entry.insert(0, self.cfg["volc"]["api_key"])

        ttk.Label(win, text="DeepSeek API Key（sk- 开头）").pack(anchor="w", **pad)
        ds_entry = ttk.Entry(win, show="*", width=62)
        ds_entry.pack(**pad)
        ds_entry.insert(0, self.cfg["deepseek"]["api_key"])

        show_var = tk.BooleanVar(value=False)

        def _toggle_show():
            show_char = "" if show_var.get() else "*"
            volc_entry.config(show=show_char)
            ds_entry.config(show=show_char)

        ttk.Checkbutton(win, text="显示明文", variable=show_var,
                        command=_toggle_show).pack(anchor="w", **pad)

        # 热词（一行一词，写 hotwords.txt；开会建连时注入 corpus.context）
        ttk.Label(win, text="热词（专有名词，一行一个词）",
                  font=("Microsoft YaHei UI", 10, "bold")).pack(anchor="w", padx=14,
                                                                pady=(12, 0))
        ttk.Label(win, text="人名、产品名、行话写这里，识别时优先按这些写法出字；"
                            "填太多会被服务器拒，建议只放最容易被听错的词。",
                  foreground="#666666", wraplength=580,
                  font=("Microsoft YaHei UI", 9)).pack(anchor="w", padx=14)
        hot_text = tk.Text(win, height=10, width=62, wrap="none",
                           font=("Microsoft YaHei UI", 10))
        hot_text.pack(padx=14, pady=(4, 0))
        hot_text.insert("1.0", "\n".join(self.hotwords))
        self.hot_limit_label = ttk.Label(win, text="", foreground="#888888")
        self.hot_limit_label.pack(anchor="w", padx=14)

        def _refresh_limit(_event=None):
            words = parse_hotwords(hot_text.get("1.0", "end"))
            tokens = hotwords_tokens(words)
            over = tokens > HOTWORDS_MAX_TOKENS
            self.hot_limit_label.config(
                text=f"共 {len(words)} 个词，约 {tokens:.0f} tokens"
                     f"（上限 {HOTWORDS_MAX_TOKENS}）"
                     + ("　超上限，保存会被拦下，请删减" if over else ""),
                foreground="#c0392b" if over else "#888888")

        hot_text.bind("<KeyRelease>", _refresh_limit)
        hot_text.bind("<<Paste>>", lambda e: win.after(10, _refresh_limit))
        hot_text.bind("<<Cut>>", lambda e: win.after(10, _refresh_limit))
        _refresh_limit()

        btns = ttk.Frame(win)
        btns.pack(pady=(10, 12))
        ttk.Button(btns, text="保存", width=10,
                   command=lambda: self._save_settings(
                       win, volc_entry.get().strip(), ds_entry.get().strip(),
                       hot_text.get("1.0", "end"))
                   ).pack(side="left", padx=8)
        ttk.Button(btns, text="取消", width=10,
                   command=win.destroy).pack(side="left", padx=8)

    def _save_settings(self, win, volc_key, ds_key, hotwords_text=""):
        """保存设置窗内容：热词 token 上限校验 → 改过的 Key 写 config.toml + 热词写 hotwords.txt。

        日志绝不写 key 明文，只记长度；允许清空（清空 = 走环境变量兜底）。
        热词超上限直接拒存并提示（超限的 corpus.context 会被服务端拒掉整次识别）。
        只在输入框内容真的被改过时才写回该 Key：设置窗预填的是 cfg 里的值（可能来自
        环境变量兜底），用户只改热词点保存时不能把 Key 明文落进 config.toml。
        """
        words = parse_hotwords(hotwords_text)
        if hotwords_tokens(words) > HOTWORDS_MAX_TOKENS:
            log.warning("[设置] 热词超上限被拒存：%d 个词约 %.0f tokens",
                        len(words), hotwords_tokens(words))
            messagebox.showerror("热词太多", limit_message(words))
            return  # 不关窗、不保存：让用户先删词
        changed = {section: value
                   for section, value in (("volc", volc_key),
                                          ("deepseek", ds_key))
                   if value != (self.cfg[section]["api_key"] or "")}
        # 软校验：只对本次真要写回的 Key 确认格式（没改的不打扰用户）
        checks = []
        if changed.get("volc") and not re.match(
                r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$", volc_key):
            checks.append("豆包语音 Key 不像 UUID 格式")
        if changed.get("deepseek") and not ds_key.startswith("sk-"):
            checks.append("DeepSeek Key 不以 sk- 开头")
        if checks:
            if not messagebox.askyesno(
                    "格式确认", "；".join(checks) + "。\n格式看起来不对，确定保存吗？\n"
                    "（留空也可以保存，将改用环境变量里的 Key）"):
                return
        try:
            for section, value in changed.items():
                save_config_value(section, "api_key", value)
        except Exception as e:
            log.exception("[设置] 写 config.toml 失败")
            messagebox.showerror("保存失败", f"写入 config.toml 失败：\n{e}")
            return
        if not save_hotwords(words):
            log.warning("[设置] 写 hotwords.txt 失败")
            messagebox.showerror("保存失败", "热词没写进 hotwords.txt，"
                                            "请检查程序目录是否可写。")
            return
        self.cfg["volc"]["api_key"] = volc_key
        self.cfg["deepseek"]["api_key"] = ds_key
        self.hotwords = words
        win.destroy()
        log.info("[设置] 已写回 %s；热词 %d 个（约 %.0f tokens）",
                 "、".join(f"{s}.api_key（{len(v)} 字符）"
                           for s, v in changed.items()) or "（Key 未改动）",
                 len(words), hotwords_tokens(words))

    def _reset_transcript(self):
        """清空转写区，放回占位提示。"""
        self._got_text = False
        self.transcript.configure(state="normal")
        self.transcript.delete("1.0", "end")
        self.transcript.insert("end", "开始录音后，实时转写会显示在这里。")
        self.transcript.tag_add("hint", "1.0", "end")
        self.transcript.tag_config("hint", foreground="#999999")
        self.transcript.configure(state="disabled")

    def _append_utterance(self, speaker_name, text):
        """主线程里追加一行「姓名/说话人N：文本」（只读区临时放行插入）。

        智能跟随：插入前记视口底部位置，用户在底部（含首句替换占位提示）才
        自动滚到底；往上翻看历史时保持视口不动。Text 处于 disabled 不影响
        滚动条和滚轮，无需额外绑定。
        """
        if not self._got_text:
            self.transcript.configure(state="normal")
            self.transcript.delete("1.0", "end")
            self.transcript.configure(state="disabled")
            self._got_text = True
            at_bottom = True  # 首句替换占位提示，视同在底部
        else:
            at_bottom = self.transcript.yview()[1] >= 0.98
        self.transcript.configure(state="normal")
        self.transcript.insert("end", f"{speaker_name}：{text}\n")
        if at_bottom:
            self.transcript.see("end")
        self.transcript.configure(state="disabled")

    def _set_status(self, text, color="#222222"):
        self.status.config(text=text, foreground=color)

    # ---------------- 录音 + 流式识别 ----------------
    def on_start(self):
        """点「开始录音」。makedirs/wave.open 等任何失败都记日志 + 弹中文框，绝不静默。"""
        try:
            self._begin_recording()
        except RecorderError as e:
            log.warning("[录音] 开始失败：%s", e)
            self._recover_start_failure()
            messagebox.showerror("打不开麦克风", str(e))
        except Exception as e:
            log.exception("[录音] 开始录音失败")
            self._recover_start_failure()
            messagebox.showerror("开始录音失败", f"开始录音失败：\n{e}")

    def _recover_start_failure(self):
        """起录音失败后的复位：没录上就退回待机态，别把界面留在半启动状态。"""
        if self.recorder.recording:
            return   # 已经录上了（失败发生在后面的步骤）：按钮保持录音态
        self.session = None
        self._reset_buttons()

    def _begin_recording(self):
        stamp = time.strftime("%Y%m%d_%H%M")
        rec_dir = os.path.join(BASE_DIR, "录音")
        os.makedirs(rec_dir, exist_ok=True)
        self.rec_path = os.path.join(rec_dir, f"会议录音_{stamp}.wav")
        self.meeting_time = time.strftime("%Y-%m-%d %H:%M")   # 写进纪要「会议时间」
        self.hotwords = load_hotwords()   # 开会时重读一次（手改过热词文件不用重启）
        self.utterances = []
        # 新会议清场：上一场流式线程迟到的分句不能再混进来
        self._drain_queue(self.utter_q)
        self.rolling = RollingDigest(os.path.join(BASE_DIR, "日志"))
        self.session = MeetingStreamSession(self.cfg, hotwords=self.hotwords)
        self.speaker_map = IncrementalSpeakerMap()
        self.stream_connect_failed = False
        self._reset_transcript()
        self.recorder.start(self.rec_path, on_frame=self._on_audio_frame)
        log.info("[录音] 开始：%s（会议时间 %s，热词 %d 个）",
                 self.rec_path, self.meeting_time, len(self.hotwords))
        self.btn_start.config(state="disabled")
        self.btn_stop.config(state="normal")
        self._set_status("正在连接流式识别…", "#8e44ad")
        # 建连可能要几秒，放后台线程，不卡界面；录音照常进行
        threading.Thread(target=self._connect_stream, daemon=True).start()
        self._tick_job = self.root.after(500, self._tick)

    def _drain_queue(self, q):
        """丢掉队列里的残留消息（开会前清场，防上一场的迟到消息串场）。"""
        while True:
            try:
                q.get_nowait()
            except queue.Empty:
                return

    def _connect_stream(self):
        """后台线程建流式连接。结果经 progress_q 回主线程，消息里带上会话实例
        （主线程据此丢弃上一场会议迟到的结果）。"""
        session = self.session
        try:
            session.start(on_utterance=self.utter_q.put)
        except Exception as e:
            log.warning("[流式] 连接失败：%s", e)
            self.progress_q.put(("stream_error", (session, str(e))))
        else:
            self.progress_q.put(("stream_ok", (session, None)))

    def _on_audio_frame(self, frame):
        """PortAudio 回调线程里被调：喂流式会话。内部只入队，不阻塞。"""
        if self.session is not None:
            self.session.feed(frame)

    def _tick(self):
        """每 500ms 刷新「录音中 xx:xx」+ 状态行（连接状态 / 句数 / 已预写摘要），
        并检查要不要起一轮滚动摘要（判定在主线程，LLM 调用在工作线程）。

        重新排期放 finally：刷新里任何异常都不能把 500ms 心跳停掉（停了界面就
        永远停在旧状态，而且完全静默）。
        """
        try:
            recording = self.recorder.recording
        except Exception:
            log.exception("[界面] 读录音状态失败，停止刷新")
            return
        try:
            if recording:
                m, s = divmod(int(self.recorder.elapsed), 60)
                self.elapsed.config(text=f"录音中 {m:02d}:{s:02d}")
                if self.stream_connect_failed:
                    conn, color = "流式连接失败，识别不可用（录音继续）", "#c0392b"
                elif self.session is not None and self.session.started:
                    if self.session.connected:
                        conn, color = "流式已连接", "#222222"
                    elif self.session.failed:
                        conn, color = ("连接已断开，识别已停止（录音继续）", "#c0392b")
                    else:
                        conn, color = "连接中断，自动重连中…", "#c0392b"
                else:
                    conn, color = "正在连接流式识别…", "#8e44ad"
                digest_info = ""
                if self.rolling is not None and (self.rolling.count or self.rolling.busy):
                    digest_info = f" · 已预写 {self.rolling.count} 段摘要"
                self._set_status(
                    f"录音中 · {conn} · 已识别 "
                    f"{self.session.utterance_count if self.session else 0} 句"
                    f"{digest_info}", color)
                self._maybe_start_digest()
            else:
                self.elapsed.config(text="")
        except Exception:
            log.exception("[界面] 刷新录音状态出错（心跳继续）")
        finally:
            if recording:
                self._tick_job = self.root.after(500, self._tick)

    # ---------------- 滚动摘要（长会议后台预写） ----------------
    def _maybe_start_digest(self):
        """录音 ≥30 分钟后每 20 分钟预写一轮摘要；判定在主线程，LLM 在工作线程。"""
        rolling = self.rolling
        if rolling is None or self.session is None:
            return
        if not rolling.due(self.recorder.elapsed, len(self.utterances)):
            return
        snapshot = rolling.begin(self.utterances)
        if snapshot is None:
            return
        # 时间零点 = 整场会议第一句的时间戳：摘要里的 [mm:ss] 与最终纪要的
        # 「智能章节」时间戳必须同一套坐标，所以这里显式带下去。
        snapshot["base_ms"] = meeting_base_ms(self.utterances)
        log.info("[摘要] 开始第 %d 段预写（新转写 %d 句，已有摘要 %d 字）",
                 rolling.count + 1, len(snapshot["new"]), len(snapshot["prior"]))
        threading.Thread(target=self._digest_worker, args=(rolling, snapshot),
                         daemon=True).start()

    def _digest_worker(self, rolling, snapshot):
        """工作线程跑滚动摘要：绝不碰 tkinter，结果经 progress_q 回主线程。

        任何失败都只记日志 + 通知主线程复位——滚动摘要绝不能影响出稿主路径。
        回传时带上发起这轮的 RollingDigest 实例：主线程据此丢弃「上一场会议」的
        迟到结果（散会后马上开新会时可能发生）。
        """
        try:
            text = generate_digest(self.cfg, snapshot["prior"], snapshot["new"],
                                   base_start_ms=snapshot.get("base_ms"))
        except Exception as e:
            log.warning("[摘要] 预写失败（不影响出稿）：%s", e)
            self.progress_q.put(("digest_fail", (rolling, str(e))))
            return
        self.progress_q.put(("digest_ok", (rolling, text, snapshot["covered"])))

    def on_stop(self):
        try:
            self.rec_duration = self.recorder.stop()
        except RecorderError as e:
            # 收尾不能漏：流式会话/线程必须在这里关掉，否则残留连接活着，
            # 迟到的分句还会污染下一场会议。
            self._finish_session()
            self._reset_buttons()
            messagebox.showwarning("录音有问题", str(e))
            return
        self._cancel_job(self._tick_job)
        self._set_status("收尾中…（等最后一两句转写）", "#8e44ad")
        self.btn_start.config(state="disabled")
        self.btn_stop.config(state="disabled")
        self.worker = threading.Thread(target=self._worker_run, daemon=True)
        self.worker.start()

    def _finish_session(self):
        """收尾流式会话并摘掉引用（幂等，流式侧保证未 started 也安全）。"""
        session, self.session = self.session, None
        if session is None:
            return
        try:
            session.finish()
        except Exception:
            log.exception("[流式] 会话收尾失败（不阻断出稿）")

    # ---------------- 管线（后台线程） ----------------
    def _worker_run(self):
        """后台线程：流式收尾 → 收集好的 utterances → 映射 → 纪要 → docx。

        整个函数体包在 try 里：工作线程静默一死，界面就永远停在「收尾中」，
        所以宁可报一条中文错误，也不能一声不响地死掉。
        """
        try:
            self._run_pipeline()
        except Exception:
            log.exception("[管线] 工作线程异常")
            self.progress_q.put(("error", "生成失败，请查看项目「日志」文件夹里的最新日志。"))

    def _run_pipeline(self):
        """_worker_run 的正文：工作线程里跑，绝不碰 tkinter。"""
        def progress(state, detail):
            self.progress_q.put((state, detail))

        session = self.session
        utterances = []
        if session is not None and session.started:
            progress("finishing", "")
            session.finish()
            utterances = session.utterances
        rolling = self.rolling
        # 一次原子取回 (text, covered)：分两次读会被主线程的 commit() 插在中间，
        # 读到「旧 text + 新 covered」，那一段发言就从终稿里丢了。
        digest, digest_covered = (rolling.snapshot() if rolling is not None
                                  else ("", 0))
        try:
            docx_path, report = run_pipeline_streaming(
                utterances, progress=progress, cfg=self.cfg,
                meeting_time=self.meeting_time, hotwords=self.hotwords,
                digest=digest, digest_covered=digest_covered)
        except ConfigError as e:
            log.warning("[管线] 配置问题：%s", e)
            self.progress_q.put(("error", str(e)))
            return
        except RuntimeError as e:
            # 管线/DeepSeek 抛的指导性中文错误（如无转写内容、key 无效），直接透传给用户
            log.warning("[管线] 生成失败：%s", e)
            self.progress_q.put(("error", str(e)))
            return
        except Exception:
            log.exception("[管线] 出稿失败")
            self.progress_q.put(("error", "生成失败，请查看项目「日志」文件夹里的最新日志。"))
            return
        self.progress_q.put(("done", (docx_path, report["md_path"],
                                      report.get("pdf_path"),
                                      report.get("image_path"))))

    # ---------------- 队列轮询（主线程） ----------------
    def _poll_queues(self):
        """每 200ms 排空三个队列（电平 / 转写 / 进度）。

        整体包 try + finally 重新排期：任何一个回调抛异常（speaker_map.update、
        _on_progress…）都会让「末尾 after() 执行不到」的写法永久停摆且完全静默。
        """
        try:
            # 电平条
            try:
                while True:
                    rms = self.recorder.level_queue.get_nowait()
                    self.level.config(value=min(100, int(rms * 300)))
            except queue.Empty:
                pass
            # 流式 definite 分句（reader 线程塞的原始 utterance）→ 增量映射 → 显示
            try:
                while True:
                    u = self.utter_q.get_nowait()
                    named, _ = self.speaker_map.update(u)
                    self.utterances.append(named)   # 滚动摘要的数据源（主线程唯一写者）
                    self._append_utterance(named["speaker_name"], named["text"])
            except queue.Empty:
                pass
            # 管线进度/结果
            try:
                while True:
                    state, detail = self.progress_q.get_nowait()
                    self._on_progress(state, detail)
            except queue.Empty:
                pass
        except Exception:
            log.exception("[界面] 轮询队列时出错（已继续排期，界面不静默）")
        finally:
            self.root.after(200, self._poll_queues)

    def _on_progress(self, state, detail):
        if state in ("stream_ok", "stream_error"):
            # 消息里带会话实例：上一场会议迟到的结果（散会后马上开新会）必须丢弃
            session, reason = (detail if isinstance(detail, (tuple, list))
                               else (None, detail))[:2]
            if session is not None and session is not self.session:
                log.info("[流式] 丢弃上一场会议的连接结果：%s", reason or "")
                return
            if state == "stream_ok":
                log.info("[流式] 连接成功")
            else:
                self.stream_connect_failed = True
                log.warning("[流式] 连接失败，本次会议无实时转写：%s", reason)
            return
        if state == "digest_ok":
            rolling, text, covered = detail
            if rolling is not self.rolling:      # 上一场会议的迟到结果，丢弃
                log.info("[摘要] 丢弃上一场会议的预写结果")
                return
            path = self.rolling.commit(text, covered)
            log.info("[摘要] 第 %d 段摘要已更新（覆盖前 %d 句）：%s",
                     self.rolling.count, self.rolling.covered, path or "（落盘失败）")
            return
        if state == "digest_fail":
            rolling, reason = detail
            if rolling is not self.rolling:
                return
            rolling.fail(reason)   # 只复位，等下一轮；绝不影响出稿
            return
        if state == "done":
            # 管线自己在出稿末尾也会发一次 ("done", "")——那是过场事件，不是结果；
            # 只有工作线程塞的 4 元组 (docx, md, pdf, image) 才走完成逻辑。
            if not isinstance(detail, (tuple, list)) or len(detail) < 4:
                return
            self._set_status("完成", "#27ae60")
            self._reset_buttons()
            self._finish(detail)
            return
        if state == "error":
            self._set_status("出错", "#c0392b")
            self._reset_buttons()
            messagebox.showerror("生成失败", detail)
            return
        text = STATE_TEXT.get(state, state)
        if detail:
            text = f"{text}{detail}"
        self._set_status(text + "…", "#8e44ad")

    def _finish(self, result):
        """出稿成功：弹完成框（有信息图就补一行路径）+ 打开输出目录。

        result = (docx_path, md_path, pdf_path, image_path)（工作线程塞的四元组）。
        """
        docx_path, md_path, pdf_path, image_path = result
        out_dir = os.path.dirname(docx_path)
        if pdf_path:
            text = f"已生成会议纪要（Word + PDF + Markdown）：\n{docx_path}\n{pdf_path}\n{md_path}"
        else:
            text = (f"已生成会议纪要（Word + Markdown）：\n{docx_path}\n{md_path}\n\n"
                    "PDF 这次没生成：这台电脑上没找到 WPS 或 Word，装一个就自动有了。")
        if image_path:
            text += f"\n\n总结信息图：\n{image_path}"
        messagebox.showinfo("已完成", text)
        try:
            os.startfile(out_dir)  # Windows：打开输出文件夹
        except OSError:
            pass

    # ---------------- 收尾 ----------------
    def _reset_buttons(self):
        self.btn_start.config(state="normal")
        self.btn_stop.config(state="disabled")

    def _cancel_job(self, job):
        if job is not None:
            try:
                self.root.after_cancel(job)
            except Exception:
                pass

    def _on_close(self):
        if self.recorder.recording:
            if not messagebox.askyesno("还在录音", "会议还没结束，现在退出会丢掉这段录音。确定退出吗？"):
                return
            try:
                self.recorder.stop()
            except RecorderError:
                pass
        # 出稿线程还活着时直接关窗会把 daemon 线程砍掉，docx 可能写一半
        worker = getattr(self, "worker", None)
        if worker is not None and worker.is_alive():
            if not messagebox.askyesno(
                    "正在生成纪要",
                    "正在生成纪要，退出会中断出稿。确定退出吗？\n"
                    "（已经写出来的文件不会损坏）"):
                return
        self._finish_session()
        self.root.destroy()
        self._unregister_pid()

    def _unregister_pid(self):
        """正常退出：撤销单实例登记——只在登记的还是自己的 pid 时才删。

        pid 文件可能已经被第二个实例覆写了（上次异常退出没清干净时会这样），
        无条件删会把别人的登记删掉。
        """
        path = _pid_file_path()
        if _read_pid(path) != os.getpid():
            log.info("[单实例] pid 文件登记的不是本进程，退出时不删")
            return
        _remove_pid(path)


def main():
    """启动入口：单实例守卫 → 建界面。任何启动异常都弹中文错误框，不静默。"""
    log_path = None
    pid_path = _pid_file_path()
    wrote_pid = False
    try:
        log_path = setup_logging()
        log.info("=" * 40)
        log.info("[启动] 会议记录（试用版）")
        if _handle_previous_instance(pid_path) == ACTION_FOCUS:
            log.info("[启动] 已有窗口在运行，本次启动直接退出")
            return 0
        root = tk.Tk()
        App(root)
        _write_pid(pid_path)  # 界面建好才算启动成功，登记 pid 供下次单实例判定
        wrote_pid = True
        root.mainloop()
    except Exception as e:
        log.exception("[启动] 启动失败")
        if wrote_pid:
            _remove_pid(pid_path)
        _startup_error_box(log_path, e)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
