# -*- coding: utf-8 -*-
"""流式协议单测：二进制帧打包/解析 roundtrip + definite 分句抽取 + 会话级防御
（全部假数据/假 ws，不碰网络）。

覆盖：4 字节头布局、full client request 剥壳校验、audio 帧正/负 seq、
服务端帧解析 roundtrip、error 帧错误码、残缺帧容错、definite 过滤、
speaker 字段名多路兼容、去重（真实链：start_time→text 兜底）、float32→s16le 转换、
分包时长读 config chunk_ms、reader 撞畸形帧不死、连续错误帧触发重连、
finish() 收尾三处防御（未启动也置停 / 抢发送锁带超时 / 收尾后 sender 丢弃缓冲）、
jsonl 落盘失败不阻断识别。
"""
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
    HEADER_SIZE,
    MSG_AUDIO_ONLY,
    MSG_FULL_REQUEST,
    MSG_SERVER_ERROR,
    MSG_SERVER_FULL_RESPONSE,
    PROTOCOL_VERSION,
    SERIALIZATION_JSON,
    MeetingStreamSession,
    build_audio_request,
    build_full_request,
    build_header,
    definite_utterances,
    filter_new_utterances,
    frame_to_pcm_bytes,
    parse_response,
    _speaker_of,
)

_SESSION_CFG = {
    "volc": {"api_key": "test-key"},
    "streaming": {"url": "wss://fake.test/sauc/bigmodel_async",
                  "resource_id": "volc.seedasr.sauc.duration",
                  "model_name": "bigmodel", "ssd_version": "200",
                  "enable_nonstream": True, "chunk_ms": 200, "reconnect_max": 5},
    "asr": {"language": "zh-CN", "enable_speaker_info": True,
            "show_utterances": True, "enable_punc": True, "enable_itn": True},
}


def _cfg(**stream_over):
    """会话 cfg（深拷贝，测试之间不互相污染）。"""
    cfg = json.loads(json.dumps(_SESSION_CFG))
    cfg["streaming"].update(stream_over)
    return cfg


def _wait_for(predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


class _ScriptWs:
    """假 websocket：recv 依次吐脚本里的帧，脚本空了就 TimeoutError（小睡防热转）；
    stop() 返回 True 时当作连接已关（收尾让 reader 立刻退出，不然要等 6 秒超时）。"""

    def __init__(self, frames=(), stop=None):
        self._frames = list(frames)
        self._stop = stop
        self.sent = []
        self.closed = False

    def send(self, frame):
        self.sent.append(frame)

    def recv(self, timeout=None):
        if self._stop is not None and self._stop():
            raise OSError("收尾：模拟连接关闭")
        if self._frames:
            return self._frames.pop(0)
        time.sleep(0.01)
        raise TimeoutError

    def close(self):
        self.closed = True


class _BrokenFile:
    """写必失败的假文件句柄（验证落盘失败路径）。"""

    def __init__(self):
        self.closed = False

    def write(self, _s):
        raise OSError("模拟磁盘写失败")

    def flush(self):
        pass

    def close(self):
        self.closed = True


def _payload_of(frame, offset=12):
    """帧 → 按协议剥出 gzip 解压后的 JSON dict（客户端帧结构：4 头+4 seq+4 size+body）。"""
    size = struct.unpack(">I", frame[offset - 4:offset])[0]
    return json.loads(gzip.decompress(frame[offset:offset + size]).decode("utf-8"))


def _server_frame(data, flags, message_type=MSG_SERVER_FULL_RESPONSE, code=0):
    """测试独立构造服务端帧（与实现走不同代码路径，验证 parse_response 正确解析）。"""
    body = gzip.compress(json.dumps(data, ensure_ascii=False).encode("utf-8"))
    head = build_header(message_type, flags, SERIALIZATION_JSON, COMPRESSION_GZIP)
    seq = struct.pack(">i", 7) if flags & 0x01 else b""
    if message_type == MSG_SERVER_ERROR:
        return head + seq + struct.pack(">i", code) + struct.pack(">I", len(body)) + body
    return head + seq + struct.pack(">I", len(body)) + body


def _utt_frame(text, speaker, start_time):
    """一句 definite 分句的 full response 帧（实测字段口径：additions.speaker_id）。"""
    return _server_frame({"result": {"utterances": [
        {"definite": True, "text": text, "end_time": start_time + 800,
         "additions": {"speaker_id": speaker},
         "words": [{"start_time": start_time, "end_time": start_time + 100,
                    "text": "x"}]}]}}, FLAG_POS_SEQ)


def _error_frame(code):
    return _server_frame({}, FLAG_POS_SEQ, message_type=MSG_SERVER_ERROR, code=code)


class HeaderTest(unittest.TestCase):
    def test_build_header(self):
        h = build_header(MSG_FULL_REQUEST, FLAG_POS_SEQ,
                         SERIALIZATION_JSON, COMPRESSION_GZIP)
        self.assertEqual(len(h), 4)
        self.assertEqual(h[0], (PROTOCOL_VERSION << 4) | HEADER_SIZE)
        self.assertEqual(h[1], (MSG_FULL_REQUEST << 4) | FLAG_POS_SEQ)
        self.assertEqual(h[2], (SERIALIZATION_JSON << 4) | COMPRESSION_GZIP)
        self.assertEqual(h[3], 0x00)


class FullRequestTest(unittest.TestCase):
    def test_roundtrip_fields(self):
        frame = build_full_request(1, uid="单测", request={"model_name": "bigmodel"})
        # 手工按协议剥壳（不依赖实现内部函数）
        self.assertEqual(frame[0], (PROTOCOL_VERSION << 4) | HEADER_SIZE)
        self.assertEqual(frame[1], (MSG_FULL_REQUEST << 4) | FLAG_POS_SEQ)
        self.assertEqual(frame[2], (SERIALIZATION_JSON << 4) | COMPRESSION_GZIP)
        self.assertEqual(struct.unpack(">i", frame[4:8])[0], 1)
        size = struct.unpack(">I", frame[8:12])[0]
        self.assertEqual(len(frame), 12 + size)
        payload = json.loads(gzip.decompress(frame[12:12 + size]).decode("utf-8"))
        self.assertEqual(payload["user"]["uid"], "单测")
        self.assertEqual(payload["audio"]["format"], "pcm")
        self.assertEqual(payload["audio"]["rate"], 16000)
        self.assertEqual(payload["audio"]["bits"], 16)
        self.assertEqual(payload["audio"]["channel"], 1)
        self.assertEqual(payload["request"]["model_name"], "bigmodel")

    def test_default_request_streaming_params(self):
        payload = _payload_of(build_full_request(1))
        req = payload["request"]
        self.assertTrue(req["enable_nonstream"])     # 二遍识别
        self.assertTrue(req["enable_speaker_info"])  # 说话人分离
        self.assertEqual(req["ssd_version"], "200")
        self.assertEqual(req["result_type"], "single")
        self.assertTrue(req["enable_punc"])
        self.assertTrue(req["enable_itn"])
        self.assertTrue(req["show_utterances"])


class AudioRequestTest(unittest.TestCase):
    def test_audio_frame_roundtrip(self):
        pcm = b"\x00" * 6400  # 200ms @16k s16le
        frame = build_audio_request(3, pcm, is_last=False)
        self.assertEqual(frame[1], (MSG_AUDIO_ONLY << 4) | FLAG_POS_SEQ)
        self.assertEqual(struct.unpack(">i", frame[4:8])[0], 3)
        size = struct.unpack(">I", frame[8:12])[0]
        self.assertEqual(gzip.decompress(frame[12:12 + size]), pcm)

    def test_last_frame_negative_seq(self):
        frame = build_audio_request(5, b"", is_last=True)
        self.assertEqual(frame[1] & 0x0F, FLAG_NEG_WITH_SEQ)
        self.assertEqual(struct.unpack(">i", frame[4:8])[0], -5)


class ParseResponseTest(unittest.TestCase):
    def test_full_response_roundtrip(self):
        data = {"result": {"text": "我是张三", "utterances": [
            {"definite": True, "text": "我是张三", "speaker": "1",
             "start_time": 0, "end_time": 1500}]}}
        resp = parse_response(_server_frame(data, FLAG_POS_SEQ))
        self.assertIsNotNone(resp)
        self.assertEqual(resp["message_type"], MSG_SERVER_FULL_RESPONSE)
        self.assertFalse(resp["is_last"])
        self.assertEqual(resp["seq"], 7)
        self.assertEqual(resp["data"]["result"]["text"], "我是张三")

    def test_last_flag(self):
        resp = parse_response(_server_frame({"result": {}}, 0b0011))
        self.assertTrue(resp["is_last"])

    def test_error_frame_code(self):
        resp = parse_response(_server_frame({}, FLAG_POS_SEQ,
                                            message_type=MSG_SERVER_ERROR,
                                            code=45000002))
        self.assertEqual(resp["code"], 45000002)
        self.assertEqual(resp["message_type"], MSG_SERVER_ERROR)

    def test_truncated_frames_return_none(self):
        self.assertIsNone(parse_response(b""))
        self.assertIsNone(parse_response(b"\x11\x91"))
        self.assertIsNone(parse_response(None))

    def test_bad_gzip_payload(self):
        # size 合法但 payload 不是 gzip：解析不炸、data=None
        frame = (build_header(MSG_SERVER_FULL_RESPONSE, FLAG_POS_SEQ,
                              SERIALIZATION_JSON, COMPRESSION_GZIP)
                 + struct.pack(">i", 1) + struct.pack(">I", 4) + b"xxxx")
        resp = parse_response(frame)
        self.assertIsNone(resp["data"])


class UtteranceExtractTest(unittest.TestCase):
    def test_only_definite(self):
        data = {"result": {"utterances": [
            {"text": "部分文本", "definite": False, "speaker": "1"},
            {"text": "我是张三", "definite": True, "speaker": "1",
             "start_time": 0, "end_time": 1500},
        ]}}
        utts = definite_utterances(data)
        self.assertEqual(len(utts), 1)
        self.assertEqual(utts[0]["text"], "我是张三")
        self.assertEqual(utts[0]["speaker"], "1")
        self.assertEqual(utts[0]["start_time"], 0)
        self.assertEqual(utts[0]["end_time"], 1500)

    def test_definite_string_true(self):
        data = {"result": {"utterances": [
            {"text": "我是张三", "definite": "true", "speaker": "2"}]}}
        self.assertEqual(len(definite_utterances(data)), 1)

    def test_definite_in_additions(self):
        data = {"result": {"utterances": [
            {"text": "我是张三", "additions": {"definite": True}, "speaker": "2"}]}}
        self.assertEqual(len(definite_utterances(data)), 1)

    def test_start_time_from_words(self):
        """实测：definite 句无 utterance 级 start_time，从逐字 words 推导。"""
        data = {"result": {"utterances": [
            {"text": "你好", "definite": True,
             "additions": {"speaker_id": "0"}, "end_time": 999,
             "words": [{"start_time": -1, "text": " "},
                       {"start_time": 200, "end_time": 280, "text": "你"}]}]}}
        utts = definite_utterances(data)
        self.assertEqual(len(utts), 1)
        self.assertEqual(utts[0]["speaker"], "0")   # additions.speaker_id（实测字段名）
        self.assertEqual(utts[0]["start_time"], 200)
        self.assertEqual(utts[0]["end_time"], 999)

    def test_speaker_field_fallbacks(self):
        """speaker 字段名以实测为准，先兼容多路。"""
        for key, val in [("speaker", "1"), ("speaker_id", "S2"), ("spk", "3")]:
            self.assertEqual(_speaker_of({"text": "x", key: val}), val)
        self.assertEqual(_speaker_of({"text": "x", "additions": {"speaker": "5"}}), "5")
        self.assertEqual(_speaker_of({"text": "x", "additions": {"speaker_id": "6"}}), "6")
        self.assertEqual(_speaker_of({"text": "x"}), "0")

    def test_empty_text_skipped(self):
        data = {"result": {"utterances": [
            {"text": "  ", "definite": True, "speaker": "1"}]}}
        self.assertEqual(definite_utterances(data), [])

    def test_missing_result(self):
        self.assertEqual(definite_utterances({"code": 1}), [])
        self.assertEqual(definite_utterances(None), [])
        self.assertEqual(definite_utterances({"result": {"text": "只有全文"}}), [])


class FilterNewTest(unittest.TestCase):
    def test_dedup_real_chain_start_time_then_text(self):
        """真实去重链：definite_utterances 的输出没有 utterance_id 键，
        去重只能靠 start_time → text 兜底（实测定版，2026-09-02）。
        同一句重复下发只收一次；同一句的修订版（同 start_time、文本变长）也算重复。
        （旧用例构造带 utterance_id 的 dict 直接喂 filter，测的是生产不可达分支。）
        """
        frame = {"definite": True, "text": "我是张三", "end_time": 900,
                 "additions": {"speaker_id": "0"},
                 "words": [{"start_time": 0, "end_time": 100, "text": "我"}]}
        data = {"result": {"utterances": [frame]}}
        seen = set()
        first, seen = filter_new_utterances(definite_utterances(data), seen)
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0]["start_time"], 0)
        # 同一句再下发一遍（服务端会重复发）：去重键是 t:0，丢掉
        second, _ = filter_new_utterances(definite_utterances(data), seen)
        self.assertEqual(second, [])
        # 同一句的修订版（同 start_time、文本变长）也走同一条去重键
        revised = {"result": {"utterances": [dict(frame, text="我是张三，大家好")]}}
        third, _ = filter_new_utterances(definite_utterances(revised), seen)
        self.assertEqual(third, [])

    def test_dedup_real_chain_text_fallback(self):
        """既没有 start_time 也没有 utterance_id（words 也取不到）→ 落到 text 兜底。"""
        data = {"result": {"utterances": [{"definite": True, "text": "你好"}]}}
        first, seen = filter_new_utterances(definite_utterances(data), set())
        second, _ = filter_new_utterances(definite_utterances(data), seen)
        self.assertEqual(len(first), 1)
        self.assertIsNone(first[0]["start_time"])
        self.assertEqual(second, [])

    def test_dedup_fallback_start_time(self):
        seen = set()
        new1, seen = filter_new_utterances(
            [{"text": "a", "speaker": "1", "start_time": 100}], seen)
        new2, _ = filter_new_utterances(
            [{"text": "a", "speaker": "1", "start_time": 100}], seen)
        self.assertEqual(len(new1), 1)
        self.assertEqual(new2, [])

    def test_dedup_id_in_additions(self):
        seen = set()
        u = {"text": "a", "speaker": "1", "additions": {"utterance_id": "9"}}
        self.assertEqual(len(filter_new_utterances([u], seen)[0]), 1)
        self.assertEqual(filter_new_utterances([u], seen)[0], [])


class PcmTest(unittest.TestCase):
    def test_frame_to_pcm_bytes(self):
        frame = np.array([0.0, 1.0, -1.0], dtype=np.float32)
        self.assertEqual(frame_to_pcm_bytes(frame),
                         struct.pack("<hhh", 0, 32767, -32767))

    def test_clip_out_of_range(self):
        self.assertEqual(frame_to_pcm_bytes(np.array([2.0], dtype=np.float32)),
                         struct.pack("<h", 32767))
        self.assertEqual(frame_to_pcm_bytes(np.array([-2.0], dtype=np.float32)),
                         struct.pack("<h", -32767))

    def test_empty(self):
        self.assertEqual(frame_to_pcm_bytes(np.array([], dtype=np.float32)), b"")

    def test_2d_frame_flattens(self):
        """PortAudio 回调给的是 (frames,1) 二维数组，应能直接转。"""
        frame = np.array([[0.5], [-0.5]], dtype=np.float32)
        out = frame_to_pcm_bytes(frame)
        self.assertEqual(len(out), 4)


class ChunkMsConfigTest(unittest.TestCase):
    """分包时长 = config [streaming] chunk_ms（旧实现写死 200ms，配置是死的）。"""

    def _session(self, cfg):
        return MeetingStreamSession(cfg)

    def test_chunk_ms_drives_packet_size(self):
        session = self._session(_cfg(chunk_ms=100))
        self.assertEqual(session._chunk_bytes, 16000 * 2 * 100 // 1000)   # 3200
        # 3200 样本 float32 = 6400 字节 PCM → 100ms 一包 = 2 包
        session.feed(np.zeros(3200, dtype=np.float32))
        self.assertEqual([len(p) for p in session._audio_buf], [3200, 3200])

    def test_default_is_200ms(self):
        session = self._session(_cfg())
        self.assertEqual(session._chunk_bytes, 6400)
        session.feed(np.zeros(3200, dtype=np.float32))
        self.assertEqual([len(p) for p in session._audio_buf], [6400])

    def test_illegal_chunk_ms_falls_back_with_warning(self):
        for bad in (0, -5, 99999, "abc"):
            with self.assertLogs("会议记录", level="WARNING") as cm:
                session = self._session(_cfg(chunk_ms=bad))
            self.assertEqual(session._chunk_bytes, 6400, bad)
            self.assertIn("chunk_ms", "\n".join(cm.output))

    def test_missing_chunk_ms_silent_default(self):
        cfg = _cfg()
        del cfg["streaming"]["chunk_ms"]
        with self.assertNoLogs("会议记录", level="WARNING"):
            session = self._session(cfg)
        self.assertEqual(session._chunk_bytes, 6400)


class ReaderResilienceTest(unittest.TestCase):
    """reader 线程收帧之后的兜底：畸形帧只丢这一帧，绝不许把 reader 弄死。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="transcript_test_")
        self.session = None
        self._orig_connect = streaming_asr._ws_connect

    def tearDown(self):
        streaming_asr._ws_connect = self._orig_connect
        if self.session is not None:
            self.session.finish()
        for name in os.listdir(self.tmp):
            os.remove(os.path.join(self.tmp, name))
        os.rmdir(self.tmp)

    def _start(self, frames):
        ws = _ScriptWs(frames, stop=self._finishing_now)
        streaming_asr._ws_connect = lambda url, **kw: ws
        collected = []
        self.session = MeetingStreamSession(_cfg(), on_utterance=collected.append,
                                            transcript_dir=self.tmp)
        self.session.start()
        return ws, collected

    def _finishing_now(self):
        return self.session is not None and self.session._finishing.is_set()

    def test_malformed_payload_frames_dont_kill_reader(self):
        bad1 = _server_frame(123, FLAG_POS_SEQ)   # payload 是 JSON 数字，不是对象
        bad2 = _server_frame({"result": {"utterances": [
            {"definite": True, "text": ["畸形文本"]}]}}, FLAG_POS_SEQ)  # text 不是字符串
        ws, collected = self._start([bad1, bad2,
                                    _utt_frame("我是张三", "0", 100),
                                    _utt_frame("下面开始讨论", "1", 1200)])
        self.assertTrue(_wait_for(lambda: len(collected) == 2),
                        "畸形帧之后 reader 必须继续处理后续好帧")
        self.assertEqual([u["text"] for u in collected], ["我是张三", "下面开始讨论"])
        self.assertTrue(self.session.connected)
        self.assertFalse(self.session.failed)

    def test_three_error_frames_trigger_reconnect(self):
        # 非空音频错误码连续 3 个（中间没有正常数据帧）→ 与断线同路径重连
        first = _ScriptWs([_error_frame(45000001)] * 3,
                         stop=lambda: self.session is not None
                         and self.session._finishing.is_set())
        second = _ScriptWs([_utt_frame("重连后的句子", "0", 100)],
                           stop=lambda: self.session is not None
                           and self.session._finishing.is_set())
        conns = []

        def factory(url, **kwargs):
            ws = first if not conns else second
            conns.append(ws)
            return ws

        streaming_asr._ws_connect = factory
        collected = []
        self.session = MeetingStreamSession(_cfg(), on_utterance=collected.append,
                                            transcript_dir=self.tmp)
        with self.assertLogs("会议记录", level="WARNING") as cm:
            self.session.start()
            self.assertTrue(_wait_for(lambda: len(conns) >= 2 and len(collected) == 1),
                            "连续 3 个错误帧应触发重连并继续识别")
        output = "\n".join(cm.output)
        self.assertIn("连续 3 个错误帧", output)
        self.assertIn("连接已重建", output)
        self.assertEqual(collected[0]["text"], "重连后的句子")
        self.assertTrue(self.session.connected)
        self.assertFalse(self.session.failed)

    def test_two_error_frames_do_not_reconnect(self):
        """两个错误帧只是 warning（空音频提示 45000002 也不计数）。"""
        ws, collected = self._start([_error_frame(45000002), _error_frame(45000001),
                                    _error_frame(45000001),
                                    _utt_frame("正常句子", "0", 100)])
        self.assertTrue(_wait_for(lambda: len(collected) == 1))
        self.assertEqual(len(collected), 1)


class FinishDefenseTest(unittest.TestCase):
    """finish() 三处收尾防御。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="transcript_test_")

    def tearDown(self):
        for name in os.listdir(self.tmp):
            os.remove(os.path.join(self.tmp, name))
        os.rmdir(self.tmp)

    def test_finish_before_start_sets_stop(self):
        """没启动就散会：也要置 _stop，否则竞态里起来的 reader/sender 会活着空转。"""
        session = MeetingStreamSession(_cfg(), transcript_dir=self.tmp)
        session._open_jsonl()
        session.finish()
        self.assertTrue(session._stop.is_set())
        self.assertIsNone(session._jsonl_file)
        # 竞态：start 在 finish 之后才把线程拉起来 → 两个线程必须立刻退出
        session._reader = threading.Thread(target=session._reader_loop, daemon=True)
        session._sender = threading.Thread(target=session._sender_loop, daemon=True)
        session._reader.start()
        session._sender.start()
        session._reader.join(2)
        session._sender.join(2)
        self.assertFalse(session._reader.is_alive(), "reader 线程不该在收尾后活着")
        self.assertFalse(session._sender.is_alive(), "sender 线程不该在收尾后活着")
        session.finish()          # 幂等：重复调用不炸
        self.assertTrue(session._stop.is_set())

    def test_finish_sends_final_packet_then_locks_out_sender(self):
        """正常收尾：负 seq 收尾包 → 置 _final_sent（sender 不许再发音频）。"""
        ws = _ScriptWs()
        session = MeetingStreamSession(_cfg(), transcript_dir=self.tmp)
        session._started = True       # 白盒：不起线程，只验收尾契约
        session._ws = ws
        session._connected = True
        session.finish()
        self.assertEqual(len(ws.sent), 1)
        self.assertEqual(ws.sent[0][1] & 0x0F, FLAG_NEG_WITH_SEQ)   # 负 seq 收尾
        self.assertTrue(session._final_sent.is_set())
        self.assertTrue(session._stop.is_set())

    def test_finish_skips_final_packet_when_send_lock_busy(self):
        """sender 卡在 send 上时（锁拿不到）不许把收尾无限拖住。"""
        ws = _ScriptWs()
        session = MeetingStreamSession(_cfg(), transcript_dir=self.tmp)
        session._started = True       # 白盒：模拟已启动但 sender 占着锁
        session._ws = ws
        session._connected = True
        session._send_lock.acquire()
        orig = streaming_asr._FINISH_SEND_LOCK_TIMEOUT
        streaming_asr._FINISH_SEND_LOCK_TIMEOUT = 0.2
        try:
            with self.assertLogs("会议记录", level="WARNING") as cm:
                t0 = time.monotonic()
                session.finish()
                elapsed = time.monotonic() - t0
        finally:
            streaming_asr._FINISH_SEND_LOCK_TIMEOUT = orig
            session._send_lock.release()
        self.assertLess(elapsed, 2.0)
        self.assertIn("跳过收尾包", "\n".join(cm.output))
        self.assertEqual(ws.sent, [])          # 一个收尾包都没发出去
        self.assertTrue(session._final_sent.is_set())

    def test_sender_drops_buffer_after_final_sent(self):
        """收尾包发出后，sender 不许再发正 seq 音频（包级竞态），剩余缓冲丢弃。"""
        ws = _ScriptWs()
        session = MeetingStreamSession(_cfg(), transcript_dir=self.tmp)
        session._ws = ws
        session._final_sent.set()
        with session._cond:
            session._audio_buf.append(b"\x00" * 6400)
            session._cond.notify_all()
        session._sender_loop()        # 直接跑一轮：应立刻退出
        self.assertEqual(ws.sent, [])
        self.assertEqual(len(session._audio_buf), 0)


class JsonlFailureTest(unittest.TestCase):
    """落盘是外围能力：open/append 失败都不许掐掉识别主路径。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="transcript_test_")

    def tearDown(self):
        for name in os.listdir(self.tmp):
            os.remove(os.path.join(self.tmp, name))
        os.rmdir(self.tmp)

    def test_open_failure_does_not_raise(self):
        fd, path = tempfile.mkstemp()
        os.close(fd)
        try:
            session = MeetingStreamSession(
                _cfg(), transcript_dir=os.path.join(path, "sub"))  # 拿文件当目录
            with self.assertLogs("会议记录", level="WARNING") as cm:
                session._open_jsonl()
            self.assertIn("转写落盘不可用", "\n".join(cm.output))
            self.assertIsNone(session._jsonl_file)
            self.assertIsNone(session._jsonl_path)
            # 不落盘也照常收句（内存与回调路径不受影响）
            session._handle_data(json.loads(json.dumps(
                {"result": {"utterances": [{"definite": True, "text": "照常收",
                                            "additions": {"speaker_id": "0"}}]}})))
            self.assertEqual([u["text"] for u in session.utterances], ["照常收"])
            with self.assertLogs("会议记录", level="WARNING") as cm2:
                session._close_jsonl()
            self.assertIn("1 句没能落盘", "\n".join(cm2.output))
        finally:
            os.remove(path)

    def test_append_failure_logged_then_silently_counted(self):
        session = MeetingStreamSession(_cfg(), transcript_dir=self.tmp)
        session._jsonl_file = _BrokenFile()
        with self.assertLogs("会议记录", level="ERROR") as cm:
            session._append_jsonl({"text": "第一句"})
        self.assertIn("转写落盘失败", "\n".join(cm.output))
        self.assertIsNone(session._jsonl_file)      # 坏句柄关掉置 None
        self.assertEqual(session._jsonl_lost, 1)
        with self.assertNoLogs("会议记录", level="ERROR"):   # 之后静默计数
            for i in range(3):
                session._append_jsonl({"text": f"第{i}句"})
        self.assertEqual(session._jsonl_lost, 4)
        with self.assertLogs("会议记录", level="WARNING") as cm2:
            session._close_jsonl()
        self.assertIn("4 句没能落盘", "\n".join(cm2.output))
        session._close_jsonl()      # 幂等：不再重复汇总


if __name__ == "__main__":
    unittest.main(verbosity=2)
