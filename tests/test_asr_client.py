# -*- coding: utf-8 -*-
"""录音文件识别（file-mode 备用路径）单测：说话人口径、轮询错误分类、
TOS 删除、重试不再白等。全部假 Session / 假响应，不碰网络。

覆盖：parse_utterances 五路 speaker 字段（实测字段是 additions.speaker_id，
与流式主路径同口径）+ 缺省「0」+ 空句跳过 + result.text 兜底；
_poll 把 550xxxx / HTTP 5xx 当可重试（计数与退避复用网络异常那套），
45000030 等业务错误码立即失败；_delete_object 非 2xx 只 warning 不炸；
_upload / _submit 最后一次失败不再 sleep（白等 14 秒的旧行为）。
"""
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asr_client
from asr_client import AsrClient, parse_utterances

_CFG = {
    "volc": {"api_key": "test-key", "resource_id": "volc.seedasr.auc"},
    "tos": {"access_key_id": "AKLTtest", "secret_access_key": "test-secret",
            "bucket": "test-bucket", "region": "cn-beijing", "endpoint": ""},
    "asr": {"language": "zh-CN", "enable_speaker_info": True,
            "show_utterances": True, "enable_punc": True, "enable_itn": True,
            "enable_ddc": True, "poll_interval_sec": 5, "poll_timeout_sec": 1500},
    "app": {"delete_audio_after_asr": True},
}


class _Resp:
    """假 requests.Response（只带被测代码用到的字段）。"""

    def __init__(self, status_code=200, headers=None, json_data=None, text=""):
        self.status_code = status_code
        self.headers = headers or {}
        self._json = json_data if json_data is not None else {}
        self.text = text

    def json(self):
        return self._json


class _FakeClock:
    """只劫持 time.sleep（记账不真等），其余属性（monotonic/strftime）走真实时钟。"""

    def __init__(self):
        self.sleeps = []

    def sleep(self, sec):
        self.sleeps.append(sec)

    def __getattr__(self, name):
        return getattr(time, name)


class _FakeSess:
    """假 requests.Session：按脚本依次返回响应（元素是异常则抛出），并记录调用。"""

    def __init__(self, results=None):
        self.results = list(results or [])
        self.calls = []

    def _next(self):
        item = self.results.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def post(self, url, **kwargs):
        self.calls.append(("post", url))
        return self._next()

    def put(self, url, **kwargs):
        self.calls.append(("put", url))
        return self._next()

    def delete(self, url, **kwargs):
        self.calls.append(("delete", url))
        return self._next()


def _client(sess):
    c = AsrClient(_CFG)
    c._sess = sess
    return c


def _resp(status_code, code="", message="", json_data=None):
    return _Resp(status_code,
                 {"X-Api-Status-Code": code, "X-Api-Message": message},
                 json_data=json_data)


class ParseUtterancesTest(unittest.TestCase):
    """speaker 口径与流式主路径同源（streaming_asr._speaker_of）。"""

    def _one(self, extra):
        utt = {"text": "你好", "start_time": 0, "end_time": 900}
        utt.update(extra)
        return parse_utterances({"result": {"utterances": [utt]}})[0]

    def test_speaker_field_routes(self):
        for extra, expect in [({"speaker": "1"}, "1"),
                              ({"speaker_id": "S2"}, "S2"),
                              ({"spk": "3"}, "3"),
                              ({"additions": {"speaker": "5"}}, "5"),
                              ({"additions": {"speaker_id": "6"}}, "6")]:
            self.assertEqual(self._one(extra)["speaker"], expect, extra)

    def test_multi_speaker_not_collapsed(self):
        """回归：原来只认 speaker/additions.speaker，多人会塌成全是「说话人0」。"""
        data = {"result": {"utterances": [
            {"text": "我是张三", "start_time": 0, "end_time": 100,
             "additions": {"speaker_id": "0"}},
            {"text": "我是李四", "start_time": 100, "end_time": 200,
             "additions": {"speaker_id": "1"}}]}}
        self.assertEqual([u["speaker"] for u in parse_utterances(data)], ["0", "1"])

    def test_speaker_default_zero(self):
        self.assertEqual(self._one({})["speaker"], "0")

    def test_empty_utterance_skipped(self):
        data = {"result": {"utterances": [{"text": "  "}, {"text": "好"}]}}
        self.assertEqual([u["text"] for u in parse_utterances(data)], ["好"])

    def test_non_string_text_skipped(self):
        data = {"result": {"utterances": [{"text": ["畸形"]}, {"text": "好"}]}}
        self.assertEqual([u["text"] for u in parse_utterances(data)], ["好"])

    def test_result_text_fallback(self):
        utts = parse_utterances({"result": {"text": " 只有全文 "}})
        self.assertEqual(utts, [{"speaker": "0", "text": "只有全文",
                                 "start_time": 0, "end_time": None}])


class PollRetryTest(unittest.TestCase):
    """_poll 错误分类：550xxxx / HTTP 5xx 可重试，业务错误码立即失败。"""

    def test_550_retries_then_succeeds(self):
        sess = _FakeSess([_resp(200, "55000031", "服务繁忙"),
                          _resp(200, "20000000", json_data={"result": {"text": "好"}})])
        clock = _FakeClock()
        with mock.patch.object(asr_client, "time", clock):
            data = _client(sess)._poll("rid")
        self.assertEqual(data, {"result": {"text": "好"}})
        self.assertEqual(len(sess.calls), 2)
        self.assertEqual(clock.sleeps, [2])       # 2^n 退避，与网络异常同口径

    def test_45000030_is_fatal_immediately(self):
        sess = _FakeSess([_resp(200, "45000030", "requested resource not granted")])
        clock = _FakeClock()
        with mock.patch.object(asr_client, "time", clock):
            with self.assertRaises(RuntimeError) as cm:
                _client(sess)._poll("rid")
        self.assertEqual(len(sess.calls), 1)
        self.assertEqual(clock.sleeps, [])
        self.assertIn("45000030", str(cm.exception))
        self.assertIn("还没开通", str(cm.exception))   # ERROR_HINTS 大白话提示带上

    def test_http_500_retries(self):
        sess = _FakeSess([_resp(500), _resp(503),
                          _resp(200, "20000000", json_data={"result": {}})])
        clock = _FakeClock()
        with mock.patch.object(asr_client, "time", clock):
            _client(sess)._poll("rid")
        self.assertEqual(clock.sleeps, [2, 4])

    def test_network_error_and_550_share_counter(self):
        sess = _FakeSess([asr_client.requests.ConnectionError("断网"),
                          _resp(200, "55000031", "服务繁忙"),
                          _resp(200, "20000000", json_data={"result": {}})])
        clock = _FakeClock()
        with mock.patch.object(asr_client, "time", clock):
            _client(sess)._poll("rid")
        self.assertEqual(clock.sleeps, [2, 4])

    def test_retry_cap_then_fail(self):
        from asr_client import _POLL_RETRY_MAX
        sess = _FakeSess([_resp(200, "55000031", "服务繁忙")] * _POLL_RETRY_MAX)
        clock = _FakeClock()
        with mock.patch.object(asr_client, "time", clock):
            with self.assertRaises(RuntimeError) as cm:
                _client(sess)._poll("rid")
        self.assertEqual(len(sess.calls), _POLL_RETRY_MAX)
        self.assertEqual(clock.sleeps, [2, 4, 8, 16])   # 第 5 次直接抛，不再等
        self.assertIn("连续失败", str(cm.exception))


class DeleteObjectTest(unittest.TestCase):
    def test_2xx_logs_deleted(self):
        sess = _FakeSess([_Resp(204)])
        with self.assertLogs("会议记录", level="INFO") as cm:
            _client(sess)._delete_object("https://test-bucket.tos-cn-beijing.volces.com/x.wav")
        self.assertIn("已从 TOS 删除音频", "\n".join(cm.output))

    def test_non_2xx_warns_only(self):
        sess = _FakeSess([_Resp(403, text="denied")])
        with self.assertLogs("会议记录", level="WARNING") as cm:
            _client(sess)._delete_object("https://test-bucket.tos-cn-beijing.volces.com/x.wav")
        output = "\n".join(cm.output)
        self.assertIn("删除音频失败", output)
        self.assertIn("403", output)
        self.assertNotIn("已从 TOS 删除音频", output)


class RetryNoSleepOnLastAttemptTest(unittest.TestCase):
    """最后一次失败不再 sleep（旧行为：3 次失败各白等 2+4+8=14 秒）。"""

    def test_submit_last_failure_no_sleep(self):
        sess = _FakeSess([_resp(200, "55000031", "服务繁忙")] * 3)
        clock = _FakeClock()
        with mock.patch.object(asr_client, "time", clock):
            with self.assertRaises(RuntimeError):
                _client(sess)._submit("https://x/y.wav", "wav")
        self.assertEqual(len(sess.calls), 3)
        self.assertEqual(clock.sleeps, [2, 4])

    def test_upload_last_failure_no_sleep(self):
        fd, path = tempfile.mkstemp(suffix=".wav")
        os.close(fd)
        try:
            sess = _FakeSess([_Resp(500, text="boom")] * 3)
            clock = _FakeClock()
            with mock.patch.object(asr_client, "time", clock):
                with self.assertRaises(RuntimeError):
                    _client(sess)._upload(path, "wav")
            self.assertEqual(len(sess.calls), 3)
            self.assertEqual(clock.sleeps, [2, 4])
        finally:
            os.remove(path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
