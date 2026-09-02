# -*- coding: utf-8 -*-
"""会议纪要 Markdown → docx（python-docx）。

排版只做三档：标题（#/##/###）/ 正文 / 列表（- 无序、1. 有序），不做复杂渲染。
文件名 会议纪要_YYYYMMDD_HHMM.docx，存在项目 输出/ 目录；页脚写生成时间。
"""
import os
import re
import time

from docx import Document
from docx.shared import Pt

_BULLET_RE = re.compile(r"^\s*[-*•]\s+(.+)$")
_NUMBER_RE = re.compile(r"^\s*\d+[.、）)]\s*(.+)$")


def _plain(text):
    """剥掉行内残留符号（反引号/下划线强调），供标题用。"""
    return text.replace("`", "").replace("__", "").replace("**", "")


def _add_text_paragraph(doc, text, style=None):
    """正文/列表段落：支持 **粗体** 行内标记，其余符号剥掉。"""
    p = doc.add_paragraph(style=style)
    bold = False
    for seg in text.split("**"):
        seg = seg.replace("`", "").replace("__", "")
        if not seg:
            continue
        run = p.add_run(seg)
        run.bold = bold
        bold = not bold
    if not p.runs:
        p.add_run("")
    return p


def _render_markdown(doc, md):
    """Markdown 行 → docx 段落（标题/正文/列表三档）。"""
    for raw in md.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        if line.startswith("### "):
            doc.add_heading(_plain(line[4:].strip()), level=3)
        elif line.startswith("## "):
            doc.add_heading(_plain(line[3:].strip()), level=2)
        elif line.startswith("# "):
            doc.add_heading(_plain(line[2:].strip()), level=1)
        else:
            m = _BULLET_RE.match(line)
            if m:
                _add_text_paragraph(doc, m.group(1), style="List Bullet")
                continue
            m = _NUMBER_RE.match(line)
            if m:
                _add_text_paragraph(doc, m.group(1), style="List Number")
                continue
            _add_text_paragraph(doc, line.strip())


def save_minutes_docx(markdown, out_dir):
    """把纪要 Markdown 存成 docx，返回文件绝对路径。同名自动加序号。"""
    now = time.localtime()
    stamp = time.strftime("%Y%m%d_%H%M", now)
    full = time.strftime("%Y年%m月%d日 %H:%M", now)

    doc = Document()
    doc.add_heading("会议纪要", level=0)
    meta = doc.add_paragraph()
    run = meta.add_run(f"生成时间：{full}")
    run.font.size = Pt(10)
    _render_markdown(doc, markdown)
    footer = doc.sections[0].footer
    footer.paragraphs[0].text = f"生成时间：{full}"

    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"会议纪要_{stamp}.docx")
    n = 2
    while os.path.exists(path):
        path = os.path.join(out_dir, f"会议纪要_{stamp}_{n}.docx")
        n += 1
    doc.save(path)
    return path
