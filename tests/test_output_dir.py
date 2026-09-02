# -*- coding: utf-8 -*-
"""输出目录配置单测：load_config 读 output_dir / pipeline._out_dir 回落 /
save_output_dir 写回后可再读出；另覆盖泛化 save_config_value（全部用临时目录，
不碰真实 config.toml）。
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config_loader import load_config, save_config_value, save_output_dir
from pipeline import _out_dir

_PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class LoadOutputDirTest(unittest.TestCase):
    """① config_loader 读 [app] output_dir：空值 → ""，有值 → 原样。"""

    def test_load_output_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = os.path.join(tmp, "config.toml")
            with open(cfg_path, "w", encoding="utf-8") as f:
                f.write("[app]\noutput_dir = \"\"\n")
            self.assertEqual(load_config(cfg_path)["app"]["output_dir"], "")
            with open(cfg_path, "w", encoding="utf-8") as f:
                f.write("[app]\noutput_dir = \"D:\\\\我的纪要\\\\2026\"\n")
            self.assertEqual(load_config(cfg_path)["app"]["output_dir"],
                             "D:\\我的纪要\\2026")


class OutDirFallbackTest(unittest.TestCase):
    """② pipeline._out_dir 回落：output_dir 空/缺 app 节 → BASE_DIR/输出；
    有值 → 原样返回。"""

    def test_fallback_to_default(self):
        self.assertEqual(_out_dir({"app": {"output_dir": ""}}),
                         os.path.join(_PROJECT_DIR, "输出"))
        self.assertEqual(_out_dir({}), os.path.join(_PROJECT_DIR, "输出"))

    def test_uses_configured_dir(self):
        self.assertEqual(_out_dir({"app": {"output_dir": "D:\\纪要存档"}}),
                         "D:\\纪要存档")


class SaveOutputDirTest(unittest.TestCase):
    """③ save_output_dir 写回后可再读出来（tmp 最小 config.toml）。"""

    def _write(self, cfg_path, text):
        with open(cfg_path, "w", encoding="utf-8") as f:
            f.write(text)

    def test_insert_after_app_section(self):
        """[app] 存在但没有 output_dir 行 → 插到 [app] 之后，其他内容不动。"""
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = os.path.join(tmp, "config.toml")
            self._write(cfg_path, "[deepseek]\napi_key = \"x\"\n\n"
                                  "[app]\ningest_brain_memory = false\n")
            save_output_dir(r"D:\我的纪要", config_path=cfg_path)
            cfg = load_config(cfg_path)
            self.assertEqual(cfg["app"]["output_dir"], r"D:\我的纪要")
            self.assertFalse(cfg["app"]["ingest_brain_memory"])
            self.assertEqual(cfg["deepseek"]["api_key"], "x")  # 其余节不受影响

    def test_replace_existing_line(self):
        """已存在 output_dir 行 → 整行替换，不产生第二行。"""
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = os.path.join(tmp, "config.toml")
            self._write(cfg_path, "[app]\noutput_dir = 'D:\\\\旧的'\n"
                                  "ingest_brain_memory = false\n")
            save_output_dir(r"E:\别处\纪要", config_path=cfg_path)
            with open(cfg_path, encoding="utf-8") as f:
                text = f.read()
            self.assertEqual(text.count("output_dir"), 1)
            cfg = load_config(cfg_path)
            self.assertEqual(cfg["app"]["output_dir"], r"E:\别处\纪要")
            self.assertFalse(cfg["app"]["ingest_brain_memory"])

    def test_path_with_quote_uses_basic_string(self):
        """路径含单引号 → 回退 basic string 写入，读回一致。"""
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = os.path.join(tmp, "config.toml")
            self._write(cfg_path, "[app]\noutput_dir = \"\"\n")
            save_output_dir(r"C:\纪要's备份", config_path=cfg_path)
            self.assertEqual(load_config(cfg_path)["app"]["output_dir"],
                             r"C:\纪要's备份")


class SaveConfigValueTest(unittest.TestCase):
    """④ 泛化 save_config_value：任意节/键的单行写回（tmp 最小 config.toml）。"""

    def _write(self, cfg_path, text):
        with open(cfg_path, "w", encoding="utf-8") as f:
            f.write(text)

    def test_replace_volc_api_key(self):
        """① 在 [volc] 节替换已有 api_key 行，其他行/其他节不动。"""
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = os.path.join(tmp, "config.toml")
            self._write(cfg_path, "[volc]\napi_key = \"old-uuid\"\n"
                                  "resource_id = \"volc.seedasr.auc\"\n\n"
                                  "[deepseek]\napi_key = \"sk-old\"\n")
            save_config_value("volc", "api_key", "new-uuid-value", config_path=cfg_path)
            cfg = load_config(cfg_path)
            self.assertEqual(cfg["volc"]["api_key"], "new-uuid-value")
            self.assertEqual(cfg["volc"]["resource_id"], "volc.seedasr.auc")
            self.assertEqual(cfg["deepseek"]["api_key"], "sk-old")

    def test_insert_key_into_existing_section(self):
        """② 节存在但没有该 key → 插到节标题之后。"""
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = os.path.join(tmp, "config.toml")
            self._write(cfg_path, "[deepseek]\nmodel = \"deepseek-v4-flash\"\n")
            save_config_value("deepseek", "api_key", "sk-test-123", config_path=cfg_path)
            cfg = load_config(cfg_path)
            self.assertEqual(cfg["deepseek"]["api_key"], "sk-test-123")
            self.assertEqual(cfg["deepseek"]["model"], "deepseek-v4-flash")

    def test_append_section_when_missing(self):
        """节都没有 → 文末追加节标题 + 该行，仍可读回。"""
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = os.path.join(tmp, "config.toml")
            self._write(cfg_path, "[deepseek]\nmodel = \"x\"\n")
            save_config_value("app", "output_dir", r"D:\新目录", config_path=cfg_path)
            cfg = load_config(cfg_path)
            self.assertEqual(cfg["app"]["output_dir"], r"D:\新目录")
            self.assertEqual(cfg["deepseek"]["model"], "x")

    def test_special_chars_roundtrip(self):
        """③ 含引号/反斜杠的值 → basic string 写回，读回一致。"""
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = os.path.join(tmp, "config.toml")
            self._write(cfg_path, "[volc]\napi_key = \"\"\n")
            weird = "sk-'a\"b\\c'"
            save_config_value("volc", "api_key", weird, config_path=cfg_path)
            self.assertEqual(load_config(cfg_path)["volc"]["api_key"], weird)


if __name__ == "__main__":
    unittest.main(verbosity=2)
