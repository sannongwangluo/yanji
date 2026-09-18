# -*- coding: utf-8 -*-
"""滚动摘要状态机（长会议中途预写摘要，散会出稿更快）。

- 触发定版：录音时长 ≥ 30 分钟才启用，之后每 20 分钟一轮（下一轮触发点 = 本轮触发点
  + 20 分钟，不看 LLM 跑多久）；
- 纯逻辑：不碰 tkinter、不自己调 LLM。GUI 主线程 after() 里问 due() → 工作线程跑
  minutes_llm.generate_digest() → 结果经队列回主线程 commit()/fail()；
- 每轮落盘 日志/digest_YYYYMMDD_HHMM.md（临时文件 + os.replace 原子写）：崩机后手上
  还有最新摘要可人工补救（不做自动恢复）；
- 摘要失败绝不影响出稿：fail() 只复位 busy，出稿走整稿路径照常。
"""
import logging
import os
import threading
import time
import uuid

log = logging.getLogger("会议记录")

DIGEST_THRESHOLD_SEC = 30 * 60    # 录音满 30 分钟才启用
DIGEST_INTERVAL_SEC = 20 * 60     # 每 20 分钟跑一轮
DIGEST_MIN_UTTERANCES = 5         # 新增分句少于这个数不值得跑一轮 LLM


class RollingDigest:
    """一次会议的滚动摘要状态（text/covered/count/busy）。"""

    def __init__(self, digest_dir, threshold_sec=DIGEST_THRESHOLD_SEC,
                 interval_sec=DIGEST_INTERVAL_SEC,
                 min_utterances=DIGEST_MIN_UTTERANCES):
        # text/covered 是跨线程读写的（工作线程取快照、主线程 commit）：
        # 分两行读会读到「新 covered + 旧 text」，那一段发言就从终稿里不见了。
        self._lock = threading.RLock()
        self.digest_dir = digest_dir
        self.threshold_sec = threshold_sec
        self.interval_sec = interval_sec
        self.min_utterances = min_utterances
        self.text = ""            # 当前最新摘要（空 = 还没有）
        self.covered = 0          # 已纳入摘要的分句条数
        self.count = 0            # 已成功预写的段数（GUI 状态行显示）
        self.busy = False         # 有一轮正在后台跑
        self.last_error = ""
        self.last_path = None     # 最近一次落盘的文件路径
        self._next_at = threshold_sec   # 下一轮触发的录音秒数

    # ---- 快照 ----
    def snapshot(self):
        """原子取回 (text, covered)——工作线程只用这个，绝不分成两次读。

        分两行读的话，主线程 commit() 可能插在中间：读到旧 text 配新 covered，
        出稿时那一段发言会被当成「已在摘要里」而丢掉。
        """
        with self._lock:
            return self.text, self.covered

    # ---- 判定 ----
    def due(self, elapsed_sec, utterance_count):
        """现在该跑一轮吗（主线程每 500ms 问一次；不改变状态）。"""
        with self._lock:
            if self.busy or elapsed_sec < self._next_at:
                return False
            return utterance_count - self.covered >= self.min_utterances

    def begin(self, utterances):
        """取本轮快照并置 busy；不该跑返回 None。

        返回 {"prior": 上一版摘要, "new": 新增分句（已带 speaker_name）, "covered": 覆盖条数}。
        覆盖条数用「取快照时的总条数」，保证 commit 时不会把比摘要更新的句子算进去。
        """
        with self._lock:
            if self.busy:
                return None
            new = list(utterances[self.covered:])
            if not new:
                return None
            self.busy = True
            return {"prior": self.text, "new": new, "covered": len(utterances)}

    # ---- 结果回填 ----
    def commit(self, text, covered):
        """一轮成功：更新摘要 + 落盘，返回落盘路径（落盘失败返回 None）。"""
        with self._lock:
            self.text = (text or "").strip()
            self.covered = max(self.covered, int(covered))
            self.count += 1
            self.busy = False
            self.last_error = ""
            self._next_at += self.interval_sec
        self.last_path = self.save()
        return self.last_path

    def fail(self, reason=""):
        """一轮失败：复位 busy、记原因，等下一轮（不影响出稿主路径）。"""
        with self._lock:
            self.busy = False
            self.last_error = str(reason)
            self._next_at += self.interval_sec
        log.warning("[摘要] 本轮预写失败（不影响出稿）：%s", reason)

    # ---- 落盘 ----
    def save(self, now=None):
        """原子写 日志/digest_YYYYMMDD_HHMM.md（同名占位就加 _2/_3…）。

        任何 OSError 都只记 warning 并返回 None：save() 是在 GUI 主线程里被
        commit() 调用的，异常穿出去会打死 after() 轮询链（界面整个停摆）。
        """
        tmp = None
        try:
            os.makedirs(self.digest_dir, exist_ok=True)
            stamp = (time.strftime("%Y%m%d_%H%M", now) if now
                     else time.strftime("%Y%m%d_%H%M"))
            base = os.path.join(self.digest_dir, f"digest_{stamp}")
            path, n = f"{base}.md", 2
            while os.path.exists(path):
                path = f"{base}_{n}.md"
                n += 1
            tmp = f"{path}.{uuid.uuid4().hex}.tmp"
            with open(tmp, "w", encoding="utf-8", newline="\n") as f:
                f.write(self.text)
                f.write("\n")
            os.replace(tmp, path)
        except OSError as e:
            log.warning("[摘要] 落盘失败：%s", e)
            return None
        finally:
            if tmp is not None:
                try:
                    if os.path.exists(tmp):
                        os.remove(tmp)
                except OSError:
                    pass
        log.info("[摘要] 已预写第 %d 段摘要（覆盖前 %d 句），落盘 %s",
                 self.count, self.covered, path)
        return path
