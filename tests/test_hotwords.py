# -*- coding: utf-8 -*-
"""热词表单测：解析 / token 估算 / 原子写 / 上限拦截 / 注入前截断。

覆盖：① 解析忽略空行与 # 注释；② 估算公式（中文×1.5 + 英文数字词×1.5 + 其他×1.0）；
③ 首次运行用示例词创建、保存后能读回；④ 超上限拒存且不动原文件、不留下临时文件；
⑤ 写盘失败返回 False 不抛；⑥ 注入前 clamp 截断。
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import hotwords
from hotwords import (DEFAULT_HOTWORDS, HOTWORDS_MAX_TOKENS, clamp_hotwords,
                      estimate_tokens, hotwords_tokens, limit_message,
                      load_hotwords, parse_hotwords, save_hotwords)


class ParseTest(unittest.TestCase):
    """① 一行一词，忽略空行和 # 注释。"""

    def test_parse(self):
        text = "徐志龙\n\n# 这是注释\n  飞明  \n老顺昌\n\n"
        self.assertEqual(parse_hotwords(text), ["徐志龙", "飞明", "老顺昌"])

    def test_parse_empty(self):
        self.assertEqual(parse_hotwords(""), [])
        self.assertEqual(parse_hotwords("\n\n#只有注释\n"), [])


class EstimateTest(unittest.TestCase):
    """② 保守上界公式（宁多不少）。"""

    def test_formula(self):
        self.assertEqual(estimate_tokens("中文"), 3.0)          # 2 字 × 1.5
        self.assertEqual(estimate_tokens("API"), 1.5)           # 1 个英文词 × 1.5
        self.assertEqual(estimate_tokens("中文API"), 4.5)
        self.assertEqual(estimate_tokens("a-b"), 4.0)           # 2 个词 × 1.5 + 1 个其他
        self.assertEqual(estimate_tokens("  \n "), 0.0)         # 空白不算

    def test_hotwords_tokens_is_joined_text(self):
        words = ["徐志龙", "飞明"]
        self.assertEqual(hotwords_tokens(words),
                         estimate_tokens("徐志龙\n飞明"))

    def test_default_words_always_saveable(self):
        """内置示例词必须能存（否则首次运行就撞上限）。"""
        self.assertLessEqual(hotwords_tokens(list(DEFAULT_HOTWORDS)),
                             HOTWORDS_MAX_TOKENS)
        with tempfile.TemporaryDirectory() as tmp:
            self.assertTrue(save_hotwords(list(DEFAULT_HOTWORDS),
                                          os.path.join(tmp, "hotwords.txt")))

    def test_limit_scale(self):
        """60 tokens ≈ 40 个汉字：十来个小词放得下，几十个长词放不下。"""
        ten_short = ["徐志龙", "飞明", "老顺昌", "优大人", "铂金装",
                     "三人团", "货盘", "核销", "拉动", "二部"]
        self.assertLessEqual(hotwords_tokens(ten_short), HOTWORDS_MAX_TOKENS)
        forty_long = [f"很长的专有名词{i}" for i in range(40)]
        self.assertGreater(hotwords_tokens(forty_long), HOTWORDS_MAX_TOKENS)

    def test_limit_message_mentions_numbers(self):
        msg = limit_message([f"词{i}" for i in range(60)])
        self.assertIn(str(HOTWORDS_MAX_TOKENS), msg)
        self.assertIn("请删减", msg)


class FileTest(unittest.TestCase):
    """③④⑤ 读写、上限拦截、原子写。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "hotwords.txt")

    def tearDown(self):
        self.tmp.cleanup()

    def test_first_run_creates_with_examples(self):
        words = load_hotwords(self.path)
        self.assertEqual(words, list(DEFAULT_HOTWORDS))
        self.assertTrue(os.path.exists(self.path))
        # 再读一次拿到的是文件内容（不再是默认值）
        save_hotwords(["自定义词"], self.path)
        self.assertEqual(load_hotwords(self.path), ["自定义词"])

    def test_save_then_load_roundtrip(self):
        self.assertTrue(save_hotwords(["徐志龙", "老顺昌"], self.path))
        with open(self.path, encoding="utf-8") as f:
            self.assertEqual(f.read(), "徐志龙\n老顺昌\n")
        self.assertEqual(load_hotwords(self.path), ["徐志龙", "老顺昌"])

    def test_save_rejects_over_limit(self):
        """超上限：拒写、返回 False、原文件一字不动、不留 .tmp。"""
        save_hotwords(["原始词"], self.path)
        with open(self.path, encoding="utf-8") as f:
            before = f.read()
        huge = [f"很长的专有名词{i}" for i in range(40)]
        self.assertGreater(hotwords_tokens(huge), HOTWORDS_MAX_TOKENS)
        self.assertFalse(save_hotwords(huge, self.path))
        with open(self.path, encoding="utf-8") as f:
            self.assertEqual(f.read(), before)
        self.assertEqual([n for n in os.listdir(self.tmp.name) if n.endswith(".tmp")], [])

    def test_save_empty_clears_file(self):
        save_hotwords(["词"], self.path)
        self.assertTrue(save_hotwords([], self.path))
        self.assertEqual(load_hotwords(self.path), [])

    def test_write_failure_returns_false(self):
        """目标目录不存在 → 返回 False，不抛异常（不能挡启动）。"""
        bad = os.path.join(self.tmp.name, "没有这个目录", "hotwords.txt")
        self.assertFalse(save_hotwords(["词"], bad))

    def test_save_uses_temp_file_and_os_replace(self):
        """原子写：先写同目录临时文件，再 os.replace 到目标（B13 真断言）。"""
        real_replace = os.replace
        calls = []

        def fake_replace(src, dst):
            calls.append((src, dst))
            real_replace(src, dst)

        with mock.patch.object(hotwords.os, "replace", side_effect=fake_replace):
            self.assertTrue(save_hotwords(["徐志龙"], self.path))
        self.assertEqual(len(calls), 1)
        src, dst = calls[0]
        self.assertEqual(dst, self.path)
        self.assertNotEqual(src, self.path)
        self.assertTrue(src.endswith(".tmp"))
        self.assertEqual(os.path.dirname(src), os.path.dirname(self.path))
        self.assertFalse(os.path.exists(src))          # 临时文件被 replace 走了
        self.assertEqual(load_hotwords(self.path), ["徐志龙"])

    def test_no_tmp_files_left(self):
        save_hotwords(["词一", "词二"], self.path)
        self.assertEqual([n for n in os.listdir(self.tmp.name) if ".tmp" in n], [])

    def test_gbk_file_treated_as_empty(self):
        """用户用 GBK 存过：按空表处理，不抛异常（下次保存会写成 UTF-8）。"""
        with open(self.path, "wb") as f:
            f.write("徐志龙\n".encode("gbk"))
        self.assertEqual(load_hotwords(self.path), [])


class ClampTest(unittest.TestCase):
    """⑥ 注入前的第二道保险：超限按顺序截断。"""

    def test_clamp_truncates(self):
        huge = [f"很长的专有名词{i}" for i in range(40)]
        kept = clamp_hotwords(huge)
        self.assertLess(len(kept), len(huge))
        self.assertLessEqual(hotwords_tokens(kept), HOTWORDS_MAX_TOKENS)
        self.assertEqual(kept, huge[:len(kept)])   # 保留前缀

    def test_clamp_keeps_small_list(self):
        words = ["徐志龙", "飞明"]
        self.assertEqual(clamp_hotwords(words), words)

    def test_clamp_drops_even_single_over_limit_word(self):
        """单个巨长词也超限 → 返回空表（宁可这轮不带热词，也不发超限语料）。"""
        self.assertEqual(clamp_hotwords(["超" * 200]), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
