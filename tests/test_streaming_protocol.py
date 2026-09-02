# -*- coding: utf-8 -*-
"""流式协议单测：二进制帧打包/解析 roundtrip + definite 分句抽取（全部假数据，
不碰网络）。

覆盖：4 字节头布局、full client request 剥壳校验、audio 帧正/负 seq、
服务端帧解析 roundtrip、error 帧错误码、残缺帧容错、definite 过滤、
speaker 字段名多路兼容、去重、float32→s16le 转换。
"""
import gzip
import json
import os
import struct
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np

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
    build_audio_request,
    build_full_request,
    build_header,
    definite_utterances,
    filter_new_utterances,
    frame_to_pcm_bytes,
    parse_response,
    _speaker_of,
)


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
    def test_dedup_by_utterance_id(self):
        u1 = {"text": "你好", "speaker": "1", "utterance_id": "u1"}
        u1b = {"text": "你好，大家好", "speaker": "1", "utterance_id": "u1"}  # 修订版同 id
        u2 = {"text": "再见", "speaker": "1", "utterance_id": "u2"}
        seen = set()
        new1, seen = filter_new_utterances([u1, u2], seen)
        self.assertEqual(len(new1), 2)
        new2, _ = filter_new_utterances([u1b, u2], seen)
        self.assertEqual(new2, [])

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
