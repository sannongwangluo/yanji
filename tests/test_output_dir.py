# -*- coding: utf-8 -*-
"""输出目录配置单测：load_config 读 output_dir / pipeline._out_dir 回落 /
save_output_dir 写回后可再读出（全部用临时目录，不碰真实 config.toml）。
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config_loader import load_config, save_output_dir
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
