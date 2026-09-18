# -*- coding: utf-8 -*-
"""会议纪要 Markdown → docx（python-docx）+ 同名 Markdown 原样落盘。

排版（2026-09-17 翻案：旧定版「只做三档、prompt 禁表格」作废）——对齐钉钉「智能纪要」
（通义妙记）导出样例的卡片样式，色值/字号全部来自样例 docx 的 XML 实测（见 archive.md）：
- 标题：# 26pt 加粗 / ## 18pt 加粗 / ### 12pt 加粗 / #### 11pt 加粗，黑色，不用内置
  Heading 样式（避免默认蓝绿）；
- 正文/列表：# 11pt，等线(eastAsia)+Arial(西文)，1.2 倍行距，段前段后 6pt；行内 **粗体**；
- 卡片：Markdown 引用块（连续 `> ` 行）→ **单格 1×1 表格**，只留左侧 2.25pt 竖线
  （left single sz=18 color=BBBFC4）、无底纹、文字 646a73 灰、内边距 60/120/30/120 dxa
  ——这是「卡片」的单一来源标记（样例的头部信息卡与智能章节摘要卡都是这个形态）；
- 表格：Markdown 表格 → 真 docx 表格（Table Grid 细边框，表头加粗 + DEE0E3 浅底纹）；
  按 GFM 容错：行尾竖线可省、分隔行 1 个短横也算、`\\|` 与行内代码里的竖线不当分隔符。
成对导出（save_minutes_pair）：会议纪要_YYYYMMDD_HHMM.docx / .md 同名成对，存输出目录；
docx 页脚写生成时间，md 为 generate_minutes 返回的原始 Markdown（含控制字符也照原样，
XML 非法的控制字符只在渲染进 docx 时滤掉——见 _plain）。
三件套（2026-09-17 新增）：save_pdf_from_docx 用本机 WPS/Word 的 COM 把 docx 转成同名 PDF
（样式零失真，不另写排版引擎）；**best-effort**——没装 WPS/Word、没装 pywin32、COM 报错
一律记日志返回 None，绝不影响 docx/md 主流程；COM 段跑在独立 daemon 线程里，
主叫线程最多等 PDF_TIMEOUT_SEC（120 秒），WPS 弹窗/RPC 卡死也不会挂住管线。
save_minutes_docx 保留（只存 docx，兼容旧调用）。
"""
import logging
import os
import re
import threading
import time

from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, RGBColor

log = logging.getLogger("会议记录")

# ---- 钉钉「智能纪要」样例实测样式（2026-09-17 侦察，定版依据）----
# 样例：C:\Users\三农网络\Desktop\智能纪要：电商多平台运营策略复盘讨论 2026年9月2日.docx
FONT_EAST = "等线"          # rFonts eastAsia（样例全篇）
FONT_ASCII = "Arial"        # rFonts ascii/hAnsi/cs（样例全篇）
H1_SIZE = 26                # sz=52：样例大标题「智能纪要：…」
H2_SIZE = 18                # sz=36：样例栏目标题（总结/待办/智能章节…）
H3_SIZE = 12                # 样例同层级正文是 sz=22 加粗，我们留 12pt 保住层级感
H4_SIZE = 11
BODY_SIZE = 11              # sz=22：样例正文与要点
CARD_ACCENT = "BBBFC4"      # 卡片左竖线：left single sz=18 color=BBBFC4
CARD_TEXT = "646a73"        # 卡片文字色：color=646a73
META_TEXT = "8F959E"        # 样例图注灰：color=8F959E
TABLE_HEADER_FILL = "DEE0E3"  # 表头底纹：取样例文档内唯一浅色底纹（dee0e3）
LINE_SPACING = 1.2          # 样例 line=288 lineRule=auto
SPACE_BEFORE = 6            # 样例 before=120（dxa→6pt）
SPACE_AFTER = 6             # 样例 after=120
SECTION_BEFORE = 19         # 样例栏目标题 before=380
SECTION_AFTER = 7           # 样例栏目标题 after=140
CARD_MARGINS = (60, 120, 30, 120)   # tcMar top/left/bottom/right（dxa）
CARD_BAR_SZ = "18"          # 左边框粗细（1/8 pt 单位 → 2.25pt）

_BULLET_RE = re.compile(r"^\s*[-*•]\s+(.+)$")
_NUMBER_RE = re.compile(r"^\s*\d+[.、）)]\s*(.+)$")
_QUOTE_RE = re.compile(r"^\s*>\s?(.*)$")
# 行尾竖线可省（GFM 合法：`| a | b`），行首竖线仍是「这是表格」的判定依据
_TABLE_ROW_RE = re.compile(r"^\s*\|.*$")
_TABLE_SEP_CELL_RE = re.compile(r"^:?-{1,}:?$")
_IMAGE_RE = re.compile(r"^!\[([^\]]*)\]\(([^)]+)\)\s*$")
# XML 1.0 不允许的控制字符（\t\n\r 除外）：python-docx 的 add_run 遇到会抛
# ValueError("All strings must be XML compatible…")，整份 docx 都出不来
_ILLEGAL_XML_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _strip_control(text):
    """滤掉 XML 非法控制字符（docx 渲染的**唯一**入口都走这里）。"""
    return _ILLEGAL_XML_RE.sub("", text)


def _plain(text):
    """剥掉行内残留符号（反引号/下划线强调）+ 滤控制字符，标题与逐段 run 共用。"""
    return _strip_control(text.replace("`", "").replace("__", "").replace("**", ""))


def _style_run(run, size=BODY_SIZE, bold=None, color=None):
    """统一给 run 设字体/字号/颜色（等线 + Arial，色值传 6 位十六进制）。"""
    run.font.size = Pt(size)
    run.font.name = FONT_ASCII
    if bold is not None:
        run.bold = bold
    if color:
        run.font.color.rgb = RGBColor.from_string(color)
    rPr = run._element.get_or_add_rPr()
    rFonts = rPr.find(qn("w:rFonts"))
    if rFonts is None:
        rFonts = OxmlElement("w:rFonts")
        rPr.insert(0, rFonts)
    rFonts.set(qn("w:ascii"), FONT_ASCII)
    rFonts.set(qn("w:hAnsi"), FONT_ASCII)
    rFonts.set(qn("w:cs"), FONT_ASCII)
    rFonts.set(qn("w:eastAsia"), FONT_EAST)
    return run


def _style_para(p, before=SPACE_BEFORE, after=SPACE_AFTER, line=LINE_SPACING):
    pf = p.paragraph_format
    pf.space_before = Pt(before)
    pf.space_after = Pt(after)
    pf.line_spacing = line
    return p


def _add_runs(p, text, size=BODY_SIZE, color=None, base_bold=False):
    """把一段文本写进段落：支持行内 **粗体**，其余符号（反引号/下划线）剥掉。

    粗体按 `**` 切分后的**段序奇偶**判定（第 0、2、4… 段普通，第 1、3… 段加粗）。
    注意不能用「遇段取反」的写法：行首就是 `**` 时切分结果第一段是空串，取反会错位，
    整行粗体/普通反过来（2026-09-17 修：新模板里 **小标题**、**关键决策** 这类行首加粗
    很常见，错位会让整个 docx 的加粗全反）。

    逐段过 `_plain`：它是 docx 文本的单一入口（正文/卡片/表格单元格共用），
    **XML 非法控制字符在这一层滤掉**，md 原文照旧不动（md 只是文本，管不着）。
    """
    for idx, seg in enumerate(text.split("**")):
        seg = _plain(seg)
        if not seg:
            continue
        bold = base_bold if idx % 2 == 0 else not base_bold
        _style_run(p.add_run(seg), size=size, bold=bold, color=color)
    if not p.runs:
        _style_run(p.add_run(""), size=size, color=color)
    return p


def _add_text_paragraph(doc, text, style=None, size=BODY_SIZE, color=None):
    """正文/列表段落：支持 **粗体** 行内标记，其余符号剥掉。"""
    p = doc.add_paragraph(style=style)
    _style_para(p)
    return _add_runs(p, text, size=size, color=color)


def _add_heading(doc, text, size):
    """标题：显式设字体字号加粗（不用内置 Heading 样式，避免默认蓝绿）。"""
    p = doc.add_paragraph()
    _style_para(p, before=SECTION_BEFORE if size == H2_SIZE else SPACE_BEFORE,
                after=SECTION_AFTER if size == H2_SIZE else SPACE_AFTER)
    _style_run(p.add_run(_plain(text)), size=size, bold=True)
    return p


# ---- 卡片（Markdown 引用块 → 单格表格卡片）----

def _set_cell_borders(cell, sides):
    """给单元格设边框。sides: {边名: (val, sz, color) 或 None=nil}（顺序按 OOXML）。"""
    tcPr = cell._tc.get_or_add_tcPr()
    borders = tcPr.find(qn("w:tcBorders"))
    if borders is None:
        borders = OxmlElement("w:tcBorders")
        tcPr.append(borders)          # tcW 之后、tcMar 之前，顺序合规
    for side in ("top", "left", "bottom", "right"):
        el = OxmlElement(f"w:{side}")
        spec = sides.get(side)
        if spec is None:
            el.set(qn("w:val"), "nil")
        else:
            val, sz, color = spec
            el.set(qn("w:val"), val)
            el.set(qn("w:sz"), sz)
            el.set(qn("w:color"), color)
        borders.append(el)


def _set_cell_margins(cell, margins=CARD_MARGINS):
    tcPr = cell._tc.get_or_add_tcPr()
    mar = OxmlElement("w:tcMar")
    for side, w in zip(("top", "left", "bottom", "right"), margins):
        el = OxmlElement(f"w:{side}")
        el.set(qn("w:w"), str(w))
        el.set(qn("w:type"), "dxa")
        mar.append(el)
    tcPr.append(mar)


def _set_cell_shading(cell, fill):
    """单元格底纹（表头用）。shd 必须在 tcMar 之前，这里只用于没有 tcMar 的单元格。"""
    tcPr = cell._tc.get_or_add_tcPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), fill)
    mar = tcPr.find(qn("w:tcMar"))
    if mar is not None:
        mar.addprevious(shd)
    else:
        tcPr.append(shd)


def _add_card(doc, lines):
    """引用块 → 卡片：单格 1×1 表格，只留左侧竖线，无底纹，灰字（样例实测形态）。"""
    table = doc.add_table(rows=1, cols=1)
    table.autofit = True
    cell = table.cell(0, 0)
    _set_cell_borders(cell, {"left": ("single", CARD_BAR_SZ, CARD_ACCENT)})
    _set_cell_margins(cell)
    first = True
    for text in lines:
        p = cell.paragraphs[0] if first else cell.add_paragraph()
        first = False
        _style_para(p)
        if text:
            _add_runs(p, text, color=CARD_TEXT)
        else:
            _style_run(p.add_run(""), color=CARD_TEXT)
    return table


# ---- 表格（Markdown 表格 → 真 docx 表格）----

def _is_table_row(line):
    return _TABLE_ROW_RE.match(line) is not None


def _split_row(line):
    """一行 Markdown 表格 → 单元格列表。

    ① 行首/行尾竖线去掉（行尾可省，GFM 合法）；
    ② 只按**未转义**的 `|` 切分，`\\|` 还原成字面 `|`；
    ③ 行内代码 `` `a|b` `` 里的竖线不当分隔符（反引号内视为普通字符）。
    """
    text = line.strip()
    if text.startswith("|"):
        text = text[1:]
    if text.endswith("|") and not text.endswith("\\|"):
        text = text[:-1]
    cells, buf, in_code, i = [], "", False, 0
    while i < len(text):
        ch = text[i]
        if ch == "\\" and i + 1 < len(text) and text[i + 1] == "|":
            buf += "|"
            i += 2
            continue
        if ch == "`":
            in_code = not in_code
        elif ch == "|" and not in_code:
            cells.append(buf.strip())
            buf = ""
            i += 1
            continue
        buf += ch
        i += 1
    cells.append(buf.strip())
    return cells


def _is_separator_row(cells):
    return bool(cells) and all(_TABLE_SEP_CELL_RE.match(c) for c in cells if c != "")


def _clear_cell(cell):
    """清掉单元格里的默认空段落文字，返回第一段（供写入内容用）。"""
    p = cell.paragraphs[0]
    for run in list(p.runs):
        run._element.getparent().remove(run._element)
    return p


def _add_markdown_table(doc, rows):
    """Markdown 表格 → docx 表格：Table Grid 细边框，表头加粗 + 浅底纹。"""
    rows = [r for r in rows if not _is_separator_row(r)]
    if not rows:
        return None
    header, body = rows[0], rows[1:]
    table = doc.add_table(rows=1 + len(body), cols=len(header))
    table.style = "Table Grid"
    for j, text in enumerate(header):
        cell = table.cell(0, j)
        p = _clear_cell(cell)
        _style_para(p)
        _add_runs(p, text, base_bold=True)
        _set_cell_shading(cell, TABLE_HEADER_FILL)
    for i, row in enumerate(body, start=1):
        for j in range(len(header)):
            cell = table.cell(i, j)
            p = _clear_cell(cell)
            _style_para(p)
            _add_runs(p, row[j] if j < len(row) else "")
    return table


def _add_image(doc, src, base_dir=None, width=None):
    """Markdown 图片行 → 嵌入图片（相对路径按 md/docx 所在目录解析）。"""
    path = src if os.path.isabs(src) else os.path.join(base_dir or ".", src)
    if not os.path.exists(path):
        log.warning("[docx] 图片不存在，跳过：%s", path)
        return None
    try:
        section = doc.sections[0]
        if width is None:
            # 正文宽从 section 真实值算（page_width - 左右边距），不写死
            width = section.page_width - section.left_margin - section.right_margin
            width -= int(Inches(0.02))       # 留 0.02in 安全余量（≤0.05in 要求内）
        p = doc.add_paragraph()
        _style_para(p, before=SPACE_AFTER, after=SPACE_AFTER)
        p.alignment = 1                      # 居中
        _style_run(p.add_run(), size=BODY_SIZE)
        # 满宽插入，高度按图片纵横比自动。**不再缩图**：图放不下是渲染侧的问题
        # （infographic 会把纵横比压到 ≤0.92、底部空白裁掉），docx 里永远满宽。
        p.runs[0].add_picture(path, width=width)
        return p
    except Exception:
        log.exception("[docx] 插图失败（跳过这张图，正文不受影响）：%s", path)
        return None


def _render_markdown(doc, md, base_dir=None):
    """Markdown → docx：标题 / 卡片（引用块）/ 表格 / 图片 / 列表 / 正文。"""
    lines = md.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].rstrip()
        if not line.strip():
            i += 1
            continue

        image = _IMAGE_RE.match(line)
        if image:                      # ![alt](path) → 嵌入图片
            _add_image(doc, image.group(2).strip(), base_dir=base_dir)
            i += 1
            continue

        quote = _QUOTE_RE.match(line)
        if quote:                      # 连续 `> ` 行 = 一张卡片
            block = []
            while i < len(lines) and (m := _QUOTE_RE.match(lines[i])):
                block.append(m.group(1).strip())
                i += 1
            while block and not block[0]:
                block.pop(0)
            while block and not block[-1]:
                block.pop()
            _add_card(doc, block or [""])
            continue

        if _is_table_row(line):        # 连续表格行 = 一张表
            rows = []
            while i < len(lines) and _is_table_row(lines[i]):
                rows.append(_split_row(lines[i]))
                i += 1
            _add_markdown_table(doc, rows)
            continue

        for prefix, size in (("#### ", H4_SIZE), ("### ", H3_SIZE),
                             ("## ", H2_SIZE), ("# ", H1_SIZE)):
            if line.startswith(prefix):
                _add_heading(doc, line[len(prefix):].strip(), size)
                break
        else:
            m = _BULLET_RE.match(line)
            if m:
                _add_text_paragraph(doc, m.group(1), style="List Bullet")
            elif _NUMBER_RE.match(line):
                _add_text_paragraph(doc, _NUMBER_RE.match(line).group(1),
                                    style="List Number")
            else:
                _add_text_paragraph(doc, line.strip())
        i += 1


def _save_docx_at(markdown, path):
    """把纪要 Markdown 渲染成 docx 存到指定路径，页脚写生成时间。

    图片相对路径按 docx 所在目录解析（信息图 PNG 与 docx/md 同目录）。
    """
    full = time.strftime("%Y年%m月%d日 %H:%M")
    doc = Document()
    first_line = next((ln.strip() for ln in markdown.splitlines() if ln.strip()), "")
    if not first_line.startswith("# "):   # md 自带大标题时不重复加「会议纪要」
        _style_run(doc.add_paragraph().add_run("会议纪要"), size=H1_SIZE, bold=True)
    meta = doc.add_paragraph()
    _style_para(meta, before=0, after=SPACE_AFTER)
    _style_run(meta.add_run(f"生成时间：{full}"), size=10, color=META_TEXT)
    _render_markdown(doc, markdown, base_dir=os.path.dirname(os.path.abspath(path)))
    footer = doc.sections[0].footer
    footer.paragraphs[0].text = f"生成时间：{full}"
    doc.save(path)


def _next_base_name(out_dir, stamp):
    """下一个可用的文件名基（无扩展名）：docx 与 md 都不存在的第一个序号。

    基名 会议纪要_<stamp>，冲突则 会议纪要_<stamp>_2 / _3…（两种扩展名任一
    占用都跳过，保证成对文件永远同基名）。
    """
    base = os.path.join(out_dir, f"会议纪要_{stamp}")
    n = 2
    while os.path.exists(f"{base}.docx") or os.path.exists(f"{base}.md"):
        base = os.path.join(out_dir, f"会议纪要_{stamp}_{n}")
        n += 1
    return base


def save_minutes_pair(markdown, out_dir, base_name=None):
    """纪要成对导出：docx + md 同名成对放 out_dir。返回 (docx_path, md_path)。

    base_name：显式指定文件名基（不带扩展名）。信息图 PNG 要与 md/docx 同基名，
    调用方先用 next_base_name() 拿到基名、画图、再带着同一个基名来存。

    - 文件名基 = 会议纪要_YYYYMMDD_HHMM（同名冲突时两文件同基名加 _2/_3…）；
    - md 内容 = generate_minutes 返回的原始 Markdown 原样写（utf-8、不加 BOM，
      文件头不加任何多余东西）；
    - docx 渲染复用 _save_docx_at（与 save_minutes_docx 同一套排版）。
    """
    os.makedirs(out_dir, exist_ok=True)
    base = base_name or _next_base_name(out_dir, time.strftime("%Y%m%d_%H%M"))
    docx_path = base + ".docx"
    md_path = base + ".md"
    with open(md_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(markdown)
    _save_docx_at(markdown, docx_path)
    return docx_path, md_path


def next_base_name(out_dir):
    """当前时间戳下可用的文件名基（不带扩展名）。信息图与 docx/md 共用它。"""
    return _next_base_name(out_dir, time.strftime("%Y%m%d_%H%M"))


SUMMARY_IMAGE_HEADING = "## 总结"


def insert_summary_image(markdown, image_name):
    """在 md 的「## 总结」标题后插一行图片引用（信息图）；找不到标题就插在开头之后。"""
    ref = f"![总结信息图]({image_name})"
    lines = markdown.splitlines()
    for idx, line in enumerate(lines):
        if line.strip() == SUMMARY_IMAGE_HEADING:
            lines.insert(idx + 1, "")
            lines.insert(idx + 2, ref)
            return "\n".join(lines)
    # 没有「## 总结」（不该发生）→ 插在正文第一行标题之后，至少别丢图
    insert_at = 1 if lines else 0
    lines.insert(insert_at, "")
    lines.insert(insert_at + 1, ref)
    return "\n".join(lines)


def save_minutes_docx(markdown, out_dir):
    """把纪要 Markdown 存成 docx，返回文件绝对路径。同名自动加序号。

    文件名基与成对导出共用 `_next_base_name`（docx/md 两扩展名同一序号空间），
    不再只看 .docx——两套口径会让同一基名的 md 和 docx 撞车。
    """
    os.makedirs(out_dir, exist_ok=True)
    path = _next_base_name(out_dir, time.strftime("%Y%m%d_%H%M")) + ".docx"
    _save_docx_at(markdown, path)
    return path


# ---- PDF（本机 WPS/Word COM 转换 docx，best-effort）----

_WD_FORMAT_PDF = 17          # Word/WPS 的 wdFormatPDF
_WD_DO_NOT_SAVE = 0          # Close(0)：不保存改动（我们只读打开，本来也没有改动）
PDF_TIMEOUT_SEC = 120        # COM 转换墙钟上限：WPS 弹窗 / RPC 卡死时不能让管线线程永远挂住


def _com_quit(app, doc):
    """尽力收尾：先关文档、再退出应用（WPS 的 IDispatch 不保证 Quit 可用）。"""
    try:
        if doc is not None:
            doc.Close(_WD_DO_NOT_SAVE)
    except Exception as e:
        log.warning("[PDF] 关闭文档失败（PDF 已生成，忽略）：%s", e)
    try:
        if app is not None:
            app.Quit()
    except Exception:
        log.debug("[PDF] COM Quit 失败（PDF 已生成，忽略）")


def _com_convert(docx_path, pdf_path):
    """**必须在工作线程里跑**的 COM 段：CoInitialize → 打开 → SaveAs PDF → 收尾。

    只读打开（ReadOnly=True）、不进最近文件、Visible=False + DisplayAlerts=0。
    返回 True 表示 SaveAs 没抛异常（PDF 是否有效由调用方看文件）。`CoUninitialize`
    放在 finally 里，与该线程自己的 `CoInitialize` 成对。
    """
    import pythoncom
    from win32com.client import Dispatch

    pythoncom.CoInitialize()
    app = doc = None
    try:
        app = Dispatch("Word.Application")
        try:
            app._FlagAsMethod("Quit")
        except Exception:
            pass
        app.Visible = False
        app.DisplayAlerts = 0
        try:
            # Word 的 Open 位置参数序：FileName, ConfirmConversions, ReadOnly, AddToRecentFiles
            doc = app.Documents.Open(docx_path, False, True, False)
        except Exception:
            doc = app.Documents.Open(docx_path, ReadOnly=True)
        doc.SaveAs(pdf_path, _WD_FORMAT_PDF)
    except Exception:
        log.exception("[PDF] COM 转换失败（docx/md 不受影响）：%s", docx_path)
        return False
    finally:
        _com_quit(app, doc)
        del doc, app
        pythoncom.CoUninitialize()
    return True


def save_pdf_from_docx(docx_path, pdf_path=None):
    """用本机 WPS/Word 的 COM 把 docx 转成同名 PDF，返回 pdf 路径；失败返回 None。

    - **best-effort**：没装 WPS/Word、没装 pywin32、COM 报错、转换超时……一律记日志
      返回 None，绝不抛异常——PDF 只是加料，docx/md 必须先稳稳产出；
    - COM 在**独立的 daemon 线程**里跑（`pythoncom.CoInitialize()` /
      `CoUninitialize()` 成对，都在该线程内），主叫线程 `join(PDF_TIMEOUT_SEC)`：
      WPS 弹窗、RPC 卡死时最多等 120 秒就放弃，绝不让管线线程永远挂住；
    - 只读打开（`ReadOnly=True`）、不进最近文件（`AddToRecentFiles=False`）、
      `Visible=False` + `DisplayAlerts=0`，不在朋友屏幕上弹任何东西；
    - 实测坑：WPS 12.1 注册了 `Word.Application` 这个 ProgID，但它的 IDispatch 不把
      `Quit` 暴露成方法，`win32com` 动态派发会报 `AttributeError`，要先
      `app._FlagAsMethod("Quit")`。
    """
    pdf_path = pdf_path or (os.path.splitext(docx_path)[0] + ".pdf")
    try:
        import pythoncom
        from win32com.client import Dispatch
    except Exception as e:   # pywin32 没装（或非 Windows）：直接跳过
        log.warning("[PDF] pywin32 不可用（%s），跳过 PDF 导出（docx/md 不受影响）", e)
        return None

    result = {}

    def _work():
        try:
            result["ok"] = _com_convert(docx_path, pdf_path)
        except Exception:      # 兜底：线程里再漏异常就不会有人看见了
            log.exception("[PDF] COM 转换线程异常（docx/md 不受影响）：%s", docx_path)

    worker = threading.Thread(target=_work, name="pdf-convert", daemon=True)
    worker.start()
    worker.join(PDF_TIMEOUT_SEC)
    if worker.is_alive():      # 线程还在卡（daemon，不会拖住进程退出）
        log.warning("[PDF] 转换超过 %s 秒仍未返回（WPS 弹窗/RPC 卡死？），放弃等待；"
                    "docx/md 不受影响：%s", PDF_TIMEOUT_SEC, docx_path)
        return None
    if not result.get("ok"):
        return None

    if not os.path.exists(pdf_path) or os.path.getsize(pdf_path) == 0:
        log.warning("[PDF] 转换后没拿到有效 PDF（docx/md 不受影响）：%s", pdf_path)
        try:                   # best-effort 清掉半成品/0 字节文件，别留在输出目录里
            os.remove(pdf_path)
        except OSError as e:
            log.debug("[PDF] 清理无效 PDF 失败（忽略）：%s（%s）", pdf_path, e)
        return None
    log.info("[PDF] 已生成 %s（%d 字节）", pdf_path, os.path.getsize(pdf_path))
    return pdf_path
