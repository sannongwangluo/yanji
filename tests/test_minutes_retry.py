# -*- coding: utf-8 -*-
"""generate_minutes 自动重试单测：mock _post_chat（不碰网络）验证重试策略。

覆盖：① 首次空内容 → 重试成功；② 连续瞬态失败 → 抛中文 RuntimeError 且共
3 次尝试；③ 401/403 鉴权失败不重试（只调用 1 次）；④ 非瞬态错误（其他
4xx / 解析失败）不重试。
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import minutes_llm
from minutes_llm import _AuthError, _TransientError, generate_minutes

_CFG = {"deepseek": {"api_key": "test-key", "model": "deepseek-v4-flash",
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
