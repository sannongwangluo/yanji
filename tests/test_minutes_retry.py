# -*- coding: utf-8 -*-
"""generate_minutes 自动重试单测：mock _post_chat（不碰网络）验证重试策略。

覆盖：① 首次空内容 → 重试成功；② 连续瞬态失败 → 抛中文 RuntimeError 且共
3 次尝试；③ 401/403 鉴权失败不重试（只调用 1 次）；④ 非瞬态错误（其他
4xx / 解析失败）不重试；⑤ 输出被 max_tokens 截断（确定性失败）不重试；
⑥ _post_chat 解析 finish_reason：length+空内容 → 不重试的中文截断错误，
普通空内容 → 可重试的 _TransientError，正常内容 → 原样返回；
⑦ 真分类（在 urllib 层做假、跑真 _post_chat）：HTTP 5xx/网络错 → _TransientError、
401/403 → _AuthError、其他 4xx → 普通 RuntimeError；
⑧ 真链路重试次数：5xx 调 3 次、401 只调 1 次、首次 5xx 后重试能成功。
"""
import io
import json
import os
import sys
import unittest
import urllib.error
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import minutes_llm
from minutes_llm import _AuthError, _TransientError, generate_minutes

_CFG = {"deepseek": {"api_key": "test-key", "model": "deepseek-flash",
                     "endpoint": "https://api.deepseek.com/v1/chat/completions"}}
_UTTS = [
    {"speaker": "0", "text": "我是张三", "speaker_name": "张三"},
    {"speaker": "0", "text": "今天讨论下季度预算", "speaker_name": "张三"},
]


class RetryTest(unittest.TestCase):
    def test_empty_content_retries_then_succeeds(self):
        """① 首次返回空内容（瞬态）→ 第 2 次尝试成功。"""
        post_mock = mock.Mock(side_effect=[
            _TransientError("DeepSeek 返回了空内容。"),
            "# 会议主题\n## 参会人\n张三",
        ])
        with mock.patch.object(minutes_llm, "_post_chat", post_mock), \
                mock.patch.object(minutes_llm.time, "sleep") as sleep_mock, \
                self.assertLogs("会议记录", level="WARNING") as cm:
            result = generate_minutes(_CFG, _UTTS)
        self.assertEqual(result, "# 会议主题\n## 参会人\n张三")
        self.assertEqual(post_mock.call_count, 2)
        sleep_mock.assert_called_once_with(minutes_llm.RETRY_DELAY)
        self.assertIn("第 1/3 次调用失败", "\n".join(cm.output))

    def test_transient_failures_raise_after_three_attempts(self):
        """② 空内容/网络/5xx 连续失败 → 3 次尝试后抛中文 RuntimeError。"""
        post_mock = mock.Mock(side_effect=[
            _TransientError("DeepSeek 返回了空内容。"),
            _TransientError("调用 DeepSeek 网络失败：timeout"),
            _TransientError("调用 DeepSeek 失败（HTTP 500）。"),
        ])
        with mock.patch.object(minutes_llm, "_post_chat", post_mock), \
                mock.patch.object(minutes_llm.time, "sleep") as sleep_mock, \
                self.assertLogs("会议记录", level="WARNING") as cm:
            with self.assertRaises(RuntimeError) as ctx:
                generate_minutes(_CFG, _UTTS)
        self.assertEqual(post_mock.call_count, 3)
        self.assertIn("已自动重试 2 次仍失败", str(ctx.exception))
        self.assertEqual(sleep_mock.call_count, 2)  # 前 2 次失败各等 3 秒
        output = "\n".join(cm.output)
        self.assertIn("第 1/3 次调用失败", output)
        self.assertIn("第 2/3 次调用失败", output)

    def test_auth_error_no_retry(self):
        """③ 401/403（key 无效/没额度）不重试：只调用 1 次，直接抛中文错误。"""
        post_mock = mock.Mock(side_effect=_AuthError(
            "DeepSeek API Key 无效或没额度了。\n"
            "请检查 config.toml 的 [deepseek] api_key（或环境变量 DEEPSEEK_API_KEY）。"))
        with mock.patch.object(minutes_llm, "_post_chat", post_mock), \
                mock.patch.object(minutes_llm.time, "sleep") as sleep_mock:
            with self.assertRaises(RuntimeError) as ctx:
                generate_minutes(_CFG, _UTTS)
        self.assertEqual(post_mock.call_count, 1)
        self.assertIn("Key 无效或没额度", str(ctx.exception))
        sleep_mock.assert_not_called()

    def test_non_transient_error_no_retry(self):
        """④ 其他 4xx（非 401/403）不重试：只调用 1 次，直接抛中文错误。"""
        post_mock = mock.Mock(side_effect=RuntimeError("调用 DeepSeek 失败（HTTP 400）。"))
        with mock.patch.object(minutes_llm, "_post_chat", post_mock), \
                mock.patch.object(minutes_llm.time, "sleep") as sleep_mock:
            with self.assertRaises(RuntimeError) as ctx:
                generate_minutes(_CFG, _UTTS)
        self.assertEqual(post_mock.call_count, 1)
        self.assertIn("HTTP 400", str(ctx.exception))
        sleep_mock.assert_not_called()

    def test_truncated_output_no_retry(self):
        """⑤ 输出被 max_tokens 截断（finish_reason=length 且 content 空）→ 不重试。

        这是确定性失败：同样的请求只会同样截断，重试 2 次纯浪费时间。
        """
        post_mock = mock.Mock(side_effect=RuntimeError(minutes_llm._TRUNCATED_MSG))
        with mock.patch.object(minutes_llm, "_post_chat", post_mock), \
                mock.patch.object(minutes_llm.time, "sleep") as sleep_mock:
            with self.assertRaises(RuntimeError) as ctx:
                generate_minutes(_CFG, _UTTS)
        self.assertEqual(post_mock.call_count, 1)
        self.assertIn("max_tokens", str(ctx.exception))
        self.assertIn("截断", str(ctx.exception))
        sleep_mock.assert_not_called()


class _FakeResponse:
    """_post_chat 里 `with opener.open(...) as resp` 的假响应。"""

    def __init__(self, body):
        self._body = json.dumps(body, ensure_ascii=False).encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeOpener:
    def __init__(self, body):
        self._body = body

    def open(self, req, timeout=None):
        return _FakeResponse(self._body)


class PostChatParsingTest(unittest.TestCase):
    """⑥ _post_chat 的 finish_reason 解析（假 opener，不碰网络）。"""

    _DS = {"api_key": "test-key", "endpoint": "https://api.deepseek.com/v1/chat/completions"}
    _PAYLOAD = {"model": "deepseek-flash", "messages": [], "max_tokens": 32768}

    def _body(self, finish_reason, content):
        return {"choices": [{"finish_reason": finish_reason,
                             "message": {"content": content,
                                         "reasoning_content": "思考…"}}],
                "usage": {"completion_tokens_details": {"reasoning_tokens": 4099}}}

    def _post(self, body):
        with mock.patch.object(minutes_llm.urllib.request, "build_opener",
                               return_value=_FakeOpener(body)):
            return minutes_llm._post_chat(self._DS, self._PAYLOAD)

    def test_length_with_empty_content_raises_truncated(self):
        """finish_reason=length + content 空 → 不重试的截断 RuntimeError（非瞬态）。"""
        with self.assertRaises(RuntimeError) as ctx:
            self._post(self._body("length", ""))
        self.assertNotIsInstance(ctx.exception, _TransientError)
        self.assertIn("max_tokens", str(ctx.exception))
        self.assertIn("思考", str(ctx.exception))

    def test_stop_with_empty_content_is_transient(self):
        """finish_reason=stop + content 空 → 仍是可重试的 _TransientError。"""
        with self.assertRaises(_TransientError):
            self._post(self._body("stop", ""))

    def test_normal_content_returned(self):
        """正常返回：content 原样返回（首尾空白剥掉）。"""
        self.assertEqual(self._post(self._body("stop", "  # 会议主题\n张三  ")),
                         "# 会议主题\n张三")


def _http_error(code):
    """urllib 层真实会抛的 HTTPError（_post_chat 只看 code，响应体给空的）。"""
    return urllib.error.HTTPError(
        "https://api.deepseek.com/v1/chat/completions", code,
        "error", {}, io.BytesIO(b""))


class _ErrorOpener:
    """open() 总是抛指定异常的假 opener（在 urllib 层做假）。"""

    def __init__(self, exc):
        self._exc = exc
        self.calls = 0

    def open(self, req, timeout=None):
        self.calls += 1
        raise self._exc


class _FlakyOpener:
    """前 fail_times 次抛异常，之后返回正常响应（证明重试真能救回来）。"""

    def __init__(self, exc, body, fail_times=1):
        self._exc = exc
        self._body = body
        self._fail_times = fail_times
        self.calls = 0

    def open(self, req, timeout=None):
        self.calls += 1
        if self.calls <= self._fail_times:
            raise self._exc
        return _FakeResponse(self._body)


class _ClosesHttpErrors:
    """HTTPError 内部套着 tempfile 包装，不 close 会在 GC 时喷 ResourceWarning。"""

    def setUp(self):
        super().setUp()
        self._http_errors = []

    def tearDown(self):
        for exc in self._http_errors:
            exc.close()
        super().tearDown()

    def _http_error(self, code):
        exc = _http_error(code)
        self._http_errors.append(exc)
        return exc


class ErrorClassificationTest(_ClosesHttpErrors, unittest.TestCase):
    """⑦ 真分类：HTTP 状态码 / 网络错误由真 _post_chat 自己判出异常类型。

    上面的重试测试把现成的 _TransientError/_AuthError 实例喂给 mock 的
    _post_chat，从没让真 _post_chat 走 HTTP/网络分支——把 `e.code >= 500` 改成
    `>= 600`、`in (401, 403)` 改坏，那些测试照样全绿（变异实验实测）。这里从
    urllib 入口做假，让分类代码真跑一遍。
    """

    _DS = {"api_key": "test-key",
           "endpoint": "https://api.deepseek.com/v1/chat/completions"}
    _PAYLOAD = {"model": "deepseek-flash", "messages": [], "max_tokens": 32768}

    def _raised(self, exc):
        """跑一次真 _post_chat，返回它抛出来的异常（并确认只调了 1 次）。"""
        opener = _ErrorOpener(exc)
        with mock.patch.object(minutes_llm.urllib.request, "build_opener",
                               return_value=opener):
            with self.assertRaises(Exception) as ctx:
                minutes_llm._post_chat(self._DS, self._PAYLOAD)
        self.assertEqual(opener.calls, 1)
        return ctx.exception

    def test_5xx_is_transient(self):
        """HTTP 5xx → _TransientError（可重试），带状态码。"""
        for code in (500, 502, 503):
            with self.subTest(code=code):
                exc = self._raised(self._http_error(code))
                self.assertIsInstance(exc, _TransientError)
                self.assertIn(f"HTTP {code}", str(exc))
                self.assertNotIn("max_tokens", str(exc))  # 别误判成截断

    def test_401_403_is_auth_error(self):
        """401/403（key 无效/没额度）→ _AuthError（不重试），中文提示不变。"""
        for code in (401, 403):
            with self.subTest(code=code):
                exc = self._raised(self._http_error(code))
                self.assertIsInstance(exc, _AuthError)
                self.assertNotIsInstance(exc, _TransientError)
                self.assertIn("API Key 无效或没额度", str(exc))

    def test_other_4xx_is_plain_error(self):
        """其他 4xx（400/422/429）→ 普通 RuntimeError：既不重试也不是鉴权错。"""
        for code in (400, 422, 429):
            with self.subTest(code=code):
                exc = self._raised(self._http_error(code))
                self.assertIsInstance(exc, RuntimeError)
                self.assertNotIsInstance(exc, (_TransientError, _AuthError))
                self.assertIn(f"HTTP {code}", str(exc))

    def test_network_error_is_transient(self):
        """URLError（连不上/超时）→ _TransientError（可重试）。"""
        exc = self._raised(urllib.error.URLError("connection refused"))
        self.assertIsInstance(exc, _TransientError)
        self.assertIn("网络失败", str(exc))


class RetryThroughRealPostChatTest(_ClosesHttpErrors, unittest.TestCase):
    """⑧ 真链路重试次数：让真 _post_chat 在 urllib 层失败，数 _chat 的尝试次数。"""

    def test_5xx_retries_three_attempts(self):
        """HTTP 500 → 共 3 次尝试（间隔 3 秒），最后抛中文 RuntimeError。"""
        opener = _ErrorOpener(self._http_error(500))
        with mock.patch.object(minutes_llm.urllib.request, "build_opener",
                               return_value=opener), \
                mock.patch.object(minutes_llm.time, "sleep") as sleep_mock:
            with self.assertRaises(RuntimeError) as ctx:
                generate_minutes(_CFG, _UTTS)
        self.assertEqual(opener.calls, 3)
        self.assertEqual(sleep_mock.call_count, 2)
        sleep_mock.assert_called_with(minutes_llm.RETRY_DELAY)
        self.assertIn("已自动重试 2 次仍失败", str(ctx.exception))

    def test_401_no_retry(self):
        """HTTP 401 → 只调 1 次、不睡、直接抛中文鉴权错误。"""
        opener = _ErrorOpener(self._http_error(401))
        with mock.patch.object(minutes_llm.urllib.request, "build_opener",
                               return_value=opener), \
                mock.patch.object(minutes_llm.time, "sleep") as sleep_mock:
            with self.assertRaises(RuntimeError) as ctx:
                generate_minutes(_CFG, _UTTS)
        self.assertEqual(opener.calls, 1)
        sleep_mock.assert_not_called()
        self.assertIn("Key 无效或没额度", str(ctx.exception))

    def test_500_then_success(self):
        """首次 500（瞬态）→ 重试一次拿到纪要正文（整条链路走通）。"""
        opener = _FlakyOpener(self._http_error(500), {
            "choices": [{"finish_reason": "stop",
                         "message": {"content": "# 智能纪要：示例"}}]})
        with mock.patch.object(minutes_llm.urllib.request, "build_opener",
                               return_value=opener), \
                mock.patch.object(minutes_llm.time, "sleep") as sleep_mock:
            result = generate_minutes(_CFG, _UTTS)
        self.assertEqual(result, "# 智能纪要：示例")
        self.assertEqual(opener.calls, 2)
        sleep_mock.assert_called_once_with(minutes_llm.RETRY_DELAY)


if __name__ == "__main__":
    unittest.main(verbosity=2)
