# -*- coding: utf-8 -*-
"""总结信息图单测：JSON 严格校验 / Pillow 渲染冒烟 / docx 插图与 md 引用 / 失败跳过。

覆盖：① JSON 解析容错（纯 JSON、```json 围栏、前后带寒暄、非 JSON）；
② schema 校验（合法、缺字段、类型错、条数越界、多余字段被丢掉）；
③ 渲染冒烟（2~3 branch、1~4 卡片、画布尺寸合理、文件非空、白底 + 卡片存在）；
④ 字体读不到 → 返回 None；Pillow 缺失 → 返回 None；绘图异常 → 返回 None（都不抛）；
⑤ docx 插图（文档里有图片关系）+ md 引用行插在「## 总结」后；
⑥ pipeline 里信息图失败也照常出 docx/md/PDF。
"""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import docx_writer
import infographic
import pipeline
from infographic import (parse_infographic_json, render_infographic,
                         validate_infographic)

_VALID = {
    "title": "电商多平台运营策略复盘讨论",
    "core_points": ["天猫主推防漏系列", "拼多多解决比价问题", "统一黑标店定价步调"],
    "branches": [
        {"name": "天猫平台运营复盘", "cards": [
            {"title": "主推品策略", "subtitle": "天猫当前主推防漏系列",
             "points": ["投产比仅 1:1", "防漏投产比 6-7", "新客占比约 70%"]},
            {"title": "拉新与会员运营", "subtitle": "需新增独立拉新计划",
             "points": ["以收割老客为主", "设置独立拉新计划", "定向竞品人群"]},
        ]},
        {"name": "拼多多平台运营复盘", "cards": [
            {"title": "通货盘现状", "subtitle": "控价提价后价格涨至 165",
             "points": ["日销跌破 1000", "推广费率 12-13%"]},
            {"title": "专供品策略", "subtitle": "已上线 2/4/5 包规格",
             "points": ["6 包因比价暂无法上线", "主图仅展示外箱"]},
        ]},
    ],
}

_MINUTES_MD = ("# 智能纪要：示例 2026年9月16日\n\n> **录音主题**：示例\n\n"
               "## 总结\n音频围绕示例展开研讨，内容如下：\n\n"
               "### 议题\n- 要点\n\n## 待办\n- 甲：乙\n")


def _render_fonts():
    """render_infographic 内部那 8 个字体（供直接调 _measure 的测试复用）。"""
    from PIL import ImageFont

    def f(size, bold=False):
        path = infographic.FONT_BOLD if bold else infographic.FONT_REGULAR
        return ImageFont.truetype(path, size * infographic.SCALE)

    return (f(infographic.S_TITLE, True), f(infographic.S_PANEL_LABEL, True),
            f(infographic.S_PANEL_TEXT), f(infographic.S_SECTION, True),
            f(infographic.S_CARD_HEAD, True), f(infographic.S_CARD_TITLE, True),
            f(infographic.S_CARD_TEXT), f(infographic.S_FOOTER))


class ParseJsonTest(unittest.TestCase):
    """① JSON 解析容错。"""

    def test_plain_json(self):
        data = parse_infographic_json(json.dumps(_VALID, ensure_ascii=False))
        self.assertEqual(data["title"], _VALID["title"])

    def test_code_fence(self):
        text = "```json\n" + json.dumps(_VALID, ensure_ascii=False) + "\n```"
        self.assertEqual(parse_infographic_json(text)["title"], _VALID["title"])

    def test_with_small_talk(self):
        text = "好的，这是信息图数据：\n" + json.dumps(_VALID, ensure_ascii=False) + "\n希望有用！"
        self.assertEqual(parse_infographic_json(text)["title"], _VALID["title"])

    def test_not_json(self):
        for bad in ("", "   ", "抱歉我不能这么做", "[1,2,3]", "{不是 json}"):
            self.assertIsNone(parse_infographic_json(bad), bad)


class ValidateTest(unittest.TestCase):
    """② schema 校验。"""

    def test_valid(self):
        data = validate_infographic(json.loads(json.dumps(_VALID)))
        self.assertEqual(len(data["branches"]), 2)
        self.assertEqual(data["branches"][0]["cards"][1]["title"], "拉新与会员运营")

    def test_extra_fields_dropped(self):
        raw = json.loads(json.dumps(_VALID))
        raw["extra"] = "多余字段"
        raw["branches"][0]["cards"][0]["note"] = "也不该留"
        raw["branches"][0]["cards"][0]["points"].append("")
        data = validate_infographic(raw)
        self.assertIsNotNone(data)
        self.assertNotIn("extra", data)
        self.assertNotIn("note", data["branches"][0]["cards"][0])

    def test_missing_or_wrong_type(self):
        for mutate in (
            lambda d: d.pop("title"),
            lambda d: d.pop("core_points"),
            lambda d: d.pop("branches"),
            lambda d: d.update(core_points="不是列表"),
            lambda d: d.update(branches="不是列表"),
            lambda d: d.update(title="   "),
            lambda d: d.update(branches=[]),
            lambda d: d["branches"][0].pop("name"),
            lambda d: d["branches"][0].update(cards=[]),
            lambda d: d["branches"][0]["cards"][0].pop("title"),
            lambda d: d["branches"][0]["cards"][0].update(points=[]),
            lambda d: d["branches"][0]["cards"][0].update(points="不是列表"),
        ):
            raw = json.loads(json.dumps(_VALID))
            mutate(raw)
            self.assertIsNone(validate_infographic(raw))

    def test_cards_over_limit_merged_into_last(self):
        """cards 上限 3：多出来的卡把「标题：首个要点」并进最后一张，并记 log。"""
        raw = json.loads(json.dumps(_VALID))
        raw["branches"][0]["cards"] = [
            {"title": f"卡{i}", "subtitle": "副题", "points": ["要点一"]} for i in range(5)]
        with self.assertLogs("会议记录", level="WARNING") as cm:
            data = validate_infographic(raw)
        cards = data["branches"][0]["cards"]
        self.assertEqual(len(cards), 3)
        self.assertEqual(len(cards), infographic.MAX_CARDS)
        self.assertIn("卡3：要点一", cards[-1]["points"])
        self.assertIn("超过上限", "\n".join(cm.output))

    def test_branches_over_limit_dropped(self):
        """branches 上限 3：超出丢弃并记 log（5 个分支不能直接判整张图作废）。"""
        raw = json.loads(json.dumps(_VALID))
        raw["branches"] = [{"name": f"分支{i}", "cards": [
            {"title": "卡", "subtitle": "副题", "points": ["要点"]}]} for i in range(5)]
        with self.assertLogs("会议记录", level="WARNING"):
            data = validate_infographic(raw)
        self.assertEqual(len(data["branches"]), infographic.MAX_BRANCHES)

    def test_points_over_limit_truncated(self):
        """points 上限 5：只取前 5 条并记 log。"""
        raw = json.loads(json.dumps(_VALID))
        raw["branches"][0]["cards"][0]["points"] = [f"要点{i}" for i in range(9)]
        with self.assertLogs("会议记录", level="WARNING"):
            data = validate_infographic(raw)
        self.assertEqual(len(data["branches"][0]["cards"][0]["points"]),
                         infographic.MAX_POINTS)

    def test_title_and_point_length_clamped(self):
        raw = json.loads(json.dumps(_VALID))
        raw["title"] = "很长" * 40
        raw["core_points"] = ["要点" * 60]
        data = validate_infographic(raw)
        self.assertLessEqual(len(data["title"]), 40)
        self.assertLessEqual(len(data["core_points"][0]), 60)

    def test_object_valued_item_is_dropped(self):
        """C2-5：模型把要点写成 `{"text": "要点一"}` 时不再 str() 成 Python 对象字面量。"""
        raw = json.loads(json.dumps(_VALID))
        raw["branches"][0]["cards"][0]["points"] = [{"text": "要点一"}, "要点二", ["要点三"]]
        data = validate_infographic(raw)
        points = data["branches"][0]["cards"][0]["points"]
        self.assertEqual(points, ["要点二"])
        self.assertNotIn("{'text'", json.dumps(data, ensure_ascii=False))

    def test_object_valued_title_is_dropped(self):
        raw = json.loads(json.dumps(_VALID))
        raw["title"] = {"text": "标题"}
        self.assertIsNone(validate_infographic(raw))

    def test_multiline_text_collapsed_to_one_line(self):
        """C2-5：字符串内部空白（换行/制表）压成单空格——PIL 会真换行，布局只按一行算高。"""
        raw = json.loads(json.dumps(_VALID))
        raw["title"] = "第一行\n第二行"
        card = raw["branches"][0]["cards"][0]
        card["subtitle"] = "副题\t带制表"
        card["points"] = ["要点\n换行", "另\x0b一条"]
        data = validate_infographic(raw)
        self.assertEqual(data["title"], "第一行 第二行")
        self.assertEqual(data["branches"][0]["cards"][0]["subtitle"], "副题 带制表")
        self.assertEqual(data["branches"][0]["cards"][0]["points"], ["要点 换行", "另 一条"])


class RenderTest(unittest.TestCase):
    """③ 渲染冒烟：不同规模都不炸、尺寸合理、文件非空。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def _render(self, data, name="info.png"):
        return render_infographic(data, os.path.join(self.tmp.name, name))

    def test_basic_render(self):
        from PIL import Image
        path = self._render(json.loads(json.dumps(_VALID)))
        self.assertTrue(path and os.path.exists(path))
        self.assertGreater(os.path.getsize(path), 10_000)
        with Image.open(path) as im:
            self.assertEqual(im.size[0], 2400)                    # 画布宽固定（字面值）
            self.assertGreater(im.size[1], 600)
            self.assertEqual(im.convert("RGB").getpixel((5, 5)), (255, 255, 255))  # 白底

    def test_aspect_ratio_within_target(self):
        """样例规模（3 分支 × 3 卡 × 5 要点）纵横比必须 ≤ 0.92（首页放得下）。"""
        from PIL import Image
        data = json.loads(json.dumps(_VALID))
        data["branches"] = [
            {"name": f"分支{i}", "cards": [
                {"title": f"卡{i}-{j}", "subtitle": "副题一句",
                 "points": [f"要点{k}：把 85.3 往下调，预留三、四、五包空间" for k in range(5)]}
                for j in range(3)]}
            for i in range(3)]
        for name, d in (("样例", json.loads(json.dumps(_VALID))),
                        ("满规模", data)):
            path = self._render(d, f"asp_{name}.png")
            with Image.open(path) as im:
                ratio = im.size[1] / im.size[0]
            self.assertLessEqual(ratio, 0.92, f"{name} 纵横比超标：{ratio:.2f}")

    def test_sizes_matrix(self):
        """1~4 张卡片、1~3 个分支、要点多少都不炸。"""
        for n_branch, n_card, n_point in ((1, 1, 1), (2, 2, 3), (3, 3, 5), (1, 3, 5), (2, 1, 2)):
            data = json.loads(json.dumps(_VALID))
            data["branches"] = []
            for b in range(n_branch):
                cards = []
                for c in range(n_card):
                    cards.append({"title": f"卡片{b}-{c}", "subtitle": "副题" * 3,
                                  "points": [f"要点{i}，" + "长一点的内容" * 3
                                             for i in range(n_point)]})
                data["branches"].append({"name": f"分支{b}", "cards": cards})
            data["core_points"] = ["核心" * 5] * 4
            path = self._render(data, f"info_{n_branch}_{n_card}_{n_point}.png")
            self.assertTrue(path, f"{n_branch}/{n_card}/{n_point} 渲染失败")
            self.assertGreater(os.path.getsize(path), 5_000)

    def test_empty_subtitle_ok(self):
        data = json.loads(json.dumps(_VALID))
        data["branches"][0]["cards"][0]["subtitle"] = ""
        self.assertTrue(self._render(data, "no_sub.png"))

    def test_long_title_wraps(self):
        data = json.loads(json.dumps(_VALID))
        data["title"] = "非常长的会议主题" * 4
        self.assertTrue(self._render(data, "long_title.png"))

    def test_missing_font_returns_none(self):
        with mock.patch.object(infographic, "FONT_REGULAR", r"C:\no\such\font.ttc"), \
                self.assertLogs("会议记录", level="WARNING"):
            self.assertIsNone(self._render(_VALID, "nofont.png"))

    def test_no_pillow_returns_none(self):
        with mock.patch.dict(sys.modules, {"PIL": None, "PIL.ImageFont": None}), \
                self.assertLogs("会议记录", level="WARNING"):
            self.assertIsNone(self._render(_VALID, "nopillow.png"))

    def test_draw_exception_returns_none(self):
        """绘图抛异常 → 返回 None，不往外抛。"""
        with mock.patch("PIL.ImageDraw.Draw", side_effect=RuntimeError("模拟绘图炸了")), \
                self.assertLogs("会议记录", level="ERROR"):
            self.assertIsNone(self._render(_VALID, "boom.png"))

    def test_missing_dir_is_created(self):
        """输出目录不存在时自动建（名字与行为对齐；原名字与断言相反）。"""
        path = render_infographic(_VALID, os.path.join(self.tmp.name, "no", "dir", "x.png"))
        self.assertTrue(path)
        self.assertTrue(os.path.exists(path))

    def test_render_validates_input_unconditionally(self):
        """C2-8①：带 branches 的 dict 也要过 validate（越界分支裁掉并记 log）。"""
        data = json.loads(json.dumps(_VALID))
        data["branches"] = [{"name": f"分支{i}", "cards": [
            {"title": "卡", "subtitle": "", "points": ["要点"]}]} for i in range(5)]
        with self.assertLogs("会议记录", level="WARNING") as cm:
            path = self._render(data, "validated.png")
        self.assertTrue(path)
        self.assertIn("超过上限", "\n".join(cm.output))

    def test_font_construct_failure_returns_none(self):
        """C2-8③：_font() 抛异常也返回 None（原来在 try 之外，会直接外抛）。"""
        with mock.patch.object(infographic, "_load_fonts", return_value=(mock.Mock(),) * 8), \
                mock.patch("PIL.ImageFont.truetype", side_effect=OSError("模拟字体崩了")), \
                self.assertLogs("会议记录", level="ERROR"):
            self.assertIsNone(self._render(_VALID, "font_boom.png"))

    def test_card_title_never_exceeds_card_inner_width(self):
        """C2-7：超宽卡片标题要按宽度截断加省略号，绝不越框盖到相邻卡片。"""
        from PIL import Image, ImageDraw
        data = json.loads(json.dumps(_VALID))
        data["branches"] = [{"name": "分支", "cards": [
            {"title": "超长卡片标题" * 6, "subtitle": "副题",
             "points": ["要点一", "要点二"]}]}]

        drawn = []
        real_text = ImageDraw.ImageDraw.text

        def spy(inner_self, xy, text, *args, **kwargs):
            drawn.append((text, kwargs.get("font")))
            return real_text(inner_self, xy, text, *args, **kwargs)

        with mock.patch.object(ImageDraw.ImageDraw, "text", spy):
            self.assertTrue(self._render(data, "long_card_title.png"))

        fonts = _render_fonts()
        probe = ImageDraw.Draw(Image.new("RGB", (8, 8)))
        _, model = infographic._measure(data, probe, fonts, 1.0)
        self.assertLessEqual(model["aspect"], 0.92)     # k=1.0 与渲染同档，card_w 才可比
        avail = model["card_w"] - model["card_pad"] * 2
        titles = [(t, f) for t, f in drawn if "超长卡片标题" in t]
        self.assertEqual(len(titles), 1, drawn)
        title, font = titles[0]
        self.assertLessEqual(probe.textlength(title, font=font), avail)
        self.assertIn("…", title)                       # 截断加了省略号


class WrapTest(unittest.TestCase):
    """_wrap 折行边界（C2-6：标点回拽不得超宽）。"""

    @staticmethod
    def _measure(text, font):
        return len(text)          # 用字符数当宽度，便于精确构造「已满宽」场景

    def test_pullback_when_it_still_fits(self):
        self.assertEqual(infographic._wrap("abc。", None, 4, self._measure), ["abc。"])

    def test_pullback_never_overflows(self):
        """行已满宽时不再拽回标点（否则全角标点顶出卡片右边框）。"""
        lines = infographic._wrap("abc。。", None, 3, self._measure)
        self.assertTrue(all(len(line) <= 3 for line in lines), lines)
        self.assertEqual("".join(lines), "abc。。")


class CardGridTest(unittest.TestCase):
    """卡片网格的列数与换行阈值必须跟着 MAX_CARDS 走（C2-9：消除硬编码 3）。"""

    def test_grid_column_count_follows_max_cards(self):
        from PIL import Image, ImageDraw
        fonts = _render_fonts()
        probe = ImageDraw.Draw(Image.new("RGB", (8, 8)))
        data = {"title": "标题", "core_points": ["结论"], "branches": [
            {"name": "分支", "cards": [{"title": f"卡{i}", "subtitle": "",
                                        "points": ["要点"]} for i in range(3)]}]}
        _, three = infographic._measure(data, probe, fonts, 1.0)
        with mock.patch.object(infographic, "MAX_CARDS", 2):
            _, two = infographic._measure(data, probe, fonts, 1.0)
        self.assertEqual([len(r) for r in three["layouts"][0]["rows"]], [3])
        self.assertEqual([len(r) for r in two["layouts"][0]["rows"]], [2, 1])
        self.assertEqual(three["card_w"],
                         (three["content_w"] - three["card_gap"] * 2) // 3)
        self.assertEqual(two["card_w"],
                         (two["content_w"] - two["card_gap"] * 1) // 2)


class IconGlyphTest(unittest.TestCase):
    """图标字形自检：缺字形的图标退化成 ●（不画豆腐块）。"""

    def _font(self):
        from PIL import ImageFont
        return ImageFont.truetype(infographic.FONT_BOLD, 34)

    def test_all_planned_icons_have_glyphs(self):
        from PIL import ImageFont
        font = ImageFont.truetype(infographic.FONT_REGULAR, 34)
        for ch in ("★", "◆", "▲", "●", "■", "＋"):
            self.assertTrue(infographic._has_glyph(font, ch), ch)

    def test_known_missing_glyph_detected(self):
        from PIL import ImageFont
        font = ImageFont.truetype(infographic.FONT_REGULAR, 34)
        self.assertFalse(infographic._has_glyph(font, "✚"))     # 微软雅黑没有这个字形

    def test_missing_icon_falls_back(self):
        font = self._font()
        with mock.patch.object(infographic, "_has_glyph",
                               side_effect=lambda f, ch: ch != "■"),                 self.assertLogs("会议记录", level="WARNING"):
            icons = infographic.safe_icons(font)
        self.assertNotIn("■", icons)
        self.assertIn("●", icons)
        self.assertEqual(len(icons), len(infographic.ICONS))


class MakeSummaryImageTest(unittest.TestCase):
    """LLM 环节：失败一律 None（best-effort）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = {"deepseek": {"api_key": "k", "model": "deepseek-flash",
                                 "endpoint": "https://example.invalid"}}

    def tearDown(self):
        self.tmp.cleanup()

    def _make(self, chat_side_effect):
        with mock.patch.object(infographic, "_chat", side_effect=chat_side_effect):
            return infographic.make_summary_image(
                self.cfg, _MINUTES_MD, os.path.join(self.tmp.name, "图.png"))

    def test_success_path(self):
        text = json.dumps(_VALID, ensure_ascii=False)
        path = self._make(lambda *a, **k: text)
        self.assertTrue(path and os.path.exists(path))
        self.assertTrue(path.endswith("_总结图.png") or path.endswith("图.png"))

    def test_llm_failure_returns_none(self):
        with self.assertLogs("会议记录", level="WARNING"):
            self.assertIsNone(self._make(RuntimeError("DeepSeek 挂了")))

    def test_bad_json_returns_none(self):
        with self.assertLogs("会议记录", level="WARNING"):
            self.assertIsNone(self._make(lambda *a, **k: "我不知道"))

    def test_no_key_returns_none(self):
        with self.assertLogs("会议记录", level="WARNING"):
            self.assertIsNone(infographic.make_summary_image(
                {"deepseek": {"api_key": ""}}, _MINUTES_MD,
                os.path.join(self.tmp.name, "k.png")))

    def test_generate_data_without_key_returns_none(self):
        """C2-8②：_require_key 现在也在 try 里，ConfigError 不再外抛。"""
        with self.assertLogs("会议记录", level="WARNING"):
            self.assertIsNone(infographic.generate_infographic_data(
                {"deepseek": {"api_key": ""}}, _MINUTES_MD))


class MarkdownImageTest(unittest.TestCase):
    """⑤ md 引用行 + docx 插图。"""

    def test_insert_after_summary_heading(self):
        out = docx_writer.insert_summary_image(_MINUTES_MD, "会议纪要_x_总结图.png")
        lines = out.splitlines()
        idx = lines.index("## 总结")
        self.assertEqual(lines[idx + 1], "")
        self.assertEqual(lines[idx + 2], "![总结信息图](会议纪要_x_总结图.png)")
        self.assertIn("音频围绕示例展开研讨", out)

    def test_insert_without_heading(self):
        out = docx_writer.insert_summary_image("# 标题\n正文\n", "a.png")
        self.assertIn("![总结信息图](a.png)", out)

    def test_docx_embeds_picture(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as tmp:
            png = os.path.join(tmp, "会议纪要_x_总结图.png")
            Image.new("RGB", (2400, 900), "white").save(png)
            md = docx_writer.insert_summary_image(_MINUTES_MD, os.path.basename(png))
            docx_path, md_path = docx_writer.save_minutes_pair(
                md, tmp, base_name=os.path.join(tmp, "会议纪要_x"))
            from docx import Document
            doc = Document(docx_path)
            self.assertEqual(len(doc.inline_shapes), 1)          # 真插进 docx 了
            # 文档里确实多了一个图片 part（图片关系存在）
            self.assertTrue(any("image" in part.content_type
                                for part in doc.part.package.parts))
            with open(md_path, encoding="utf-8") as f:
                self.assertIn("![总结信息图](会议纪要_x_总结图.png)", f.read())

    def test_picture_width_equals_section_content_width(self):
        """图片插入宽度 = section 正文宽（page_width - 左右边距），留 ≤0.05in 余量。"""
        from PIL import Image
        from docx import Document
        from docx.shared import Emu
        with tempfile.TemporaryDirectory() as tmp:
            png = os.path.join(tmp, "会议纪要_x_总结图.png")
            Image.new("RGB", (2400, 1900), "white").save(png)
            md = docx_writer.insert_summary_image(_MINUTES_MD, os.path.basename(png))
            docx_path, _ = docx_writer.save_minutes_pair(
                md, tmp, base_name=os.path.join(tmp, "会议纪要_x"))
            doc = Document(docx_path)
            section = doc.sections[0]
            content_w = Emu(section.page_width - section.left_margin - section.right_margin)
            pic_w = doc.inline_shapes[0].width
            self.assertEqual(pic_w, content_w - Emu(int(docx_writer.Inches(0.02))))
            self.assertLessEqual(Emu(content_w - pic_w).inches, 0.05)      # 余量 ≤0.05in
            ratio = doc.inline_shapes[0].height / pic_w
            self.assertAlmostEqual(ratio, 1900 / 2400, places=2)          # 高度按纵横比

    def test_docx_missing_image_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            md = docx_writer.insert_summary_image(_MINUTES_MD, "不存在.png")
            with self.assertLogs("会议记录", level="WARNING"):
                docx_path, _ = docx_writer.save_minutes_pair(
                    md, tmp, base_name=os.path.join(tmp, "m"))
            from docx import Document
            self.assertEqual(len(Document(docx_path).inline_shapes), 0)


class PipelineImageTest(unittest.TestCase):
    """⑥ pipeline 集成：失败不影响出稿，成功则 md 有引用、report 有 image_path。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = {"app": {"output_dir": self.tmp.name}}
        self.utts = [{"speaker": "0", "text": "我是张三", "start_time": 0}]

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self):
        with mock.patch.object(pipeline, "generate_minutes", return_value=_MINUTES_MD), \
                mock.patch.object(pipeline, "save_pdf_from_docx", return_value=None):
            return pipeline.run_pipeline_streaming(self.utts, cfg=self.cfg)

    def test_image_success(self):
        from PIL import Image
        def fake_make(cfg, md, png_path):
            Image.new("RGB", (400, 200), "white").save(png_path)
            return png_path
        with mock.patch("infographic.make_summary_image", side_effect=fake_make):
            docx_path, report = self._run()
        self.assertEqual(report["image_path"], docx_path.replace(".docx", "_总结图.png"))
        self.assertTrue(os.path.exists(report["image_path"]))
        with open(report["md_path"], encoding="utf-8") as f:
            self.assertIn("![总结信息图]", f.read())
        from docx import Document
        self.assertEqual(len(Document(docx_path).inline_shapes), 1)

    def test_image_failure_still_outputs(self):
        with mock.patch("infographic.make_summary_image",
                        side_effect=RuntimeError("画图炸了")), \
                self.assertLogs("会议记录", level="ERROR"):
            docx_path, report = self._run()
        self.assertTrue(os.path.exists(docx_path))
        self.assertIsNone(report["image_path"])
        with open(report["md_path"], encoding="utf-8") as f:
            self.assertNotIn("![总结信息图]", f.read())

    def test_image_none_still_outputs(self):
        with mock.patch("infographic.make_summary_image", return_value=None):
            docx_path, report = self._run()
        self.assertTrue(os.path.exists(docx_path))
        self.assertIsNone(report["image_path"])

    def test_infographic_module_missing_is_ok(self):
        with mock.patch.dict(sys.modules, {"infographic": None}), \
                self.assertLogs("会议记录", level="ERROR"):
            docx_path, report = self._run()
        self.assertTrue(os.path.exists(docx_path))
        self.assertIsNone(report["image_path"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
