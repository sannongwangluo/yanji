# -*- coding: utf-8 -*-
"""总结信息图：拿生成好的纪要 md 再问一次 DeepSeek（严格 JSON）→ 用 Pillow 画彩色卡片图。

两段式（纪要文字部分完全不动）：
1. `minutes_llm` 照旧出七段纪要 md；
2. 本模块把 md 发给 DeepSeek 要一份结构化摘要（严格 JSON，schema 见
   `INFOGRAPHIC_SYSTEM_PROMPT`），校验通过后用 Pillow 按钉钉样例版式画 PNG。

版式（对照样例 `image1.jpeg` 实测，见 archive.md）：
白底 + 顶部大标题 + 核心结论浅灰绿面板（#F6F8F7）+ 每个 branch 一行小节标题 +
卡片三列网格（每行最多 3 张），卡片 = 白底圆角 + 浅色标题条（六色循环）+ 图标 +
粗体卡片标题 + 虚线分隔 + • 要点自动换行；画布宽 2400px、高按内容自适应，
先在 2 倍尺寸画再 LANCZOS 缩回（抗锯齿）。

**红线**：JSON 解析失败、schema 不过、字体读不到、绘图/写盘出错 —— 一律记日志返回 None，
docx/md/PDF 文字版照常出稿（best-effort）。图标只用 ★◆▲●■✚ 这套几何符号，
不用彩色 emoji（Pillow 画不出彩色 emoji，画出来是黑白豆腐块）。
"""
import json
import logging
import os
import re

from minutes_llm import MAX_TOKENS, _chat, _require_key

log = logging.getLogger("会议记录")

WIDTH = 2400                     # 画布宽（最终像素）
SCALE = 2                        # 先按 2 倍画再缩回（抗锯齿）
MARGIN = 60 * SCALE              # 页边距
GAP = 36 * SCALE                 # 卡片间距
PANEL_BG = "#F6F8F7"             # 核心结论面板底色（样例实测）
PANEL_RADIUS = 18 * SCALE
SECTION_RADIUS = 18 * SCALE
CARD_BORDER = "#E6E9EE"
CARD_SHADOW = "#F1F3F6"
DASH_COLOR = "#CFD6E0"
TITLE_COLOR = "#141823"          # 顶部标题（样例实测）
TEXT_COLOR = "#202124"
BULLET_COLOR = "#33383F"
FOOTER_COLOR = "#9AA1AB"
FOOTER_TEXT = "内容由 AI 生成"

# 卡片标题条六色循环 + 对应的头部文字色（配色取自样例实测的浅蓝紫/浅青/浅绿/浅橙/浅粉）
CARD_COLORS = [
    ("浅蓝", "#F6F6FF", "#3B4E9B"),
    ("浅青", "#ECF9FF", "#0F6E7C"),
    ("浅绿", "#F5F9E0", "#4E6B14"),
    ("浅橙", "#FFF6ED", "#A85400"),
    ("浅紫", "#FFF5F9", "#9B2C63"),
    ("浅蓝灰", "#E9EEF6", "#475569"),
]
# 图标集：固定几何符号按序指派。原计划的 ✚(U+271A) 在微软雅黑里**没有字形**（实测
# 会画成豆腐块 □），换成同为「加号」语义的全角 ＋(U+FF0B)；渲染前还会逐个验字形，
# 缺字形的自动退化成 ●，绝不画豆腐块。
ICONS = ("★", "◆", "▲", "●", "■", "＋")
_FALLBACK_ICON = "●"

FONT_REGULAR = r"C:\Windows\Fonts\msyh.ttc"
FONT_BOLD = r"C:\Windows\Fonts\msyhbd.ttc"

# 字号（最终像素；绘制时乘 SCALE）
S_TITLE = 52
S_PANEL_LABEL = 36
S_PANEL_TEXT = 34
S_SECTION = 40
S_CARD_HEAD = 34
S_CARD_TITLE = 34
S_CARD_TEXT = 30
S_FOOTER = 24

# ---- 严格 JSON schema ----

SCHEMA_HINT = """{
  "title": "会议主题（一句话，别超过 24 字）",
  "core_points": ["核心结论1", "核心结论2", "核心结论3"],
  "branches": [
    {
      "name": "分支名（如 天猫平台运营复盘）",
      "cards": [
        {"title": "卡片标题", "subtitle": "一句加粗副题", "points": ["要点1", "要点2", "要点3"]}
      ]
    }
  ]
}"""

INFOGRAPHIC_SYSTEM_PROMPT = (
    "你是会议纪要信息图助手。用户会给你一份已经写好的会议纪要 Markdown，"
    "你要把它压缩成一张信息图所需的结构化数据。\n"
    "**只输出 JSON，不要输出任何解释、不要用 ```json 代码块包裹、不要加注释**。\n"
    "JSON 结构必须严格如下（字段名一字不差）：\n"
    f"{SCHEMA_HINT}\n"
    "要求：\n"
    "- title：会议主题，取自纪要标题，一句话，不超过 24 字；\n"
    "- core_points：2~4 条最核心的结论，每条一句话（不超过 40 字），保留关键数字；\n"
    "- branches：2~3 个分支（按议题/平台/业务线归类），每个分支 2~4 张卡片；\n"
    "- 每张卡片：title（4~12 字）、subtitle（一句加粗副题，可空字符串）、"
    "points（3~5 条要点，每条不超过 30 字，保留数字与人名）；\n"
    "- 只压缩、不编造：纪要里没有的信息一个字都不要加；数字、人名、结论按纪要原样。"
)

INFOGRAPHIC_TEMPLATE = """下面是这次会议的纪要 Markdown，请按系统提示的 JSON 结构输出信息图数据（只输出 JSON）：

{minutes}"""


def _strip_code_fence(text):
    """去掉模型可能加的 ```json 围栏，返回纯文本。"""
    t = (text or "").strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
        t = re.sub(r"\s*```$", "", t)
    return t.strip()


def parse_infographic_json(text):
    """模型输出 → dict；解析失败返回 None（best-effort，不抛）。"""
    raw = _strip_code_fence(text)
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        # 兜底：截取第一个 { 到最后一个 } 再试一次（模型前后带寒暄时）
        start, end = raw.find("{"), raw.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            data = json.loads(raw[start:end + 1])
        except ValueError:
            return None
    return data if isinstance(data, dict) else None


def _clean_str(value, limit=80):
    """单个字段 → 单行字符串。

    - 只认 str/int/float：模型偶尔把要点写成 `{"text": "…"}` 这种对象，直接 `str()`
      会把 `{'text': '要点一'}` 印到图上，这里一律返回 ""（该条目随后的过滤会丢掉它）；
    - 内部空白（换行/制表/连续空格）压成单个空格：PIL 会把 `\\n` 真换行，而布局只按
      一行算高 → 文字互相重叠。压平后「一行就是一行」。
    """
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()[:limit]


def _clean_list(values, min_n, max_n, limit=80):
    if not isinstance(values, list):
        return None
    items = [_clean_str(v, limit) for v in values]
    items = [v for v in items if v]
    if not (min_n <= len(items) <= max_n):
        return None
    return items


def validate_infographic(data):
    """严格校验 + 归一化：缺字段/类型不对/条数越界 → None；多余字段直接丢掉。"""
    if not isinstance(data, dict):
        return None
    title = _clean_str(data.get("title"), 40)
    if not title:
        return None
    core_points = _clean_list(data.get("core_points"), 1, 6, limit=60)
    if not core_points:
        return None
    branches_in = data.get("branches")
    if not isinstance(branches_in, list) or not (1 <= len(branches_in) <= 20):
        return None
    if len(branches_in) > MAX_BRANCHES:      # 图上每个 branch 一行，最多 3 行才能放进首页
        log.warning("[信息图] 分支 %d 个超过上限 %d，丢弃多余的 %d 个",
                    len(branches_in), MAX_BRANCHES, len(branches_in) - MAX_BRANCHES)
        branches_in = branches_in[:MAX_BRANCHES]
    branches = []
    for b in branches_in:
        if not isinstance(b, dict):
            return None
        name = _clean_str(b.get("name"), 40)
        cards_in = b.get("cards")
        if not name or not isinstance(cards_in, list) or not (1 <= len(cards_in) <= 5):
            return None
        if len(cards_in) > MAX_CARDS:        # 一行最多 3 张，多出来的并进最后一张
            log.warning("[信息图] 「%s」卡片 %d 张超过上限 %d，多出的要点并入最后一张",
                        name, len(cards_in), MAX_CARDS)
            extra = cards_in[MAX_CARDS:]
            cards_in = cards_in[:MAX_CARDS]
            last = dict(cards_in[MAX_CARDS - 1]) if isinstance(cards_in[MAX_CARDS - 1], dict) else None
            if last is not None:
                pts = list(last.get("points") or [])
                for e in extra:
                    if not isinstance(e, dict):
                        continue
                    title_e = _clean_str(e.get("title"), 30)
                    more = e.get("points")
                    first = _clean_str(more[0], 60) if isinstance(more, list) and more else ""
                    merged = f"{title_e}：{first}" if title_e and first else (title_e or first)
                    if merged:
                        pts.append(merged)
                last["points"] = [p for p in pts if p][:MAX_POINTS + 2]
                cards_in = cards_in[:-1] + [last]
        cards = []
        for c in cards_in:
            if not isinstance(c, dict):
                return None
            c_title = _clean_str(c.get("title"), 30)
            points = _clean_list(c.get("points"), 1, 50, limit=60)   # 上限由 MAX_POINTS 截断
            if not c_title or not points:
                return None
            if len(points) > MAX_POINTS:
                log.warning("[信息图] 「%s」要点 %d 条超过上限 %d，只取前 %d 条",
                            c_title, len(points), MAX_POINTS, MAX_POINTS)
                points = points[:MAX_POINTS]
            cards.append({
                "title": c_title,
                "subtitle": _clean_str(c.get("subtitle"), 40),
                "points": points,
            })
        branches.append({"name": name, "cards": cards})
    return {"title": title, "core_points": core_points, "branches": branches}


def build_infographic_messages(minutes_md):
    return [
        {"role": "system", "content": INFOGRAPHIC_SYSTEM_PROMPT},
        {"role": "user", "content": INFOGRAPHIC_TEMPLATE.format(minutes=minutes_md)},
    ]


def generate_infographic_data(cfg, minutes_md):
    """纪要 md → 信息图数据 dict；任何失败返回 None（不抛）。

    `_require_key` 也在 try 里：没配 API Key 时它抛 ConfigError，但这里的契约是
    「任何失败返回 None」（信息图只是加料，不能让主流程炸）。
    """
    try:
        ds = _require_key(cfg)
        text = _chat(ds, build_infographic_messages(minutes_md), "信息图", max_tokens=MAX_TOKENS)
    except Exception as e:
        log.warning("[信息图] 生成失败（不影响出稿）：%s", e)
        return None
    data = validate_infographic(parse_infographic_json(text))
    if data is None:
        log.warning("[信息图] 模型返回的 JSON 不合法或结构不符，跳过信息图")
        return None
    return data


# ---- 渲染（Pillow）----

def _load_fonts():
    """读系统微软雅黑（常规 + 加粗）；读不到返回 None（跳过信息图）。"""
    try:
        from PIL import ImageFont
    except Exception as e:
        log.warning("[信息图] 没装 Pillow（%s），跳过信息图（docx/md/PDF 不受影响）", e)
        return None
    if not (os.path.exists(FONT_REGULAR) and os.path.exists(FONT_BOLD)):
        log.warning("[信息图] 找不到系统微软雅黑字体，跳过信息图：%s", FONT_REGULAR)
        return None
    try:
        return (ImageFont.truetype(FONT_REGULAR, 34 * SCALE),
                ImageFont.truetype(FONT_BOLD, 34 * SCALE))
    except Exception as e:
        log.warning("[信息图] 字体加载失败（%s），跳过信息图", e)
        return None


def _has_glyph(font, ch):
    """字体里有没有这个字的字形（缺字形 PIL 会画成 .notdef 豆腐块）。"""
    try:
        from PIL import Image, ImageDraw
        def render(c):
            im = Image.new("L", (48, 48), 255)
            ImageDraw.Draw(im).text((4, 4), c, font=font, fill=0)
            return im.tobytes()
        return render(ch) != render("")      # 私用区字符必是 .notdef
    except Exception:
        return True                                 # 检查失败就当有，别因小失大


def safe_icons(font):
    """逐个验字形的图标列表：缺字形的换成 ●（不画豆腐块）。"""
    icons = [ch if _has_glyph(font, ch) else _FALLBACK_ICON for ch in ICONS]
    if icons != list(ICONS):
        log.warning("[信息图] 图标字形缺失，已退化为 %s（字体：%s）", icons, FONT_REGULAR)
    return icons


def _wrap(text, font, max_width, measure):
    """按像素宽贪心折行（中英混排；优先在空格/标点后断，避免行首标点）。"""
    lines, cur = [], ""
    for ch in str(text):
        if measure(cur + ch, font) <= max_width or not cur:
            cur += ch
            continue
        # 行首不要出现收尾标点：把它拽回上一行——但**拽回后仍不能超宽**，
        # 否则全角标点会顶出卡片右边框（cur 已满宽时宁可正常断行）
        if ch in "，。、；：）】！？%,.;:)]!?" and cur \
                and measure(cur + ch, font) <= max_width:
            lines.append(cur + ch)
            cur = ""
            continue
        lines.append(cur)
        cur = ch
    if cur:
        lines.append(cur)
    return lines or [""]


def _ellipsize(text, font, max_width, measure):
    """按像素宽逐字收缩，超宽就截断加省略号（保证画出来绝不越框）。"""
    if measure(text, font) <= max_width:
        return text
    cut = ""
    for ch in str(text):
        if measure(cut + ch + "…", font) > max_width:
            break
        cut += ch
    return cut + "…" if cut else ""


MAX_BRANCHES = 3                 # 图上每个 branch 一行，超过 3 行放不进首页
MAX_CARDS = 3                    # 每行最多 3 张卡（越界由 validate 合并/丢弃并记 log）
MAX_POINTS = 5                   # 每张卡最多 5 条要点（越界截断并记 log，保证图放得进首页）
ASPECT_MAX = 0.92               # 画布 高/宽 上限（6.27in 宽 → ≤5.77in 高，首页放得下）
_COMPACT_STEPS = (1.0, 0.94, 0.88, 0.82, 0.76, 0.70, 0.64)


def _measure(data, probe, fonts, k):
    """按紧凑系数 k 量一遍布局（只算尺寸，不画）。返回 (总高, 布局模型)。

    k 只收**纵向间距**（内边距/行距/块间距），字号一律不动——宁少一张卡也不小一号字。
    """
    (f_title, f_panel_label, f_panel, f_section,
     f_card_head, f_card_title, f_card_text, f_footer) = fonts

    def tw(text, font):
        return probe.textlength(text, font=font)

    def th(font):
        box = font.getbbox("测Ag")
        return box[3] - box[1]

    def sp(base):
        return max(2, int(base * SCALE * k))

    content_w = WIDTH * SCALE - MARGIN * 2
    panel_pad = sp(22)
    panel_lines = _wrap("★ 核心结论：", f_panel_label, content_w - panel_pad * 2, tw)
    core_lines = []
    for point in data["core_points"]:
        core_lines += _wrap("• " + point, f_panel, content_w - panel_pad * 2, tw)
    panel_h = (panel_pad * 2 + len(panel_lines) * (th(f_panel_label) + sp(6))
               + sp(4) + len(core_lines) * (th(f_panel) + sp(6)) - sp(6))

    card_gap = max(int(GAP * k), 16 * SCALE)
    card_w = (content_w - card_gap * (MAX_CARDS - 1)) // MAX_CARDS
    card_pad = sp(18)
    head_h = int(52 * SCALE * max(k, 0.85))
    layouts = []
    for branch in data["branches"]:
        rows, row = [], []
        for card in branch["cards"]:
            bullet_lines = []
            for point in card["points"]:
                bullet_lines += _wrap("• " + point, f_card_text, card_w - card_pad * 2, tw)
            sub_lines = _wrap(card["subtitle"], f_card_title,
                              card_w - card_pad * 2, tw) if card["subtitle"] else []
            body_h = (card_pad + len(sub_lines) * (th(f_card_title) + sp(4))
                      + sp(18) + len(bullet_lines) * (th(f_card_text) + sp(7))
                      + card_pad)
            row.append({"card": card, "h": head_h + body_h,
                        "bullets": bullet_lines, "subs": sub_lines})
            if len(row) == MAX_CARDS:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        layouts.append({"branch": branch, "rows": rows})

    section_h = th(f_section) + sp(18)
    row_gap = sp(24)
    body_h = 0
    for blk in layouts:
        body_h += section_h
        for row in blk["rows"]:
            body_h += max(c["h"] for c in row) + row_gap
        body_h += sp(6)
    footer_h = th(f_footer)
    total_h = (MARGIN + th(f_title) + sp(22) + panel_h + sp(28) + body_h
               + sp(10) + footer_h + sp(16))
    return total_h, {
        # 注意：total_h 是 2 倍画布单位，纵横比必须除以 WIDTH*SCALE 才是 1× 口径
        "aspect": total_h / (WIDTH * SCALE),
        "content_w": content_w, "card_w": card_w, "card_gap": card_gap,
        "card_pad": card_pad, "head_h": head_h, "panel_pad": panel_pad,
        "panel_h": panel_h, "panel_lines": panel_lines, "core_lines": core_lines,
        "section_h": section_h, "row_gap": row_gap, "body_h": body_h,
        "footer_h": footer_h, "sp": sp, "layouts": layouts, "total_h": total_h,
    }


def render_infographic(data, out_path):
    """把信息图数据画成 PNG，返回路径；任何失败返回 None（不抛）。

    高度受控：先按 1.0 的间距量一遍，若 高/宽 > ASPECT_MAX（0.92）就按
    `_COMPACT_STEPS` 逐档收紧**纵向间距**（字号不动），直到放得下；画布底部空白
    最后按真实内容 bbox 裁掉。docx 侧永远是满宽插入，靠这里保证图放得进首页。
    """
    data = validate_infographic(data)      # 无条件校验：带 branches 的 dict 也过一遍
    if not data:
        return None
    fonts = _load_fonts()
    if fonts is None:
        return None
    try:
        from PIL import Image, ImageChops, ImageDraw, ImageFont

        def _font(size, bold=False):
            path = FONT_BOLD if bold else FONT_REGULAR
            return ImageFont.truetype(path, size * SCALE)

        f_title, f_panel_label = _font(S_TITLE, True), _font(S_PANEL_LABEL, True)
        f_panel, f_section = _font(S_PANEL_TEXT), _font(S_SECTION, True)
        f_card_head, f_card_title, f_card_text = (_font(S_CARD_HEAD, True),
                                                  _font(S_CARD_TITLE, True), _font(S_CARD_TEXT))
        f_footer = _font(S_FOOTER)
    except Exception:
        # 字体文件被删/被占用等：契约是任何失败返回 None，绝不外抛
        log.exception("[信息图] 字体加载失败（不影响出稿）")
        return None
    fonts = (f_title, f_panel_label, f_panel, f_section,
             f_card_head, f_card_title, f_card_text, f_footer)

    try:
        probe = ImageDraw.Draw(Image.new("RGB", (8, 8)))
        total_h, model, k = 0, None, _COMPACT_STEPS[0]
        for cand in _COMPACT_STEPS:
            total_h, model = _measure(data, probe, fonts, cand)
            k = cand
            if model["aspect"] <= ASPECT_MAX:
                break
        else:
            log.warning("[信息图] 收到最紧间距仍高于目标纵横比（高/宽 %.2f > %.2f），"
                        "图会偏高；docx 里仍满宽插入", model["aspect"], ASPECT_MAX)
        if k != _COMPACT_STEPS[0]:
            log.info("[信息图] 为放进首页把纵向间距收紧到 %.0f%%（高/宽 %.2f，字号未缩）",
                     k * 100, model["aspect"])

        sp = model["sp"]
        content_w, card_w, card_gap = model["content_w"], model["card_w"], model["card_gap"]
        card_pad, head_h = model["card_pad"], model["head_h"]
        panel_pad, panel_h = model["panel_pad"], model["panel_h"]
        section_h, row_gap = model["section_h"], model["row_gap"]

        def th(font):
            box = font.getbbox("测Ag")
            return box[3] - box[1]

        def tw(text, font):
            return probe.textlength(text, font=font)

        img = Image.new("RGB", (WIDTH * SCALE, model["total_h"]), "white")
        d = ImageDraw.Draw(img)

        def rounded(box, radius, fill, outline=None, width=1):
            d.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=width)

        y = MARGIN
        d.text((MARGIN, y), data["title"], font=f_title, fill=TITLE_COLOR)
        y += th(f_title) + sp(22)

        rounded((MARGIN, y, MARGIN + content_w, y + panel_h), PANEL_RADIUS, PANEL_BG)
        ty = y + panel_pad
        for line in model["panel_lines"]:
            d.text((MARGIN + panel_pad, ty), line, font=f_panel_label, fill=TITLE_COLOR)
            ty += th(f_panel_label) + sp(6)
        ty += sp(4)
        for line in model["core_lines"]:
            d.text((MARGIN + panel_pad, ty), line, font=f_panel, fill=TEXT_COLOR)
            ty += th(f_panel) + sp(6)
        y += panel_h + sp(28)

        icon_i = 0
        icons = safe_icons(f_card_head)
        for blk in model["layouts"]:
            d.text((MARGIN, y), blk["branch"]["name"], font=f_section, fill=TEXT_COLOR)
            y += section_h
            for row in blk["rows"]:
                row_h = max(c["h"] for c in row)
                for r_i, item in enumerate(row):
                    card = item["card"]
                    x = MARGIN + r_i * (card_w + card_gap)
                    name, bg, accent = CARD_COLORS[icon_i % len(CARD_COLORS)]
                    icon = icons[icon_i % len(icons)]
                    icon_i += 1
                    rounded((x + 2 * SCALE, y + 3 * SCALE, x + card_w + 2 * SCALE,
                             y + row_h + 3 * SCALE), SECTION_RADIUS, CARD_SHADOW)
                    rounded((x, y, x + card_w, y + row_h), SECTION_RADIUS, "white",
                            outline=CARD_BORDER, width=max(1, SCALE))
                    rounded((x, y, x + card_w, y + head_h + SECTION_RADIUS),
                            SECTION_RADIUS, bg)
                    rounded((x, y + head_h, x + card_w, y + head_h + SECTION_RADIUS), 0, bg)
                    d.text((x + card_pad, y + (head_h - th(f_card_head)) // 2 - 2 * SCALE),
                           _ellipsize(f"{icon} {card['title']}", f_card_head,
                                      card_w - card_pad * 2, tw),
                           font=f_card_head, fill=accent)
                    cy = y + head_h + card_pad
                    for line in item["subs"]:
                        d.text((x + card_pad, cy), line, font=f_card_title, fill=TITLE_COLOR)
                        cy += th(f_card_title) + sp(4)
                    cy += sp(10)
                    dash_x, dash_len, dash_gap = x + card_pad, 12 * SCALE, 8 * SCALE
                    while dash_x < x + card_w - card_pad:
                        d.line((dash_x, cy, min(dash_x + dash_len, x + card_w - card_pad), cy),
                               fill=DASH_COLOR, width=max(1, SCALE))
                        dash_x += dash_len + dash_gap
                    cy += sp(10)
                    for line in item["bullets"]:
                        d.text((x + card_pad, cy), line, font=f_card_text, fill=BULLET_COLOR)
                        cy += th(f_card_text) + sp(7)
                y += row_h + row_gap
            y += sp(6)

        d.text((MARGIN + content_w - probe.textlength(FOOTER_TEXT, font=f_footer), y),
               FOOTER_TEXT, font=f_footer, fill=FOOTER_COLOR)

        # 底部空白裁掉（布局模型与真实绘制可能差几像素，按真实内容 bbox 收一刀）
        diff = ImageChops.difference(img, Image.new("RGB", img.size, "white"))
        bbox = diff.getbbox()
        if bbox:
            img = img.crop((0, 0, img.size[0], min(img.size[1], bbox[3] + sp(16))))
        if img.size[1] % SCALE:
            img = img.crop((0, 0, img.size[0], img.size[1] - img.size[1] % SCALE))
        img = img.resize((WIDTH, img.size[1] // SCALE), Image.LANCZOS)
        os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
        img.save(out_path, "PNG")
    except Exception:
        log.exception("[信息图] 绘制失败（不影响出稿）")
        return None
    log.info("[信息图] 已生成 %s（%d 字节，%sx%s，高/宽 %.2f）",
             out_path, os.path.getsize(out_path), img.size[0], img.size[1],
             img.size[1] / img.size[0])
    return out_path


def make_summary_image(cfg, minutes_md, png_path):
    """纪要 md → 总结信息图 PNG（best-effort）。任何环节失败返回 None。"""
    try:
        data = generate_infographic_data(cfg, minutes_md)
        if not data:
            return None
        return render_infographic(data, png_path)
    except Exception:
        log.exception("[信息图] 生成异常（不影响出稿）")
        return None
