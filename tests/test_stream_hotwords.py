# -*- coding: utf-8 -*-
"""流式热词注入单测：corpus.context 的线格式、带/不带两种请求形态、注入前防御。

覆盖：① corpus_context 是官方「热词直传」形态 {"hotwords":[{"word":…}]}；
② build_full_request 帧解出来能拿到同样的 request.corpus.context；
③ 热词为空 → request 里没有 corpus 字段（保持原形态，不回归）；
④ _prepare_hotwords：None/空/只有空白 → 空表，超限 → 截断 + warning，正常 → 原样；
⑤ 注入后 corpus 的 token 估算不超上限。
"""
import gzip
import json
import os
import struct
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import streaming_asr as sa
from hotwords import HOTWORDS_MAX_TOKENS, hotwords_tokens
from streaming_asr import MeetingStreamSession, build_full_request, corpus_context

_CFG = {
    "volc": {"api_key": "test-key"},
    "streaming": {"model_name": "bigmodel", "enable_nonstream": True,
                  "ssd_version": "200", "reconnect_max": 5,
                  "resource_id": "volc.seedasr.sauc.duration",
                  "url": "wss://example.invalid/x"},
    "asr": {"language": "zh-CN", "enable_speaker_info": True, "enable_punc": True,
            "enable_itn": True, "show_utterances": True},
}

_WORDS = ["徐志龙", "飞明", "老顺昌", "优大人", "铂金装"]


def _payload_of(frame):
    """从 full request 帧里取出 JSON payload（header 4 + seq 4 + size 4 + gzip body）。"""
    size = struct.unpack(">I", frame[8:12])[0]
    return json.loads(gzip.decompress(frame[12:12 + size]).decode("utf-8"))


class CorpusFormatTest(unittest.TestCase):
    """①② 线格式 = 官方热词直传形态。"""

    def test_corpus_context_shape(self):
        ctx = json.loads(corpus_context(_WORDS))
        self.assertEqual(ctx, {"hotwords": [{"word": "徐志龙"}, {"word": "飞明"},
                                            {"word": "老顺昌"}, {"word": "优大人"},
                                            {"word": "铂金装"}]})

    def test_frame_carries_context(self):
        request = dict(sa.DEFAULT_REQUEST)
        request["corpus"] = {"context": corpus_context(_WORDS)}
        payload = _payload_of(build_full_request(1, request=request))
        ctx = json.loads(payload["request"]["corpus"]["context"])
        self.assertEqual([w["word"] for w in ctx["hotwords"]], _WORDS)

    def test_no_context_when_no_hotwords(self):
        payload = _payload_of(build_full_request(1, request=dict(sa.DEFAULT_REQUEST)))
        self.assertNotIn("corpus", payload["request"])


class RequestParamsTest(unittest.TestCase):
    """③④⑤ 会话 request 段：有热词才加 corpus，且注入前按上限截断。"""

    def _session(self, hotwords):
        return MeetingStreamSession(_CFG, hotwords=hotwords)

    def test_params_without_hotwords(self):
        params = self._session(None)._request_params()
        self.assertNotIn("corpus", params)
        self.assertEqual(params["model_name"], "bigmodel")
        self.assertTrue(params["enable_nonstream"])

    def test_params_with_hotwords(self):
        params = self._session(_WORDS)._request_params()
        self.assertIn("corpus", params)
        ctx = json.loads(params["corpus"]["context"])
        self.assertEqual([w["word"] for w in ctx["hotwords"]], _WORDS)
        # 注入的语料本身必须在服务端额度内
        self.assertLessEqual(hotwords_tokens(_WORDS), HOTWORDS_MAX_TOKENS)

    def test_blank_and_empty_list_mean_no_corpus(self):
        for hotwords in ([], None, ["", "   "]):
            self.assertNotIn("corpus", self._session(hotwords)._request_params())

    def test_over_limit_is_clamped_with_warning(self):
        huge = [f"很长的专有名词{i}" for i in range(40)]
        with self.assertLogs("会议记录", level="WARNING") as cm:
            session = self._session(huge)
        params = session._request_params()
        words = [w["word"] for w in json.loads(params["corpus"]["context"])["hotwords"]]
        self.assertLess(len(words), len(huge))
        self.assertLessEqual(hotwords_tokens(words), HOTWORDS_MAX_TOKENS)
        self.assertIn("超出上限", "\n".join(cm.output))

    def test_normal_injection_logged(self):
        with self.assertLogs("会议记录", level="INFO") as cm:
            self._session(_WORDS)
        self.assertIn("[热词] 本次识别注入 5 个热词", "\n".join(cm.output))

    def test_words_are_stripped_and_str(self):
        params = self._session(["  徐志龙 ", "", "飞明"])._request_params()
        words = [w["word"] for w in json.loads(params["corpus"]["context"])["hotwords"]]
        self.assertEqual(words, ["徐志龙", "飞明"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
