"""Template mode of the built-in pptx skill: deck.json -> native PPTX in one build."""

from __future__ import annotations

import copy
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("pptx")
pytest.importorskip("PIL")

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "packages/core/ai/skills/pptx"
SCRIPTS = SKILL / "scripts"
sys.path.insert(0, str(SCRIPTS))

from PIL import Image  # noqa: E402
from pptx import Presentation  # noqa: E402

import pptx_quality_gate  # noqa: E402
from deck_engine import BuildError, build_deck, load_and_plan  # noqa: E402
from deck_engine.icons import resolve_icon  # noqa: E402
from deck_engine.model import load_styles, load_themes, resolve_theme  # noqa: E402
from deck_engine.schema import contrast_ratio  # noqa: E402
from deck_engine.schema import CHART_TYPES, LAYOUT_VARIANTS, LAYOUTS  # noqa: E402

import packages.core.ai.tools.sandbox_tools as sandbox_tools  # noqa: E402
from packages.core.ai.agentic_loop import SKILL_HANDOFF_RESULT_MAX_CHARS  # noqa: E402


ALL_LAYOUTS_DECK = {
    "title": "季度经营回顾",
    "theme": "minimal",
    "footer": "Manor · 2026 Q3 经营回顾",
    "brand": "Manor · Q3 Review",
    "slides": [
        {"layout": "cover", "kicker": "2026 Q3", "title": "季度经营回顾与{下季度计划}", "subtitle": "增长质量提升，交付效率翻倍", "meta": "经营管理部 · 2026 年 10 月"},
        {"layout": "agenda", "title": "今天讨论五件事", "items": ["核心经营指标", "客户与市场进展", "交付效率提升", "风险与问题", "下季度重点计划"]},
        {"layout": "section", "title": "核心经营指标", "subtitle": "三项指标全部达标"},
        {"layout": "stats", "title": "三项核心指标全部超额完成", "lead": "收入与留存同步提升，交付周期显著缩短", "stats": [{"value": "¥1.2亿", "label": "季度收入", "note": "同比增长 46%"}, {"value": "118%", "label": "净收入留存"}, {"value": "-38%", "label": "平均交付周期", "note": "从 21 天降至 13 天"}]},
        {"layout": "chart", "title": "收入连续四个季度增长", "chart": {"type": "column", "categories": ["Q4", "Q1", "Q2", "Q3"], "series": [{"name": "收入", "values": [62, 78, 95, 120]}], "unit": "百万元"}, "takeaways": ["企业客户贡献 68% 新增收入", "渠道收入占比升至 22%"], "sources": ["https://example.com/q3-report"]},
        {"layout": "chart", "title": "收入结构以企业客户为主", "chart": {"type": "pie", "categories": ["企业", "中小", "渠道"], "series": [{"name": "占比", "values": [68, 10, 22]}]}},
        {"layout": "bullets", "kicker": "客户与市场", "title": "大客户驱动增长，渠道开始放量", "points": [{"title": "大客户", "text": "新签 12 家年合同额超 300 万的客户"}, {"title": "Channel", "text": "Two regional integrators now source 22% of new revenue"}, "品牌搜索量季度环比提升 64%"]},
        {"layout": "cards", "title": "交付效率提升来自三项改变", "items": [{"title": "标准化方案库", "text": "沉淀 40 个行业方案模板"}, {"title": "自动化测试", "text": "回归覆盖率提升到 85%"}, {"title": "项目看板", "label": "NEW"}]},
        {"layout": "process", "title": "新交付流程：五步闭环", "steps": [{"title": "需求澄清", "text": "确认目标与验收标准"}, {"title": "方案匹配"}, {"title": "敏捷实施", "text": "两周一个迭代"}, {"title": "联合验收"}, {"title": "运营移交", "text": "知识库与培训同步交付"}]},
        {"layout": "timeline", "title": "下季度关键里程碑", "events": [{"date": "10 月", "title": "发布 3.0 版本"}, {"date": "11 月", "title": "渠道大会", "text": "签约 5 家新伙伴"}, {"date": "12 月", "title": "海外正式商用"}]},
        {"layout": "comparison", "title": "海外扩张：推荐伙伴模式", "columns": [{"heading": "直营模式", "points": ["控制力强", "前期投入约 2,000 万"]}, {"heading": "伙伴模式", "points": ["借助本地渠道", "前期投入约 500 万", "6 个月起量"]}], "highlight": 1},
        {"layout": "table", "title": "各区域季度表现", "columns": ["区域", "收入（百万元）", "同比"], "rows": [["华东", "48.2", "+52%"], ["华南", 27.6, "+41%"]]},
        {"layout": "image_text", "title": "东南亚试点：本地化交付验证", "image": "images/hero.png", "caption": "新加坡客户上线现场", "points": ["三周完成部署与培训", "客户满意度 4.8 / 5"], "image_side": "left"},
        {"layout": "quote", "title": "客户怎么说", "quote": "Manor 让我们的方案交付从按月计算变成按周计算。", "attribution": "某制造业客户 CIO"},
        {"layout": "statement", "kicker": "核心判断", "title": "AI 同事将在三年内成为企业标配", "subtitle": "接下来 18 个月是窗口期", "image": "images/hero.png"},
        {"layout": "numbered", "title": "落地四步法", "items": [{"title": "诊断", "text": "识别高价值场景"}, {"title": "试点"}, {"title": "推广", "text": "复制到更多团队"}]},
        {"layout": "icon_grid", "title": "我们的核心能力", "items": [{"icon": "brain", "title": "多智能体协作", "text": "复杂任务自动分工"}, {"icon": "shield", "title": "企业级安全"}, {"icon": "no-such-icon-keyword", "title": "开放集成", "text": "连接业务系统"}, {"icon": "users", "title": "团队共享"}]},
        {"layout": "matrix", "title": "优先级矩阵", "axes": {"x": "实施难度", "y": "业务价值"}, "quadrants": [{"title": "立即推进", "text": "高价值低难度"}, {"title": "重点投入"}, {"title": "顺手完成", "text": "低价值低难度"}, {"title": "暂缓"}]},
        {"layout": "split_image", "title": "客户故事：交付效率提升 3 倍", "image": "images/hero.png", "points": [{"icon": "clock", "title": "周期", "text": "从 21 天缩短到 7 天"}, "返工率下降 60%"]},
        {"layout": "image_cards", "title": "三类典型场景", "items": [{"image": "images/hero.png", "title": "销售助理", "text": "线索整理"}, {"image": "images/hero.png", "title": "运营助理"}, {"image": "images/hero.png", "title": "客服助理", "text": "全天响应"}]},
        {"layout": "feature", "kicker": "旗舰方案", "title": "交付工作台：{一屏}看全项目", "image": "images/hero.png", "text": "把需求、进度、验收与知识沉淀放进同一个工作台。", "points": ["实时风险提示", "自动生成周报"], "tags": ["企业版", "已上线"]},
        {"layout": "progress", "title": "年度目标完成度", "items": [{"label": "收入目标", "value": 78, "note": "全年 4.8 亿"}, {"label": "新客户数", "value": 84}, {"label": "留存目标", "value": 96}]},
        {"layout": "funnel", "title": "渠道转化漏斗", "stages": [{"title": "市场询盘", "value": "12,400", "text": "官网与活动线索"}, {"title": "合格线索", "value": "4,860"}, {"title": "签约客户", "value": "386", "text": "转化率 3.1%"}]},
        {"layout": "pyramid", "title": "能力成熟度四个层次", "levels": [{"title": "引领", "text": "定义行业标准"}, {"title": "优化"}, {"title": "规范", "text": "流程可复制"}, {"title": "起步"}]},
        {"layout": "cycle", "kicker": "闭环", "title": "持续改进的循环", "steps": [{"title": "计划", "text": "设定目标"}, {"title": "执行", "icon": "rocket"}, {"title": "检查", "text": "对比数据"}, {"title": "改进"}]},
        {"layout": "venn", "title": "三种能力的交集", "sets": [{"title": "技术", "text": "工程与数据"}, {"title": "业务"}, {"title": "设计", "text": "体验与品牌"}], "center": "产品力"},
        {"layout": "team", "title": "项目核心团队", "people": [{"name": "林舒", "role": "设计负责人", "image": "images/hero.png"}, {"name": "Zhou Ming", "role": "运营与数据", "text": "负责指标体系"}, {"name": "陈悦", "role": "员工体验"}]},
        {"layout": "gallery", "title": "四个现场", "images": [{"image": "images/hero.png", "caption": "长桌"}, {"image": "images/hero.png"}, {"image": "images/hero.png", "caption": "会议室"}, {"image": "images/hero.png", "caption": "休整"}]},
        {"layout": "metric", "kicker": "交付效率", "value": "-38%", "title": "平均交付周期缩短了 {38%}", "subtitle": "从 21 天降至 13 天", "image": "images/hero.png"},
        {"layout": "logos", "title": "已服务 {120} 家企业客户", "value": "120+", "label": "家企业客户", "logos": ["华润", "Midea", "顺丰", "Anker", "蔚来", "Haier"]},
        {"layout": "pricing", "title": "三档方案按需选择", "highlight": 1, "plans": [{"name": "标准版", "price": "¥9,800", "period": "/年", "features": ["10 个席位", "标准方案库"]}, {"name": "企业版", "price": "¥39,800", "period": "/年", "badge": "推荐", "text": "适合多团队协作", "features": ["不限席位", "私有部署", "专属成功经理"]}, {"name": "旗舰版", "price": "定制", "features": ["多区域部署", "SLA 99.95%"]}]},
        {"layout": "device", "device": "laptop", "title": "交付工作台一屏看全项目", "image": "images/hero.png", "points": [{"title": "风险提示", "text": "提前两周预警"}, "自动生成周报"]},
        {"layout": "market", "title": "千亿市场，先做制造业", "tiers": [{"label": "TAM", "value": "¥1200亿", "text": "企业智能化服务"}, {"label": "SAM", "value": "¥180亿"}, {"label": "SOM", "value": "¥12亿", "text": "三年内可触达"}]},
        {"layout": "launches", "tag": "NEW", "title": "本季度上线的能力", "highlight": 1, "icon": "rocket", "items": ["方案库 3.0", "自动化验收", "多语言交付", "海外节点"]},
        {"layout": "case_study", "kicker": "客户案例", "title": "三周完成全国上线", "image": "images/hero.png", "value": "3x", "value_label": "交付效率", "sections": [{"label": "挑战", "text": "40 家门店系统各自为政，上线周期长达三个月。"}, {"label": "做法", "text": "统一方案库与自动化测试，分三批滚动上线。"}]},
        {"layout": "testimonials", "title": "客户怎么评价", "quotes": [{"quote": "交付从按月计算变成按周计算。", "name": "王磊", "role": "某制造业 CIO"}, {"quote": "Onboarding took three weeks instead of three months.", "name": "Dana Ruiz", "role": "COO", "image": "images/hero.png"}]},
        {"layout": "roadmap", "title": "明年四条主线", "periods": ["Q1", "Q2", "Q3", "Q4"], "tasks": [{"label": "海外商用", "start": 0, "end": 1, "note": "新加坡"}, {"label": "渠道伙伴", "start": 1, "end": 3}, {"label": "3.0 版本", "start": 2, "end": 2}]},
        {"layout": "closing", "title": "下季度三项承诺", "points": ["收入突破 1.5 亿元", "交付周期再缩短 20%"], "contact": "ops@manor.example", "notes": "收尾。"},
    ],
}


def _project(tmp_path: Path, deck: dict, name: str = "deck_test") -> Path:
    project = tmp_path / "projects" / name
    (project / "images").mkdir(parents=True)
    Image.new("RGB", (1600, 1000), (40, 90, 160)).save(project / "images" / "hero.png")
    (project / "deck.json").write_text(json.dumps(deck, ensure_ascii=False), encoding="utf-8")
    return project


def _gate(project: Path, pptx_path: Path) -> dict:
    return pptx_quality_gate.run_gate(
        pptx_path,
        mode="auto",
        project=project,
        render_dir=project / "qa" / "final-render",
        min_score=90,
    )


@pytest.mark.parametrize("theme", sorted(load_styles()["styles"]))
def test_every_layout_builds_and_passes_the_final_gate_in_every_theme(tmp_path: Path, theme: str) -> None:
    deck = copy.deepcopy(ALL_LAYOUTS_DECK)
    deck["theme"] = theme
    project = _project(tmp_path, deck)

    output, plans = build_deck(project / "deck.json")
    payload = _gate(project, output)

    assert payload["status"] == "pass", payload["errors"]
    assert payload["quality_score"] >= 90
    assert payload["metrics"]["pipeline"] == "template"
    assert payload["metrics"]["template_verified_slide_count"] == len(plans) == len(deck["slides"])
    prs = Presentation(str(output))
    icons = [shape for slide in prs.slides for shape in slide.shapes if shape.name.endswith("-icon")]
    assert icons, "icon layouts must place native icon shapes"
    assert output == project / "exports" / f"{project.name}.pptx"


def test_all_layouts_are_exercised_by_the_gate_fixture() -> None:
    assert {slide["layout"] for slide in ALL_LAYOUTS_DECK["slides"]} == set(LAYOUTS)


def _variant_slides() -> list[dict]:
    slides = []
    for layout, variants in LAYOUT_VARIANTS.items():
        base = next(slide for slide in ALL_LAYOUTS_DECK["slides"] if slide["layout"] == layout)
        for variant in variants:
            slide = copy.deepcopy(base)
            slide["variant"] = variant
            slides.append(slide)
    return slides


@pytest.mark.parametrize("theme", ["mono", "neon", "editorial", "keynote", "stripe"])
def test_every_layout_variant_builds_and_passes_the_gate(tmp_path: Path, theme: str) -> None:
    slides = _variant_slides()
    # A template build takes at most 40 slides, so the variants go in batches.
    for batch, start in enumerate(range(0, len(slides), 30)):
        part = slides[start : start + 30]
        deck = {"title": "变体", "theme": theme, "slides": [ALL_LAYOUTS_DECK["slides"][0], *part]}
        project = _project(tmp_path / f"batch{batch}", deck)
        output, plans = build_deck(project / "deck.json")
        payload = _gate(project, output)
        assert payload["status"] == "pass", payload["errors"]
        assert len(plans) == len(part) + 1


def test_every_chart_type_renders_a_native_chart(tmp_path: Path) -> None:
    slides = [ALL_LAYOUTS_DECK["slides"][0]]
    for chart_type in CHART_TYPES:
        series = [{"name": "收入", "values": [62, 78, 95, 120]}]
        if chart_type not in ("pie", "doughnut"):
            series.append({"name": "成本", "values": [40, 44, 50, 61]})
        chart = {"type": chart_type, "categories": ["Q4", "Q1", "Q2", "Q3"], "series": series}
        slides.append({"layout": "chart", "title": "收入与成本变化", "chart": chart, "takeaways": ["收入持续增长"], "notes": chart_type})
        # Real decks never stack a dozen charts; alternate so the rhythm check stays quiet.
        slides.append({"layout": "statement", "title": "成本增速低于收入", "notes": "过渡"})
    project = _project(tmp_path, {"title": "图表", "theme": "consulting", "slides": slides})
    output, _ = build_deck(project / "deck.json")
    payload = _gate(project, output)
    assert payload["status"] == "pass", (payload["errors"], payload["warnings"])
    rendered = list(Presentation(str(output)).slides)
    assert [sum(shape.has_chart for shape in slide.shapes) for slide in rendered[1::2]] == [1] * len(CHART_TYPES)


def test_sparklines_rings_and_highlights_are_native_charts(tmp_path: Path) -> None:
    deck = {
        "title": "数据", "theme": "consulting",
        "slides": [
            ALL_LAYOUTS_DECK["slides"][0],
            {"layout": "stats", "title": "核心指标", "stats": [{"value": "1.2亿", "label": "收入", "trend": [1, 3, 2, 5]}, {"value": "118%", "label": "留存", "trend": [100, 108, 118]}]},
            {"layout": "progress", "variant": "rings", "title": "完成度", "items": [{"label": "收入", "value": 78}, {"label": "留存", "value": 96}]},
            {"layout": "chart", "title": "上海最大", "chart": {"type": "bar", "categories": ["上海", "北京", "广州"], "series": [{"name": "人次", "values": [90, 80, 65]}], "highlight": 0}},
        ],
    }
    project = _project(tmp_path, deck)
    output, _ = build_deck(project / "deck.json")
    assert _gate(project, output)["status"] == "pass"
    slides = list(Presentation(str(output)).slides)
    assert sum(shape.has_chart for shape in slides[1].shapes) == 2
    assert sum(shape.has_chart for shape in slides[2].shapes) == 2


def test_new_forms_validate_their_items(tmp_path: Path) -> None:
    deck = {
        "title": "坏数据", "theme": "mono",
        "slides": [
            ALL_LAYOUTS_DECK["slides"][0],
            {"layout": "progress", "title": "进度", "items": [{"label": "A", "value": 140}, {"label": "B", "value": "高"}]},
            {"layout": "venn", "title": "交集", "sets": [{"title": "A"}], "center": "一个过长的交集说明文字超过十六个字符"},
            {"layout": "stats", "variant": "zigzag", "title": "指标", "stats": [{"value": "1", "label": "a", "trend": [1]}, {"value": "2", "label": "b"}]},
            {"layout": "chart", "title": "图", "chart": {"type": "line", "categories": ["a", "b"], "series": [{"name": "s", "values": [1, 2]}], "highlight": 0}},
        ],
    }
    project = _project(tmp_path, deck)
    with pytest.raises(BuildError) as excinfo:
        build_deck(project / "deck.json")
    errors = "\n".join(excinfo.value.errors)
    assert "slides[1].items[0].value: a number from 0 to 100" in errors
    assert "slides[1].items[1].value: a number from 0 to 100" in errors
    assert "slides[2].sets: 2-3 entries" in errors
    assert "slides[2].center: non-empty string up to 16 characters" in errors
    assert "slides[3].variant: one of ['panels', 'rules', 'steps', 'hero', 'bento', 'metrics']" in errors
    assert "slides[3].stats[0].trend: 3-16 numbers" in errors
    assert "slides[4].chart.highlight: index of one category" in errors


def test_template_deck_is_native_and_editable(tmp_path: Path) -> None:
    project = _project(tmp_path, copy.deepcopy(ALL_LAYOUTS_DECK))
    output, _ = build_deck(project / "deck.json")
    prs = Presentation(str(output))
    slides = list(prs.slides)

    chart_slide = slides[4]
    assert any(shape.has_chart for shape in chart_slide.shapes)
    table_slide = slides[11]
    assert any(shape.has_table for shape in table_slide.shapes)
    assert "[Sources]\nhttps://example.com/q3-report" in chart_slide.notes_slide.notes_text_frame.text
    title = next(shape for shape in slides[1].shapes if shape.name == "tpl:title")
    run = title.text_frame.paragraphs[0].runs[0]
    assert run.font.size.pt >= 32
    rpr = run._r.get_or_add_rPr()
    assert rpr.find("{http://schemas.openxmlformats.org/drawingml/2006/main}ea").get("typeface")
    for slide in slides:
        for shape in slide.shapes:
            assert shape._element.find("{http://schemas.openxmlformats.org/presentationml/2006/main}style") is None


def test_style_backdrop_is_one_picture_under_every_editable_shape(tmp_path: Path) -> None:
    deck = copy.deepcopy(ALL_LAYOUTS_DECK)
    deck["theme"] = "velvet"
    project = _project(tmp_path, deck)
    output, _ = build_deck(project / "deck.json")
    prs = Presentation(str(output))
    for slide in prs.slides:
        first = slide.shapes[0]
        assert first.name == "tpl:backdrop" and first.shape_type == 13  # picture
        assert (first.left, first.top, first.width, first.height) == (0, 0, prs.slide_width, prs.slide_height)
        assert sum(shape.name == "tpl:backdrop" for shape in slide.shapes) == 1
    # Rendered once per distinct spec (velvet's dark pages share one, content has its own).
    specs = {json.dumps(spec, sort_keys=True) for spec in load_styles()["styles"]["velvet"]["backdrop"].values()}
    assert len(list((project / ".deck_cache").glob("backdrop_*.jpg"))) == len(specs)


def test_schema_reports_every_problem_with_its_json_path(tmp_path: Path) -> None:
    deck = {
        "title": "坏的演示",
        "theme": "glitter",
        "slides": [
            {"layout": "bullets", "title": "不是封面", "points": ["只有一条"]},
            {"layout": "carousel", "title": "未知版式"},
            {"layout": "cards", "title": "卡片", "items": [{"title": "A", "body": "typo"}]},
            {"layout": "chart", "title": "图表", "chart": {"type": "sunburst", "categories": ["a"], "series": []}},
        ],
    }
    project = _project(tmp_path, deck)

    with pytest.raises(BuildError) as excinfo:
        build_deck(project / "deck.json")
    errors = "\n".join(excinfo.value.errors)

    assert "deck.theme: 'glitter' is not a template theme" in errors
    assert "slides[0].layout: the first slide must use 'cover'" in errors
    assert "slides[0].points: 2-6 entries" in errors
    assert "slides[1].layout: 'carousel' is unknown" in errors
    assert "slides[2].items: 2-4 entries" in errors
    assert "slides[2].items[0]: unknown field(s) ['body']" in errors
    assert "slides[3].chart.type: must be one of" in errors
    assert not (project / "exports").exists()


def test_text_that_cannot_fit_names_the_slide_and_slot(tmp_path: Path) -> None:
    deck = copy.deepcopy(ALL_LAYOUTS_DECK)
    deck["slides"][7]["items"][0]["text"] = "很长的说明文字" * 40
    project = _project(tmp_path, deck)

    with pytest.raises(BuildError) as excinfo:
        build_deck(project / "deck.json")

    assert any(
        error.startswith("slide 8 (cards) item-text: the 2 entries need about")
        and "remain on this slide" in error
        for error in excinfo.value.errors
    )


def test_gate_rejects_a_template_deck_edited_after_the_build(tmp_path: Path) -> None:
    project = _project(tmp_path, copy.deepcopy(ALL_LAYOUTS_DECK))
    output, _ = build_deck(project / "deck.json")
    prs = Presentation(str(output))
    title = next(shape for shape in prs.slides[1].shapes if shape.name == "tpl:title")
    title.text_frame.paragraphs[0].runs[0].text = "被手工改过的标题"
    prs.save(str(output))

    payload = _gate(project, output)

    assert payload["status"] == "fail"
    assert any("Template build: slide 2 shape 'tpl:title': text differs" in error for error in payload["errors"])
    assert "template_verified_slide_count" not in payload["metrics"]


def test_gate_rejects_template_shape_names_without_deck_json(tmp_path: Path) -> None:
    project = _project(tmp_path, copy.deepcopy(ALL_LAYOUTS_DECK))
    output, _ = build_deck(project / "deck.json")
    (project / "deck.json").unlink()

    payload = _gate(project, output)

    assert payload["status"] == "fail"
    assert "Template build: Template deck evidence missing: deck.json" in payload["errors"]


def test_build_cli_reports_ready_then_fix_list(tmp_path: Path) -> None:
    project = _project(tmp_path, copy.deepcopy(ALL_LAYOUTS_DECK))
    ready = subprocess.run(
        [sys.executable, str(SCRIPTS / "build_deck.py"), str(project / "deck.json")],
        capture_output=True, text=True, check=False,
    )
    summary = json.loads(ready.stdout)
    assert ready.returncode == 0, ready.stdout + ready.stderr
    assert summary["status"] == "ready"
    assert summary["slides"] == len(ALL_LAYOUTS_DECK["slides"])
    report = json.loads((project / "qa" / "pptx-quality.json").read_text(encoding="utf-8"))
    assert report["metrics"]["pptx_sha256"] == hashlib.sha256(Path(summary["pptx"]).read_bytes()).hexdigest()

    (project / "deck.json").write_text(json.dumps({"title": "x", "slides": []}), encoding="utf-8")
    broken = subprocess.run(
        [sys.executable, str(SCRIPTS / "build_deck.py"), str(project / "deck.json")],
        capture_output=True, text=True, check=False,
    )
    assert broken.returncode == 1
    assert json.loads(broken.stdout)["status"] == "fix_deck_json"


def test_rebuilding_the_same_deck_is_byte_stable_for_the_plan(tmp_path: Path) -> None:
    project = _project(tmp_path, copy.deepcopy(ALL_LAYOUTS_DECK))
    _deck, _theme, first = load_and_plan(project / "deck.json")
    _deck, _theme, second = load_and_plan(project / "deck.json")
    assert [[(s.name, s.box, s.text) for s in plan.shapes] for plan in first] == [
        [(s.name, s.box, s.text) for s in plan.shapes] for plan in second
    ]


# ── server-side delivery evidence ───────────────────────────────────


def _template_report(content: bytes, *, path: str, verified: int | None) -> bytes:
    metrics = {
        "pptx_sha256": hashlib.sha256(content).hexdigest(),
        "pptx_size_bytes": len(content),
        "pipeline": "template",
    }
    if verified is not None:
        metrics["template_verified_slide_count"] = verified
    return json.dumps({
        "status": "pass", "quality_score": 98, "minimum_score": 90, "pptx": path,
        "mode": "editable", "slide_count": 3, "metrics": metrics, "errors": [],
    }).encode("utf-8")


def test_delivery_accepts_template_evidence_without_a_raster_render() -> None:
    content = b"pptx-bytes"
    path = "/skill/projects/demo/exports/demo.pptx"
    error, _ = sandbox_tools._pptx_quality_evidence_error(
        file_path=path, content_bytes=content, gate_exit_code=0,
        report_bytes=_template_report(content, path=path, verified=3),
    )
    assert error is None


def test_delivery_rejects_template_evidence_that_was_not_verified() -> None:
    content = b"pptx-bytes"
    path = "/skill/projects/demo/exports/demo.pptx"
    error, _ = sandbox_tools._pptx_quality_evidence_error(
        file_path=path, content_bytes=content, gate_exit_code=0,
        report_bytes=_template_report(content, path=path, verified=None),
    )
    assert error == "not every slide was verified against its deck.json template plan"


def test_delivery_still_requires_render_evidence_for_free_design_decks() -> None:
    content = b"pptx-bytes"
    path = "/skill/projects/demo/exports/demo.pptx"
    report = json.loads(_template_report(content, path=path, verified=3))
    report["metrics"]["pipeline"] = "svg"
    error, _ = sandbox_tools._pptx_quality_evidence_error(
        file_path=path, content_bytes=content, gate_exit_code=0,
        report_bytes=json.dumps(report).encode("utf-8"),
    )
    assert error == "not every final slide has verified render evidence"


# ── instructions stay in sync with the engine ───────────────────────


def test_skill_md_documents_exactly_the_implemented_layouts_and_themes() -> None:
    instructions = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    documented_layouts = set(re.findall(r"^\| `([a-z_]+)` \| `title`", instructions, flags=re.M))
    assert documented_layouts == set(LAYOUTS)
    documented_styles = set(re.findall(r"^\| `([a-z]+)` \| (?!`)", instructions, flags=re.M))
    assert documented_styles == set(load_styles()["styles"])
    for alias, target in load_styles()["aliases"].items():
        assert f"`{alias}`→`{target}`" in instructions
    assert "python3 scripts/build_deck.py projects/<slug>/deck.json" in instructions
    assert "/skill/workflows/free-design.md" in instructions


def test_skill_md_fits_the_sandbox_handoff_without_truncation() -> None:
    instructions = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    # The handoff wraps SKILL.md in a runtime contract of a few thousand
    # characters; the whole thing must reach the model in the first delivery.
    assert len(instructions) + 8_000 < SKILL_HANDOFF_RESULT_MAX_CHARS


def test_theme_overrides_that_make_text_illegible_are_rejected(tmp_path: Path) -> None:
    deck = copy.deepcopy(ALL_LAYOUTS_DECK)
    deck["theme_overrides"] = {"colors": {"accent": "00A6A6", "primary": "C8152D"}}
    project = _project(tmp_path, deck)

    with pytest.raises(BuildError) as excinfo:
        build_deck(project / "deck.json")

    assert any(
        error.startswith("deck.theme_overrides.colors: accent 00A6A6 on background")
        and "below 3.0:1" in error
        for error in excinfo.value.errors
    )
    assert not any("primary C8152D" in error for error in excinfo.value.errors)


def test_closing_slide_fits_four_points_with_kicker_and_contact(tmp_path: Path) -> None:
    deck = {
        "title": "收尾",
        "slides": [
            {"layout": "cover", "title": "季度回顾"},
            {
                "layout": "closing",
                "kicker": "下一步",
                "title": "下季度三项承诺与关键行动",
                "subtitle": "聚焦增长质量、交付效率与海外扩张三条主线",
                "points": [
                    "收入突破 1.5 亿元，企业客户占比超过七成",
                    "交付周期再缩短 20%，上线故障下降一半",
                    "海外收入占比达到 10%，完成东南亚两国落地",
                    "建立季度复盘机制，持续追踪关键指标",
                ],
                "contact": "经营管理部 · ops@manor.example",
            },
        ],
    }
    project = _project(tmp_path, deck)

    output, _ = build_deck(project / "deck.json")

    assert _gate(project, output)["status"] == "pass"


def test_theme_overrides_cannot_replace_the_theme_palette(tmp_path: Path) -> None:
    deck = copy.deepcopy(ALL_LAYOUTS_DECK)
    deck["theme_overrides"] = {"colors": {"background": "081425", "text": "F2F7FF"}}
    project = _project(tmp_path, deck)

    with pytest.raises(BuildError) as excinfo:
        build_deck(project / "deck.json")

    assert any("theme_overrides.colors.background: only brand colors" in error for error in excinfo.value.errors)


def test_line_breaks_in_single_line_fields_are_joined_not_rejected(tmp_path: Path) -> None:
    deck = copy.deepcopy(ALL_LAYOUTS_DECK)
    deck["slides"][0]["meta"] = "经营管理部\n2026 年 10 月"
    deck["slides"][-1]["contact"] = "联系人：张老师\n邮箱：ops@manor.example"
    project = _project(tmp_path, deck)

    output, plans = build_deck(project / "deck.json")

    assert next(s for s in plans[0].shapes if s.name == "tpl:meta").text == "经营管理部 · 2026 年 10 月"
    assert next(s for s in plans[-1].shapes if s.name == "tpl:contact").text == "联系人：张老师 · 邮箱：ops@manor.example"
    assert _gate(project, output)["status"] == "pass"


@pytest.mark.parametrize("style_id", sorted(load_styles()["styles"]))
def test_every_style_role_keeps_text_legible(style_id: str) -> None:
    style = resolve_theme(style_id, None)
    for role_name in ("cover", "section", "content", "contrast", "closing"):
        role = style.role(role_name)
        assert contrast_ratio(role["text"], role["bg"]) >= 4.5, (style_id, role_name, "text")
        assert contrast_ratio(role["muted"], role["bg"]) >= 3.0, (style_id, role_name, "muted")
        assert contrast_ratio(role["accent"], role["bg"]) >= 3.0, (style_id, role_name, "accent")
    for fill, text, muted in style.card["cycle"]:
        assert contrast_ratio(style.c(text), style.c(fill)) >= 4.5, (style_id, fill, text)
        assert contrast_ratio(style.c(muted), style.c(fill)) >= 3.0, (style_id, fill, muted)


def test_icon_keywords_resolve_to_real_icons_in_every_library() -> None:
    for library in ("tabler-outline", "tabler-filled", "phosphor-duotone"):
        for keyword in ("users", "growth", "shield", "rocket", "database", "brain"):
            name = resolve_icon(keyword, library)
            assert name and not name.endswith(("/circle-dot", "/point", "/circle")), (library, keyword, name)
    assert resolve_icon("no-such-icon-keyword", "tabler-outline")


def test_percent_formats_on_a_0_to_100_scale_are_shown_as_written() -> None:
    from deck_engine.layouts import _percent_literal

    # A model writing 29.2 for 29.2% with "0.0%" would otherwise get 2920.0%.
    assert _percent_literal("0.0%", [39.2, 27.2, 29.2]) == '0.0"%"'
    assert _percent_literal("0%", [0.39, 0.27]) == "0%"
    assert _percent_literal('0.0"%"', [39.2]) == '0.0"%"'
    assert _percent_literal("#,##0", [12400]) == "#,##0"
