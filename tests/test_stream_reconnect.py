# -*- coding: utf-8 -*-
"""断线重连模拟单测：用假 websocket 验证「断开 → 指数退避重连 → 清音频缓冲
恢复 → 分句继续收」，不碰网络。

monkeypatch streaming_asr._ws_connect：第 1 个连接读 2 帧后断开；第 2 个连接
正常循环回 definite 分句（会话 finish 时才断，让 reader 快速退出）。

重点断言（定版行为的回归）：
- 断线前喂、还没发出去的音频在重建后**不得**重发（缓冲在断开时已清空）；
- 退避期间喂的音频（≤10s）在重建后**必须**补发给新会话；
- 重连成功后去重集清空（新会话 start_time 可能从 0 重数，不许把新句吃掉）。
"""
import collections
import gzip
import json
import os
import struct
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np

import streaming_asr
from streaming_asr import (
    COMPRESSION_GZIP,
    FLAG_NEG_WITH_SEQ,
    FLAG_POS_SEQ,
    MSG_AUDIO_ONLY,
    MSG_SERVER_FULL_RESPONSE,
    SERIALIZATION_JSON,
    MeetingStreamSession,
    build_header,
    frame_to_pcm_bytes,
)


def _server_frame(data, flags):
    body = gzip.compress(json.dumps(data, ensure_ascii=False).encode("utf-8"))
    return (build_header(MSG_SERVER_FULL_RESPONSE, flags,
                         SERIALIZATION_JSON, COMPRESSION_GZIP)
            + struct.pack(">i", 1) + struct.pack(">I", len(body)) + body)


def _utt_frame(text, speaker, start_time):
    data = {"result": {"utterances": [
        {"definite": True, "text": text, "end_time": start_time + 800,
         "additions": {"speaker_id": speaker},
         "words": [{"start_time": start_time, "end_time": start_time + 100,
                    "text": "x"}]}]}}
    return _server_frame(data, FLAG_POS_SEQ)


def _sent_audio(ws):
    """假 ws 记录里取出 audio only 帧的 PCM（跳过 full client request 帧）。"""
    out = []
    for frame in ws.sent:
        if frame[1] >> 4 != MSG_AUDIO_ONLY:
            continue
        size = struct.unpack(">I", frame[8:12])[0]
        out.append(gzip.decompress(frame[12:12 + size]))
    return out


class _FakeWs:
    """极简假 websocket。killer() 返回 True 时 recv 抛断开；否则循环回帧。"""

    def __init__(self, frames, killer, send_delay=0.0):
        self._frames = list(frames)
        self._pos = 0
        self._killer = killer
        self._send_delay = send_delay   # 模拟阻塞的 socket 写（制造发不出去的积压）
        self.sent = []
        self.closed = False

    def send(self, frame):
        if self._send_delay:
            time.sleep(self._send_delay)
        self.sent.append(frame)

    def recv(self, timeout=None):
        if self._killer():
            raise OSError("模拟连接断开")
        if self._frames:
            frame = self._frames[self._pos % len(self._frames)]
            self._pos += 1
            return frame
        time.sleep(0.01)
        raise TimeoutError

    def close(self):
        self.closed = True


class _NotifyingDeque(collections.deque):
    """clear() 时置事件：让测试知道「缓冲已经被清空」这个确定性时刻。"""

    def __init__(self, *args, cleared=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._cleared = cleared

    def clear(self):
        super().clear()
        if self._cleared is not None:
            self._cleared.set()


class ReconnectTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="transcript_test_")
        self.cfg = {
            "volc": {"api_key": "test-key", "resource_id": "volc.seedasr.auc"},
            "streaming": {
                "url": "wss://fake.test/sauc/bigmodel_async",
                "resource_id": "volc.seedasr.sauc.duration",
                "model_name": "bigmodel", "ssd_version": "200",
                "enable_nonstream": True, "chunk_ms": 200, "reconnect_max": 5,
            },
            "asr": {"language": "zh-CN", "enable_speaker_info": True,
                    "show_utterances": True, "enable_punc": True,
                    "enable_itn": True},
        }
        self.session = None
        self.factory_calls = 0
        self._orig_connect = streaming_asr._ws_connect

    def tearDown(self):
        streaming_asr._ws_connect = self._orig_connect
        if self.session is not None:
            self.session.finish()
        for name in os.listdir(self.tmp):
            os.remove(os.path.join(self.tmp, name))
        os.rmdir(self.tmp)

    def _make_factory(self, first_killer):
        """连接工厂：第 1 次返回会断的连接，之后返回正常连接（finish 才断）。"""

        def factory(url, **kwargs):
            self.factory_calls += 1
            if self.factory_calls == 1:
                return _FakeWs([], first_killer)
            # 正常连接：循环回 2 句 definite 分句；会话 finish 时断开让 reader 快速退出
            return _FakeWs(
                [_utt_frame("我是张三", "0", 100),
                 _utt_frame("下面开始讨论", "1", 1200)],
                killer=lambda: self.session is not None
                and self.session._finishing.is_set())

        return factory

    def _wait_for(self, predicate, timeout=15.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.05)
        return False

    def test_reconnect_and_continue(self):
        # 第 1 个连接：读 2 次就断（模拟网络闪断）
        recv_calls = [0]

        def first_killer():
            recv_calls[0] += 1
            return recv_calls[0] > 2

        streaming_asr._ws_connect = self._make_factory(first_killer)
        collected = []
        self.session = MeetingStreamSession(self.cfg, on_utterance=collected.append,
                                            transcript_dir=self.tmp)

        with self.assertLogs("会议记录", level="WARNING") as cm:
            self.session.start()
            # 喂 2 包 200ms 音频（3200 样本/包）
            chunk = np.zeros(3200, dtype=np.float32)
            self.session.feed(chunk)
            self.session.feed(chunk)
            # 等：重连发生（工厂第 2 次调用）+ 2 句分句收齐
            self.assertTrue(
                self._wait_for(lambda: self.factory_calls >= 2 and len(collected) == 2),
                "重连后应继续收到分句")
            time.sleep(0.3)

        # 1. 断线警告 + 重建警告都出现了
        output = "\n".join(cm.output)
        self.assertIn("连接断开", output)
        self.assertIn("连接已重建，说话人标签可能漂移", output)

        # 2. 分句带实测字段口径（additions.speaker_id → speaker）
        self.assertEqual([u["speaker"] for u in collected], ["0", "1"])
        self.assertEqual(collected[0]["text"], "我是张三")
        self.assertEqual(collected[0]["start_time"], 100)

        # 3. 重连后连接恢复、状态不失败
        self.assertTrue(self.session.connected)
        self.assertFalse(self.session.failed)

        # 4. 收尾：能正常 finish（发负 seq 收尾包、reader 退出、jsonl 关闭）
        t0 = time.monotonic()
        self.session.finish()
        self.assertLess(time.monotonic() - t0, 10.0)

        # 5. jsonl 落盘 2 句（崩机不丢的验证点）
        (jsonl_path,) = [os.path.join(self.tmp, n) for n in os.listdir(self.tmp)]
        with open(jsonl_path, encoding="utf-8") as f:
            lines = [json.loads(line) for line in f if line.strip()]
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0]["speaker"], "0")
        self.assertEqual(lines[1]["text"], "下面开始讨论")

        # 6. 最后一包是负 seq 收尾帧
        last = self.session._ws.sent[-1]
        self.assertEqual(last[1] & 0x0F, FLAG_NEG_WITH_SEQ)

    def test_finish_during_disconnect_aborts_reconnect(self):
        """散会时连接已断：finish 应立即结束，不再等重连。"""
        streaming_asr._ws_connect = self._make_factory(lambda: True)
        self.session = MeetingStreamSession(self.cfg, on_utterance=None,
                                            transcript_dir=self.tmp)
        self.session.start()
        self.assertTrue(self._wait_for(lambda: not self.session.connected))
        t0 = time.monotonic()
        self.session.finish()  # 不应卡在重连退避上
        self.assertLess(time.monotonic() - t0, 5.0)
        self.assertFalse(self.session.failed)

    def test_reconnect_clears_buffer_and_keeps_backoff_audio(self):
        """定版：断开即清音频缓冲（还没发出去的包不许在新连接重发），
        退避期间喂的音频重建后必须补发。

        用「send 慢 0.2 秒」制造发不出去的积压，用「缓冲被 clear 的事件」卡住
        时序（免得落在清空之前的竞态窗口里）。
        """
        kill = threading.Event()
        cleared = threading.Event()
        conns = []

        def factory(url, **kwargs):
            # 第 1 个连接 send 慢（音频在缓冲里堆住）、由 kill 掐断；
            # 之后是正常连接（会话 finish 时才断）
            if not conns:
                ws = _FakeWs([], kill.is_set, send_delay=0.2)
            else:
                ws = _FakeWs([], lambda: self.session is not None
                             and self.session._finishing.is_set())
            conns.append(ws)
            return ws

        streaming_asr._ws_connect = factory
        self.session = MeetingStreamSession(self.cfg, on_utterance=None,
                                            transcript_dir=self.tmp)
        self.session._audio_buf = _NotifyingDeque(
            maxlen=self.session._audio_buf.maxlen, cleared=cleared)
        self.session.start()

        mark_a = frame_to_pcm_bytes(np.full(3200, 0.25, dtype=np.float32))
        mark_b = frame_to_pcm_bytes(np.full(3200, 0.75, dtype=np.float32))
        # 断线前喂 3 包（0.25）：send 慢，至少第 2、3 包还躺在缓冲里
        for _ in range(3):
            self.session.feed(np.full(3200, 0.25, dtype=np.float32))
        kill.set()                      # 触发断线
        self.assertTrue(self._wait_for(lambda: cleared.is_set()),
                        "reader 检测到断开后应先清空音频缓冲")
        # 退避期间喂 2 包（0.75）：清空之后入队 → 重建后必须补发
        for _ in range(2):
            self.session.feed(np.full(3200, 0.75, dtype=np.float32))
        self.assertTrue(
            self._wait_for(lambda: len(conns) >= 2 and self.session.connected
                           and _sent_audio(conns[1]).count(mark_b) == 2),
            "退避期间积累的音频必须补发到新连接")
        # 断线前喂的包一个都不许在新连接重发（缓冲已清）
        self.assertNotIn(mark_a, _sent_audio(conns[1]))
        # 第 1 个连接上确实发过 0.25 的包（证明这段音频真的走过主路径）
        self.assertTrue(any(p == mark_a for p in _sent_audio(conns[0])))

    def test_reconnect_clears_dedup_seen(self):
        """重连后新会话的 start_time 可能从 0 重数：旧去重键不许把新分句吃掉。"""
        recv_calls = [0]

        def first_killer():
            recv_calls[0] += 1
            return recv_calls[0] > 1

        conns = []

        def factory(url, **kwargs):
            if not conns:
                ws = _FakeWs([_utt_frame("断线前那句", "0", 100)], first_killer)
            else:
                # 关键：start_time 与断线前那句相同（新会话从头数）
                ws = _FakeWs([_utt_frame("重连后那句", "1", 100)],
                             killer=lambda: self.session is not None
                             and self.session._finishing.is_set())
            conns.append(ws)
            return ws

        streaming_asr._ws_connect = factory
        collected = []
        self.session = MeetingStreamSession(self.cfg, on_utterance=collected.append,
                                            transcript_dir=self.tmp)
        self.session.start()
        self.assertTrue(
            self._wait_for(lambda: len(conns) >= 2
                           and [u["text"] for u in collected] == ["断线前那句", "重连后那句"]),
            "重连后同 start_time 的新分句不许被去重丢掉")
        self.assertEqual([u["speaker"] for u in collected], ["0", "1"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
