# -*- coding: utf-8 -*-
"""纪要成对导出单测：save_minutes_pair 生成 docx+md 同名成对（tmp 目录，
不碰真实 输出/ 目录）。
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import docx_writer
from docx_writer import _next_base_name, save_minutes_pair

_MD = "# 会议主题\n## 参会人\n张三、李四\n\n- 决议一\n- 决议二\n"


class SaveMinutesPairTest(unittest.TestCase):
    def test_both_files_same_basename(self):
        """① docx 与 md 都生成，文件名基相同。"""
        with tempfile.TemporaryDirectory() as tmp:
            docx_path, md_path = save_minutes_pair(_MD, tmp)
            self.assertTrue(os.path.exists(docx_path))
            self.assertTrue(os.path.exists(md_path))
            base_docx = os.path.splitext(os.path.basename(docx_path))[0]
            base_md = os.path.splitext(os.path.basename(md_path))[0]
            self.assertEqual(base_docx, base_md)
            self.assertTrue(base_docx.startswith("会议纪要_"))
            self.assertTrue(docx_path.endswith(".docx"))
            self.assertTrue(md_path.endswith(".md"))

    def test_md_content_exact(self):
        """② md 内容与输入 Markdown 逐字节一致（utf-8、无 BOM、无多余头）。"""
        with tempfile.TemporaryDirectory() as tmp:
            _, md_path = save_minutes_pair(_MD, tmp)
            with open(md_path, "rb") as f:
                raw = f.read()
            self.assertEqual(raw, _MD.encode("utf-8"))

    def test_conflict_same_sequence(self):
        """③ 同名冲突时 docx/md 落同一序号（_2），基名一致。"""
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(docx_writer.time, "strftime",
                                   return_value="20260902_1000"):
                # 手工占位一个同 stamp 的 docx（md 不存在），冲突应双双落 _2
                with open(os.path.join(tmp, "会议纪要_20260902_1000.docx"),
                          "wb") as f:
                    f.write(b"placeholder")
                docx_path, md_path = save_minutes_pair(_MD, tmp)
            base = os.path.splitext(os.path.basename(docx_path))[0]
            self.assertEqual(base, os.path.splitext(os.path.basename(md_path))[0])
            self.assertEqual(base, "会议纪要_20260902_1000_2")
            self.assertTrue(os.path.exists(docx_path))
            self.assertTrue(os.path.exists(md_path))

    def test_next_base_skips_md_conflict(self):
        """补充：md 先占用时同样跳过该基名，两扩展名共用同一序号空间。"""
        with tempfile.TemporaryDirectory() as tmp:
            stamp = "20260902_1200"
            with open(os.path.join(tmp, f"会议纪要_{stamp}.md"), "wb") as f:
                f.write(b"placeholder")
            base = _next_base_name(tmp, stamp)
            self.assertEqual(os.path.basename(base), "会议纪要_20260902_1200_2")


if __name__ == "__main__":
    unittest.main(verbosity=2)
