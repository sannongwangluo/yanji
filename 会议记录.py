# -*- coding: utf-8 -*-
"""会议记录 —— 中文会议记录桌面工具。python 会议记录.py 或 python yanji.py 启动。

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
"""
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

BASE_DIR = app_base_dir()
sys.path.insert(0, BASE_DIR)
from pipeline import STATE_TEXT, _out_dir, run_pipeline_streaming, setup_logging
from recorder import Recorder, RecorderError
from speaker_map import IncrementalSpeakerMap
from streaming_asr import MeetingStreamSession

log = logging.getLogger("会议记录")

WINDOW_TITLE = "YanJi 言纪 · 会议记录"


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
        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(200, self._poll_queues)

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
        except OSError as e:
            log.warning("[输出目录] %s 不可写：%s", chosen, e)
            messagebox.showwarning("目录不可写", "这个目录没有写入权限，请换一个。")
            return
        # 持久化写回 config.toml（tomllib 只读，save_output_dir 手写回写）
        try:
            save_output_dir(chosen)
        except OSError as e:
            log.exception("[输出目录] 写 config.toml 失败")
            messagebox.showerror("保存失败", f"写入 config.toml 失败：\n{e}")
            return
        self.cfg["app"]["output_dir"] = chosen
        self._refresh_out_dir_label()
        log.info("[输出目录] 纪要保存目录改为 %s", chosen)

    def _open_settings(self):
        """设置窗：查看/修改豆包语音、DeepSeek 两个 API Key（写回 config.toml）。"""
        win = tk.Toplevel(self.root)
        win.title("设置 —— API Key")
        win.geometry("540x250")
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

        btns = ttk.Frame(win)
        btns.pack(pady=(8, 12))
        ttk.Button(btns, text="保存", width=10,
                   command=lambda: self._save_settings(
                       win, volc_entry.get().strip(), ds_entry.get().strip())
                   ).pack(side="left", padx=8)
        ttk.Button(btns, text="取消", width=10,
                   command=win.destroy).pack(side="left", padx=8)

    def _save_settings(self, win, volc_key, ds_key):
        """保存设置窗里的两个 key：软校验 → 写回 config.toml → 更新内存 cfg。

        日志绝不写 key 明文，只记长度；允许清空（清空 = 走环境变量兜底）。
        """
        # 软校验：格式不像就确认一次再保存
        checks = []
        if volc_key and not re.match(
                r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$", volc_key):
            checks.append("豆包语音 Key 不像 UUID 格式")
        if ds_key and not ds_key.startswith("sk-"):
            checks.append("DeepSeek Key 不以 sk- 开头")
        if checks:
            if not messagebox.askyesno(
                    "格式确认", "；".join(checks) + "。\n格式看起来不对，确定保存吗？\n"
                    "（留空也可以保存，将改用环境变量里的 Key）"):
                return
        try:
            save_config_value("volc", "api_key", volc_key)
            save_config_value("deepseek", "api_key", ds_key)
        except OSError as e:
            log.exception("[设置] 写 config.toml 失败")
            messagebox.showerror("保存失败", f"写入 config.toml 失败：\n{e}")
            return
        self.cfg["volc"]["api_key"] = volc_key
        self.cfg["deepseek"]["api_key"] = ds_key
        win.destroy()
        log.info("[设置] 豆包语音 key 已更新（%d字符）；DeepSeek key 已更新（%d字符）",
                 len(volc_key), len(ds_key))

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
        stamp = time.strftime("%Y%m%d_%H%M")
        rec_dir = os.path.join(BASE_DIR, "录音")
        os.makedirs(rec_dir, exist_ok=True)
        self.rec_path = os.path.join(rec_dir, f"会议录音_{stamp}.wav")
        self.session = MeetingStreamSession(self.cfg)
        self.speaker_map = IncrementalSpeakerMap()
        self.stream_connect_failed = False
        self._reset_transcript()
        try:
            self.recorder.start(self.rec_path, on_frame=self._on_audio_frame)
        except RecorderError as e:
            messagebox.showerror("打不开麦克风", str(e))
            return
        log.info("[录音] 开始：%s", self.rec_path)
        self.btn_start.config(state="disabled")
        self.btn_stop.config(state="normal")
        self._set_status("正在连接流式识别…", "#8e44ad")
        # 建连可能要几秒，放后台线程，不卡界面；录音照常进行
        threading.Thread(target=self._connect_stream, daemon=True).start()
        self._tick_job = self.root.after(500, self._tick)

    def _connect_stream(self):
        """后台线程建流式连接。结果经 progress_q 回主线程。"""
        try:
            self.session.start(on_utterance=lambda u: self.utter_q.put(u))
        except Exception as e:
            log.warning("[流式] 连接失败：%s", e)
            self.progress_q.put(("stream_error", str(e)))
        else:
            self.progress_q.put(("stream_ok", None))

    def _on_audio_frame(self, frame):
        """PortAudio 回调线程里被调：喂流式会话。内部只入队，不阻塞。"""
        if self.session is not None:
            self.session.feed(frame)

    def _tick(self):
        """每 500ms 刷新「录音中 xx:xx」+ 状态行（连接状态 / 已识别句数）。"""
        if self.recorder.recording:
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
            self._set_status(
                f"录音中 · {conn} · 已识别 {self.session.utterance_count} 句", color)
            self._tick_job = self.root.after(500, self._tick)
        else:
            self.elapsed.config(text="")

    def on_stop(self):
        try:
            self.rec_duration = self.recorder.stop()
        except RecorderError as e:
            self._reset_buttons()
            messagebox.showwarning("录音有问题", str(e))
            return
        self._cancel_job(self._tick_job)
        self._set_status("收尾中…（等最后一两句转写）", "#8e44ad")
        self.btn_start.config(state="disabled")
        self.btn_stop.config(state="disabled")
        self.worker = threading.Thread(target=self._worker_run, daemon=True)
        self.worker.start()

    # ---------------- 管线（后台线程） ----------------
    def _worker_run(self):
        """后台线程：流式收尾 → 收集好的 utterances → 映射 → 纪要 → docx。"""
        def progress(state, detail):
            self.progress_q.put((state, detail))

        session = self.session
        utterances = []
        if session is not None and session.started:
            progress("finishing", "")
            session.finish()
            utterances = session.utterances
        try:
            docx_path, report = run_pipeline_streaming(
                utterances, progress=progress, cfg=self.cfg)
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
        self.progress_q.put(("done", (docx_path, report["md_path"])))

    # ---------------- 队列轮询（主线程） ----------------
    def _poll_queues(self):
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
        self.root.after(200, self._poll_queues)

    def _on_progress(self, state, detail):
        if state == "stream_ok":
            log.info("[流式] 连接成功")
            return
        if state == "stream_error":
            self.stream_connect_failed = True
            log.warning("[流式] 连接失败，本次会议无实时转写：%s", detail)
            return
        if state == "done":
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

    def _finish(self, pair):
        docx_path, md_path = pair
        out_dir = os.path.dirname(docx_path)
        messagebox.showinfo("已完成", f"已生成会议纪要（Word + Markdown）：\n"
                                      f"{docx_path}\n{md_path}")
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
        if self.session is not None and self.session.started:
            try:
                self.session.finish()
            except Exception:
                pass
        self.root.destroy()


def main():
    setup_logging()
    log.info("=" * 40)
    log.info("[启动] YanJi 言纪 · 会议记录")
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
