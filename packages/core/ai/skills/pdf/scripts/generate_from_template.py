from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

from fpdf import FPDF
from fpdf.enums import XPos, YPos
from pypdf import PdfReader


SKILL_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = SKILL_ROOT / "assets" / "templates"
_CJK_PATTERN = re.compile(r"[\u2e80-\u9fff\uf900-\ufaff\u3040-\u30ff\uac00-\ud7af]")
_DASH_TRANSLATION = str.maketrans({"\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2013": "-", "\u2014": "-"})


def _rgb(value: str) -> tuple[int, int, int]:
    value = value.strip().lstrip("#")
    if len(value) != 6:
        raise ValueError(f"Expected a six-digit hex color, got {value!r}")
    return tuple(int(value[index : index + 2], 16) for index in (0, 2, 4))  # type: ignore[return-value]


def _text(value: Any) -> str:
    return str(value if value is not None else "").translate(_DASH_TRANSLATION)


def _contains_cjk(value: Any) -> bool:
    if isinstance(value, dict):
        return any(_contains_cjk(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_cjk(item) for item in value)
    return bool(_CJK_PATTERN.search(_text(value)))


def _font_candidates() -> list[tuple[Path, Path | None, int, int]]:
    configured_regular = os.getenv("PDF_TEMPLATE_FONT_REGULAR")
    configured_bold = os.getenv("PDF_TEMPLATE_FONT_BOLD")
    configured_index = int(os.getenv("PDF_TEMPLATE_FONT_COLLECTION_INDEX", "0") or 0)
    candidates: list[tuple[Path, Path | None, int, int]] = []
    if configured_regular:
        candidates.append(
            (
                Path(configured_regular).expanduser(),
                Path(configured_bold).expanduser() if configured_bold else None,
                configured_index,
                configured_index,
            )
        )
    candidates.extend(
        [
            (
                Path("/tmp/fonts/OTF/SimplifiedChinese/SourceHanSansSC-Regular.otf"),
                Path("/tmp/fonts/OTF/SimplifiedChinese/SourceHanSansSC-Bold.otf"),
                0,
                0,
            ),
            (
                Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
                Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"),
                2,
                2,
            ),
            (
                Path("/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf"),
                Path("/usr/share/fonts/opentype/noto/NotoSansCJKsc-Bold.otf"),
                0,
                0,
            ),
            (
                Path("/System/Library/Fonts/Supplemental/Arial Unicode.ttf"),
                None,
                0,
                0,
            ),
            (Path("/System/Library/Fonts/STHeiti Medium.ttc"), None, 0, 0),
        ]
    )
    return candidates


def _resolve_unicode_fonts() -> tuple[Path, Path, int, int]:
    for regular, bold, regular_index, bold_index in _font_candidates():
        if regular.is_file():
            if bold and bold.is_file():
                return regular, bold, regular_index, bold_index
            return regular, regular, regular_index, regular_index
    raise FileNotFoundError(
        "This template contains CJK text but no compatible Unicode font was found. "
        "Set PDF_TEMPLATE_FONT_REGULAR and optionally PDF_TEMPLATE_FONT_BOLD, or install Source Han Sans/Noto Sans CJK."
    )


class TemplatePDF(FPDF):
    def __init__(self, *, palette: dict[str, str], page_size: str, orientation: str) -> None:
        super().__init__(orientation=orientation, format=page_size)
        self.palette = palette
        self.font_family = "Helvetica"
        self.footer_label = ""
        self.footer_variant = "default"
        self.set_margins(16, 15, 16)
        self.set_auto_page_break(auto=True, margin=18)
        self.alias_nb_pages()

    def configure_fonts(self, *, needs_cjk: bool) -> None:
        if not needs_cjk:
            return
        regular, bold, regular_index, bold_index = _resolve_unicode_fonts()
        self.add_font(
            "TemplateSans",
            style="",
            fname=str(regular),
            collection_font_number=regular_index,
        )
        self.add_font(
            "TemplateSans",
            style="B",
            fname=str(bold),
            collection_font_number=bold_index,
        )
        self.font_family = "TemplateSans"

    def use_font(self, size: float, *, bold: bool = False, color: str | None = None) -> None:
        self.set_font(self.font_family, "B" if bold else "", size)
        if color:
            self.set_text_color(*_rgb(self.palette[color] if color in self.palette else color))

    def footer(self) -> None:
        self.set_y(-12)
        self.set_draw_color(*_rgb(self.palette.get("line", "DCE4EC")))
        line_start = 94 if self.footer_variant == "business_report" else 16
        self.line(line_start, self.get_y(), self.w - 16, self.get_y())
        self.set_y(-9)
        self.use_font(7.5, color="muted")
        self.set_x(line_start)
        self.cell((self.w - line_start - 16) / 2, 5, self.footer_label, new_x=XPos.RIGHT, new_y=YPos.TOP)
        self.cell(0, 5, f"{self.page_no()} / {{nb}}", align="R")


def _brand_name(data: dict[str, Any]) -> str:
    brand = data.get("brand")
    if isinstance(brand, dict) and brand.get("name"):
        return _text(brand["name"])
    if isinstance(brand, str) and brand:
        return _text(brand)
    seller = data.get("seller")
    if isinstance(seller, dict) and seller.get("name"):
        return _text(seller["name"])
    journal = data.get("journal")
    if isinstance(journal, dict) and journal.get("name"):
        return _text(journal["name"])
    return "DOCUMENT STUDIO"


def _brand_descriptor(data: dict[str, Any], fallback: str) -> str:
    brand = data.get("brand")
    if isinstance(brand, dict) and brand.get("descriptor"):
        return _text(brand["descriptor"])
    return fallback


def _ensure_space(pdf: TemplatePDF, height: float) -> None:
    if pdf.get_y() + height > pdf.h - 20:
        pdf.add_page()


def _section_title(pdf: TemplatePDF, title: str, *, number: str | None = None) -> None:
    _ensure_space(pdf, 16)
    pdf.set_fill_color(*_rgb(pdf.palette["accent"]))
    pdf.rect(16, pdf.get_y() + 1, 3, 8, style="F")
    pdf.set_x(23)
    pdf.use_font(13, bold=True, color="ink")
    prefix = f"{number}  " if number else ""
    pdf.cell(0, 10, prefix + _text(title), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(2)


def _paragraph(pdf: TemplatePDF, value: Any, *, size: float = 10, color: str = "ink") -> None:
    pdf.use_font(size, color=color)
    pdf.multi_cell(0, 5.8, _text(value), align="L", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(2)


def _bullet_list(pdf: TemplatePDF, items: list[Any]) -> None:
    for item in items:
        _ensure_space(pdf, 9)
        y = pdf.get_y()
        pdf.set_fill_color(*_rgb(pdf.palette["accent"]))
        pdf.ellipse(17, y + 2.3, 2, 2, style="F")
        pdf.set_xy(23, y)
        pdf.use_font(9.5, color="ink")
        pdf.multi_cell(0, 5.4, _text(item), align="L", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.ln(1.2)


def _meta_value(data: dict[str, Any], key: str) -> str:
    return _text(data.get(key) or "-")


def _render_business_report(pdf: TemplatePDF, spec: dict[str, Any], data: dict[str, Any]) -> None:
    labels = spec["labels"]
    pdf.add_page()
    brand = _brand_name(data)
    descriptor = _brand_descriptor(data, "RESEARCH AND INTELLIGENCE")

    # Landscape editorial rail.
    pdf.set_fill_color(*_rgb(pdf.palette["primary"]))
    pdf.rect(0, 0, 82, pdf.h, style="F")
    pdf.set_fill_color(*_rgb(pdf.palette["accent"]))
    pdf.rect(0, 0, 82, 8, style="F")
    pdf.set_xy(12, 18)
    pdf.use_font(8.5, bold=True, color="accent")
    pdf.multi_cell(58, 4.8, brand, align="L")
    pdf.set_xy(12, 29)
    pdf.use_font(6.7, bold=True, color="FFFFFF")
    pdf.multi_cell(58, 4, descriptor, align="L")
    pdf.set_xy(12, 51)
    pdf.use_font(24, bold=True, color="FFFFFF")
    pdf.multi_cell(58, 10, _text(data["document_title"]), align="L")
    pdf.set_xy(12, 91)
    pdf.use_font(11, color="FFFFFF")
    pdf.multi_cell(58, 6, _text(data["subtitle"]), align="L")

    columns = [
        (labels["prepared_for"], _meta_value(data, "prepared_for")),
        (labels["prepared_by"], _meta_value(data, "prepared_by")),
        (labels["date"], _meta_value(data, "date")),
    ]
    for index, (label, value) in enumerate(columns):
        y = 134 + index * 17
        pdf.set_xy(12, y)
        pdf.use_font(6.2, bold=True, color="accent")
        pdf.cell(58, 3.5, _text(label))
        pdf.set_xy(12, y + 5)
        pdf.use_font(7.8, bold=True, color="FFFFFF")
        pdf.multi_cell(58, 4, value, align="L")

    # Signal cards.
    pdf.set_xy(96, 14)
    pdf.use_font(8, bold=True, color="muted")
    pdf.cell(0, 5, _text(labels["overview"]), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    metrics = data.get("metrics") or []
    metric_y = 25
    width = 58
    gap = 5
    for index, metric in enumerate(metrics[:3]):
        x = 96 + index * (width + gap)
        pdf.set_fill_color(*_rgb(pdf.palette["surface"]))
        pdf.rect(x, metric_y, width, 32, style="F")
        pdf.set_xy(x + 5, metric_y + 4)
        pdf.use_font(7.5, color="muted")
        pdf.cell(width - 10, 4, _text(metric.get("label")))
        pdf.set_xy(x + 5, metric_y + 11)
        pdf.use_font(19, bold=True, color="ink")
        pdf.cell(width - 10, 8, _text(metric.get("value")))
        pdf.set_xy(x + 5, metric_y + 22)
        pdf.use_font(8, bold=True, color="accent")
        pdf.cell(width - 10, 5, _text(metric.get("change")))

    # Executive readout.
    summary_y = 67
    pdf.set_fill_color(*_rgb(pdf.palette["surface"]))
    pdf.rect(96, summary_y, 184, 37, style="F")
    pdf.set_fill_color(*_rgb(pdf.palette["accent"]))
    pdf.rect(96, summary_y, 4, 37, style="F")
    pdf.set_xy(106, summary_y + 6)
    pdf.use_font(6.8, bold=True, color="muted")
    pdf.cell(164, 4, _text(labels["summary"]))
    pdf.set_xy(106, summary_y + 14)
    pdf.use_font(10, color="ink")
    pdf.multi_cell(164, 5.2, _text(data["summary"]), align="L")

    sections = data.get("sections") or []
    first = sections[0] if sections else {}
    second = sections[1] if len(sections) > 1 else {}
    box_y = 114
    for index, (x, section) in enumerate(((96, first), (190, second)), 1):
        pdf.set_draw_color(*_rgb(pdf.palette["line"]))
        pdf.rect(x, box_y, 90, 68, style="D")
        pdf.set_xy(x + 6, box_y + 6)
        pdf.use_font(7, bold=True, color="accent")
        pdf.cell(12, 5, f"0{index}")
        pdf.set_xy(x + 19, box_y + 6)
        pdf.use_font(11, bold=True, color="ink")
        pdf.cell(64, 5, _text(section.get("title") or "INSIGHT"))
        pdf.set_xy(x + 6, box_y + 17)
        pdf.use_font(8.1, color="ink")
        paragraphs = section.get("paragraphs") or []
        if paragraphs:
            pdf.multi_cell(78, 4.6, _text(paragraphs[0]), align="L")
        bullet_y = max(pdf.get_y() + 2, box_y + 37)
        for bullet in (section.get("bullets") or [])[:3]:
            pdf.set_xy(x + 7, bullet_y)
            pdf.use_font(7.2, color="muted")
            pdf.multi_cell(76, 4, f"- {_text(bullet)}", align="L")
            bullet_y = pdf.get_y() + 1

    callout = data.get("callout")
    if isinstance(callout, dict) and callout.get("text"):
        pdf.set_fill_color(*_rgb(pdf.palette["secondary"]))
        pdf.rect(190, 158, 90, 24, style="F")
        pdf.set_xy(196, 162)
        pdf.use_font(6.5, bold=True, color="ink")
        pdf.cell(77, 4, _text(callout.get("title")))
        pdf.set_xy(196, 169)
        pdf.use_font(7.2, color="ink")
        pdf.multi_cell(77, 4, _text(callout["text"]), align="L")


def _render_project_proposal(pdf: TemplatePDF, spec: dict[str, Any], data: dict[str, Any]) -> None:
    labels = spec["labels"]
    brand = _brand_name(data)
    descriptor = _brand_descriptor(data, "CREATIVE PRACTICE")
    pdf.add_page()
    pdf.set_fill_color(*_rgb(pdf.palette["primary"]))
    pdf.rect(0, 0, pdf.w, 96, style="F")
    pdf.set_fill_color(*_rgb(pdf.palette["accent"]))
    pdf.rect(154, 0, 56, 96, style="F")
    pdf.set_fill_color(*_rgb(pdf.palette["secondary"]))
    pdf.ellipse(169, 18, 26, 26, style="F")
    pdf.set_draw_color(*_rgb(pdf.palette["ink"]))
    pdf.set_line_width(0.8)
    pdf.line(166, 66, 198, 66)
    pdf.line(182, 50, 182, 82)
    pdf.set_xy(18, 18)
    pdf.use_font(8, bold=True, color="accent")
    pdf.cell(124, 5, brand, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_xy(18, 26)
    pdf.use_font(6.5, bold=True, color="FFFFFF")
    pdf.cell(124, 4, descriptor)
    pdf.set_xy(18, 37)
    pdf.use_font(7.5, bold=True, color="FFFFFF")
    pdf.cell(124, 5, _text(labels["document"]), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_xy(18, 49)
    pdf.use_font(22, bold=True, color="FFFFFF")
    pdf.multi_cell(126, 10, _text(data["project_name"]), align="L")
    pdf.set_xy(18, 80)
    pdf.use_font(9, color="FFFFFF")
    pdf.cell(126, 5, f"{_text(data['client'])}  /  {_meta_value(data, 'date')}")
    pdf.set_xy(166, 75)
    pdf.use_font(7, bold=True, color="ink")
    pdf.multi_cell(32, 4, "IDEAS SHOULD FEEL ALIVE.", align="C")

    pdf.set_y(108)
    meta = [
        (labels["client"], _text(data["client"])),
        (labels["timeline"], _meta_value(data, "timeline")),
        (labels["budget"], _meta_value(data, "budget")),
    ]
    for index, (label, value) in enumerate(meta):
        x = 16 + index * 60.5
        pdf.set_fill_color(*_rgb(pdf.palette["surface"]))
        pdf.rect(x, 108, 57, 25, style="F")
        pdf.set_xy(x + 5, 113)
        pdf.use_font(6.8, bold=True, color="muted")
        pdf.cell(47, 4, _text(label))
        pdf.set_xy(x + 5, 121)
        pdf.use_font(9.5, bold=True, color="ink")
        pdf.cell(47, 5, value)
    pdf.set_y(143)
    _section_title(pdf, labels["overview"], number="01")
    _paragraph(pdf, data["executive_summary"], size=10.5)
    _section_title(pdf, labels["goals"], number="02")
    _bullet_list(pdf, data.get("goals") or [])

    pdf.add_page()
    pdf.set_fill_color(*_rgb(pdf.palette["primary"]))
    pdf.rect(0, 0, 8, pdf.h, style="F")
    pdf.set_fill_color(*_rgb(pdf.palette["secondary"]))
    pdf.rect(8, 0, 202, 6, style="F")
    pdf.set_xy(16, 13)
    pdf.use_font(7, bold=True, color="primary")
    pdf.cell(120, 4, f"{brand}  /  {descriptor}")
    pdf.set_y(26)
    _section_title(pdf, labels["scope"], number="03")
    _bullet_list(pdf, data.get("scope") or [])
    _section_title(pdf, labels["milestones"], number="04")
    for milestone in data.get("milestones") or []:
        _ensure_space(pdf, 29)
        y = pdf.get_y()
        pdf.set_fill_color(*_rgb(pdf.palette["surface"]))
        pdf.rect(16, y, 178, 24, style="F")
        pdf.set_xy(21, y + 5)
        pdf.use_font(15, bold=True, color="accent")
        pdf.cell(15, 7, _text(milestone.get("phase")))
        pdf.set_xy(40, y + 4)
        pdf.use_font(10, bold=True, color="ink")
        pdf.cell(92, 6, _text(milestone.get("title")))
        pdf.set_xy(140, y + 4)
        pdf.use_font(8, bold=True, color="muted")
        pdf.cell(48, 6, _text(milestone.get("period")), align="R")
        pdf.set_xy(40, y + 12)
        pdf.use_font(8.5, color="muted")
        pdf.multi_cell(145, 4.5, _text(milestone.get("description")), align="L")
        pdf.set_y(y + 28)

    _section_title(pdf, labels["deliverables"], number="05")
    _bullet_list(pdf, data.get("deliverables") or [])
    assumptions = data.get("assumptions") or []
    if assumptions:
        _section_title(pdf, labels["assumptions"], number="06")
        _bullet_list(pdf, assumptions)
    if data.get("contact"):
        _ensure_space(pdf, 20)
        pdf.ln(4)
        pdf.use_font(8, color="muted")
        pdf.cell(0, 5, f"CONTACT  /  {_text(data['contact'])}", align="R")


def _entity_card(
    pdf: TemplatePDF,
    *,
    x: float,
    y: float,
    width: float,
    label: str,
    entity: dict[str, Any],
) -> None:
    pdf.set_draw_color(*_rgb(pdf.palette["line"]))
    pdf.rect(x, y, width, 36, style="D")
    pdf.set_fill_color(*_rgb(pdf.palette["accent"]))
    pdf.rect(x, y, width, 2, style="F")
    pdf.set_xy(x + 5, y + 5)
    pdf.use_font(6.8, bold=True, color="muted")
    pdf.cell(width - 10, 4, _text(label))
    pdf.set_xy(x + 5, y + 12)
    pdf.use_font(10, bold=True, color="ink")
    pdf.cell(width - 10, 5, _text(entity.get("name")))
    pdf.set_xy(x + 5, y + 20)
    pdf.use_font(8, color="muted")
    pdf.cell(width - 10, 4, _text(entity.get("address")))
    pdf.set_xy(x + 5, y + 27)
    pdf.cell(width - 10, 4, _text(entity.get("email")))


def _money(currency: str, amount: Decimal) -> str:
    return f"{currency} {amount:,.2f}"


def _decimal_compact(value: Decimal) -> str:
    return format(value.normalize(), "f")


def _render_invoice(pdf: TemplatePDF, spec: dict[str, Any], data: dict[str, Any]) -> None:
    labels = spec["labels"]
    currency = _text(data["currency"])
    seller_name = _text(data["seller"].get("name"))
    initials = "".join(part[0] for part in seller_name.split()[:2]).upper() or "GS"
    pdf.add_page()
    pdf.set_fill_color(*_rgb(pdf.palette["primary"]))
    pdf.rect(0, 0, pdf.w, 7, style="F")
    pdf.set_fill_color(*_rgb(pdf.palette["accent"]))
    pdf.rect(16, 20, 18, 18, style="F")
    pdf.set_xy(16, 25)
    pdf.use_font(9, bold=True, color="ink")
    pdf.cell(18, 6, initials, align="C")
    pdf.set_xy(40, 21)
    pdf.use_font(11, bold=True, color="ink")
    pdf.cell(60, 6, seller_name)
    pdf.set_xy(40, 29)
    pdf.use_font(6.8, bold=True, color="muted")
    pdf.cell(60, 5, "SYSTEMS / OPERATIONS / DELIVERY")
    pdf.set_xy(105, 20)
    pdf.use_font(25, bold=True, color="ink")
    pdf.cell(89, 10, _text(labels["document"]), align="R")

    meta_y = 44
    meta = [
        (labels["number"], data["invoice_number"]),
        (labels["issue_date"], data["issue_date"]),
        (labels["due_date"], data["due_date"]),
    ]
    for index, (label, value) in enumerate(meta):
        x = 16 + index * 60.5
        pdf.set_xy(x, meta_y)
        pdf.use_font(6.8, bold=True, color="muted")
        pdf.cell(56, 4, _text(label))
        pdf.set_xy(x, meta_y + 7)
        pdf.use_font(9.5, bold=True, color="ink")
        pdf.cell(56, 5, _text(value))

    _entity_card(pdf, x=16, y=66, width=86, label=labels["from"], entity=data["seller"])
    _entity_card(pdf, x=108, y=66, width=86, label=labels["bill_to"], entity=data["customer"])

    table_y = 114
    widths = [92, 18, 32, 36]
    headers = [labels["description"], labels["quantity"], labels["unit_price"], labels["amount"]]
    pdf.set_fill_color(*_rgb(pdf.palette["accent"]))
    pdf.rect(16, table_y, 178, 12, style="F")
    x = 16
    for index, (header, width) in enumerate(zip(headers, widths, strict=True)):
        pdf.set_xy(x + 3, table_y + 3)
        pdf.use_font(6.8, bold=True, color="ink")
        pdf.cell(width - 6, 6, _text(header), align="R" if index else "L")
        x += width

    subtotal = Decimal("0")
    row_y = table_y + 12
    for item in data.get("items") or []:
        quantity = Decimal(str(item.get("quantity", 0)))
        unit_price = Decimal(str(item.get("unit_price", 0)))
        amount = quantity * unit_price
        subtotal += amount
        row_height = 16
        pdf.set_draw_color(*_rgb(pdf.palette["line"]))
        pdf.line(16, row_y + row_height, 194, row_y + row_height)
        values = [
            _text(item.get("description")),
            f"{quantity:g}",
            _money(currency, unit_price),
            _money(currency, amount),
        ]
        x = 16
        for index, (value, width) in enumerate(zip(values, widths, strict=True)):
            pdf.set_xy(x + 3, row_y + 4)
            pdf.use_font(8.3, bold=index == 3, color="ink")
            if index == 0:
                pdf.multi_cell(width - 6, 4.5, value, align="L")
            else:
                pdf.cell(width - 6, 6, value, align="R")
            x += width
        row_y += row_height

    tax_rate = Decimal(str(data.get("tax_rate", 0)))
    tax = subtotal * tax_rate
    total = subtotal + tax
    totals_y = row_y + 8
    summary_rows = [
        (labels["subtotal"], _money(currency, subtotal), False),
        (f"{labels['tax']} ({_decimal_compact(tax_rate * 100)}%)", _money(currency, tax), False),
        (labels["total"], _money(currency, total), True),
    ]
    for label, value, emphasized in summary_rows:
        if emphasized:
            pdf.set_fill_color(*_rgb(pdf.palette["accent"]))
            pdf.rect(112, totals_y, 82, 13, style="F")
        pdf.set_xy(117, totals_y + 3)
        pdf.use_font(8 if not emphasized else 9, bold=True, color="ink" if emphasized else "muted")
        pdf.cell(35, 6, _text(label))
        pdf.set_xy(150, totals_y + 3)
        pdf.use_font(8.5 if not emphasized else 10, bold=True, color="ink")
        pdf.cell(39, 6, value, align="R")
        totals_y += 11 if not emphasized else 16

    notes_y = max(totals_y + 5, 216)
    if notes_y > 238:
        pdf.add_page()
        notes_y = 25
    pdf.set_fill_color(*_rgb(pdf.palette["surface"]))
    pdf.rect(16, notes_y, 86, 36, style="F")
    pdf.rect(108, notes_y, 86, 36, style="F")
    pdf.set_xy(21, notes_y + 5)
    pdf.use_font(7, bold=True, color="muted")
    pdf.cell(76, 5, _text(labels["notes"]))
    pdf.set_xy(21, notes_y + 13)
    pdf.use_font(8.5, color="ink")
    pdf.multi_cell(76, 5, _text(data.get("notes") or "-"), align="L")
    pdf.set_xy(113, notes_y + 5)
    pdf.use_font(7, bold=True, color="muted")
    pdf.cell(76, 5, _text(labels["payment"]))
    pdf.set_xy(113, notes_y + 13)
    pdf.use_font(8.5, color="ink")
    pdf.multi_cell(76, 5, _text(data.get("payment") or "-"), align="L")


def _academic_page_header(pdf: TemplatePDF, data: dict[str, Any]) -> None:
    journal = data["journal"]
    pdf.set_fill_color(*_rgb(pdf.palette["primary"]))
    pdf.rect(0, 0, pdf.w, 5, style="F")
    pdf.set_xy(16, 11)
    pdf.use_font(7.3, bold=True, color="primary")
    pdf.cell(100, 5, _text(journal.get("name")))
    pdf.set_xy(112, 11)
    pdf.use_font(7, color="muted")
    pdf.cell(82, 5, _text(journal.get("issue")), align="R")
    pdf.set_draw_color(*_rgb(pdf.palette["line"]))
    pdf.line(16, 19, 194, 19)


def _academic_section_at(
    pdf: TemplatePDF,
    *,
    x: float,
    y: float,
    width: float,
    title: str,
    paragraphs: list[Any],
) -> float:
    pdf.set_xy(x, y)
    pdf.use_font(9.4, bold=True, color="primary")
    pdf.cell(width, 6, _text(title), new_x=XPos.LEFT, new_y=YPos.NEXT)
    pdf.set_draw_color(*_rgb(pdf.palette["accent"]))
    pdf.line(x, pdf.get_y(), x + 18, pdf.get_y())
    pdf.set_y(pdf.get_y() + 3)
    for paragraph in paragraphs:
        pdf.set_x(x)
        pdf.use_font(8.5, color="ink")
        pdf.multi_cell(width, 4.55, _text(paragraph), align="J", new_x=XPos.LEFT, new_y=YPos.NEXT)
        pdf.set_y(pdf.get_y() + 2.2)
    return pdf.get_y()


def _render_academic_paper(pdf: TemplatePDF, spec: dict[str, Any], data: dict[str, Any]) -> None:
    labels = spec["labels"]
    journal = data["journal"]
    sections = data["sections"]

    # Page 1: manuscript identity, abstract, and opening two-column text.
    pdf.add_page()
    _academic_page_header(pdf, data)
    pdf.set_xy(16, 26)
    pdf.use_font(6.8, bold=True, color="accent")
    pdf.cell(178, 5, _text(journal.get("article_type")))
    pdf.set_xy(16, 36)
    pdf.use_font(22, bold=True, color="ink")
    pdf.multi_cell(178, 10.5, _text(data["title"]), align="L", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_x(16)
    pdf.set_font(pdf.font_family, "I", 10)
    pdf.set_text_color(*_rgb(pdf.palette["muted"]))
    pdf.multi_cell(178, 6, _text(data.get("subtitle")), align="L", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_y(pdf.get_y() + 4)
    pdf.set_x(16)
    pdf.use_font(9.2, bold=True, color="ink")
    pdf.multi_cell(178, 5, "  /  ".join(_text(author) for author in data["authors"]), align="L")
    pdf.set_y(pdf.get_y() + 2)
    pdf.set_x(16)
    pdf.use_font(7.5, color="muted")
    pdf.multi_cell(178, 4.2, "  |  ".join(_text(item) for item in data["affiliations"]), align="L")
    if data.get("correspondence"):
        pdf.set_x(16)
        pdf.set_font(pdf.font_family, "I", 7.5)
        pdf.set_text_color(*_rgb(pdf.palette["muted"]))
        pdf.cell(178, 4.5, _text(data["correspondence"]))

    abstract_y = max(pdf.get_y() + 8, 91)
    pdf.set_fill_color(*_rgb(pdf.palette["surface"]))
    pdf.rect(16, abstract_y, 178, 47, style="F")
    pdf.set_fill_color(*_rgb(pdf.palette["primary"]))
    pdf.rect(16, abstract_y, 3, 47, style="F")
    pdf.set_xy(24, abstract_y + 6)
    pdf.use_font(7, bold=True, color="primary")
    pdf.cell(160, 4, labels["abstract"])
    pdf.set_xy(24, abstract_y + 14)
    pdf.use_font(8.7, color="ink")
    pdf.multi_cell(161, 4.55, _text(data["abstract"]), align="J")

    keywords_y = abstract_y + 52
    pdf.set_xy(16, keywords_y)
    pdf.use_font(6.8, bold=True, color="primary")
    pdf.cell(22, 5, labels["keywords"])
    pdf.set_xy(39, keywords_y)
    pdf.set_font(pdf.font_family, "I", 7.8)
    pdf.set_text_color(*_rgb(pdf.palette["muted"]))
    pdf.multi_cell(155, 5, ", ".join(_text(keyword) for keyword in data["keywords"]), align="L")

    columns_y = keywords_y + 13
    pdf.set_draw_color(*_rgb(pdf.palette["line"]))
    pdf.line(105, columns_y, 105, 274)
    _academic_section_at(
        pdf,
        x=16,
        y=columns_y,
        width=83,
        title=labels["introduction"],
        paragraphs=sections.get("introduction") or [],
    )
    _academic_section_at(
        pdf,
        x=111,
        y=columns_y,
        width=83,
        title=labels["methods"],
        paragraphs=sections.get("methods") or [],
    )

    # Page 2: quantitative signals, results narrative, figure, and table.
    pdf.add_page()
    _academic_page_header(pdf, data)
    metrics = data.get("study_metrics") or []
    metric_y = 28
    for index, metric in enumerate(metrics[:3]):
        x = 16 + index * 61
        pdf.set_draw_color(*_rgb(pdf.palette["line"]))
        pdf.rect(x, metric_y, 56, 25, style="D")
        pdf.set_xy(x + 5, metric_y + 4)
        pdf.use_font(6.2, bold=True, color="muted")
        pdf.cell(46, 4, _text(metric.get("label")))
        pdf.set_xy(x + 5, metric_y + 11)
        pdf.use_font(16, bold=True, color="primary")
        pdf.cell(46, 8, _text(metric.get("value")))

    pdf.set_draw_color(*_rgb(pdf.palette["line"]))
    pdf.line(105, 63, 105, 145)
    _academic_section_at(
        pdf,
        x=16,
        y=64,
        width=83,
        title=labels["results"],
        paragraphs=sections.get("results") or [],
    )

    figure = data.get("figure") or {}
    pdf.set_xy(111, 64)
    pdf.use_font(7, bold=True, color="primary")
    pdf.cell(83, 5, "FIGURE 1")
    chart_y = 78
    values = figure.get("values") or []
    max_value = max((float(item.get("value", 0)) for item in values), default=1) or 1
    for index, item in enumerate(values[:3]):
        y = chart_y + index * 18
        pdf.set_xy(111, y)
        pdf.use_font(7.3, color="ink")
        pdf.cell(25, 5, _text(item.get("label")))
        pdf.set_fill_color(*_rgb(pdf.palette["surface"]))
        pdf.rect(137, y, 41, 6, style="F")
        bar_width = 41 * float(item.get("value", 0)) / max_value
        pdf.set_fill_color(*_rgb(pdf.palette["primary"]))
        if bar_width:
            pdf.rect(137, y, bar_width, 6, style="F")
        pdf.set_xy(180, y)
        pdf.use_font(7.3, bold=True, color="primary")
        pdf.cell(14, 5, f"{float(item.get('value', 0)):g} C", align="R")
    pdf.set_xy(111, 136)
    pdf.set_font(pdf.font_family, "I", 7)
    pdf.set_text_color(*_rgb(pdf.palette["muted"]))
    pdf.multi_cell(83, 4, _text(figure.get("caption")), align="L")

    table = data["results_table"]
    table_y = 158
    pdf.set_xy(16, table_y - 8)
    pdf.set_font(pdf.font_family, "I", 7.5)
    pdf.set_text_color(*_rgb(pdf.palette["muted"]))
    pdf.cell(178, 5, _text(table.get("caption")))
    widths = [54, 42, 44, 38]
    x = 16
    pdf.set_fill_color(*_rgb(pdf.palette["primary"]))
    pdf.rect(16, table_y, 178, 11, style="F")
    for header, width in zip(table["headers"], widths, strict=True):
        pdf.set_xy(x + 3, table_y + 3)
        pdf.use_font(6.5, bold=True, color="FFFFFF")
        pdf.cell(width - 6, 5, _text(header))
        x += width
    row_y = table_y + 11
    for row_index, row in enumerate(table["rows"]):
        if row_index % 2 == 0:
            pdf.set_fill_color(*_rgb(pdf.palette["surface"]))
            pdf.rect(16, row_y, 178, 12, style="F")
        x = 16
        for value, width in zip(row, widths, strict=True):
            pdf.set_xy(x + 3, row_y + 3)
            pdf.use_font(7.2, color="ink")
            pdf.cell(width - 6, 5, _text(value))
            x += width
        row_y += 12

    pdf.set_fill_color(*_rgb(pdf.palette["surface"]))
    pdf.rect(16, 219, 178, 39, style="F")
    pdf.set_xy(22, 225)
    pdf.use_font(6.8, bold=True, color="primary")
    pdf.cell(166, 4, "INTERPRETIVE SUMMARY")
    pdf.set_xy(22, 233)
    pdf.use_font(8.5, color="ink")
    pdf.multi_cell(
        166,
        4.7,
        "Connected shade produced the strongest modeled reduction, while isolated interventions showed a smaller and less consistent effect. Values are synthetic and should not be interpreted as empirical evidence.",
        align="L",
    )

    # Page 3: interpretation, limitations, data note, and references.
    pdf.add_page()
    _academic_page_header(pdf, data)
    pdf.set_draw_color(*_rgb(pdf.palette["line"]))
    pdf.line(105, 29, 105, 139)
    _academic_section_at(
        pdf,
        x=16,
        y=30,
        width=83,
        title=labels["discussion"],
        paragraphs=sections.get("discussion") or [],
    )
    conclusion_end = _academic_section_at(
        pdf,
        x=111,
        y=30,
        width=83,
        title=labels["conclusion"],
        paragraphs=sections.get("conclusion") or [],
    )
    data_note_y = max(conclusion_end + 7, 92)
    pdf.set_fill_color(*_rgb(pdf.palette["surface"]))
    pdf.rect(111, data_note_y, 83, 39, style="F")
    pdf.set_xy(117, data_note_y + 6)
    pdf.use_font(6.5, bold=True, color="primary")
    pdf.cell(71, 4, labels["data_note"])
    pdf.set_xy(117, data_note_y + 14)
    pdf.use_font(7.4, color="ink")
    pdf.multi_cell(71, 4.2, _text(data.get("data_note")), align="L")

    references_y = 154
    pdf.set_xy(16, references_y)
    pdf.use_font(10, bold=True, color="primary")
    pdf.cell(178, 6, labels["references"])
    pdf.set_draw_color(*_rgb(pdf.palette["accent"]))
    pdf.line(16, references_y + 7, 194, references_y + 7)
    references = data["references"]
    split_at = (len(references) + 1) // 2
    for column_index, column_refs in enumerate((references[:split_at], references[split_at:])):
        x = 16 if column_index == 0 else 111
        y = references_y + 13
        for reference_index, reference in enumerate(column_refs, 1 if column_index == 0 else split_at + 1):
            pdf.set_xy(x, y)
            pdf.use_font(7.5, color="ink")
            pdf.multi_cell(83, 4.2, f"{reference_index}. {_text(reference)}", align="L")
            y = pdf.get_y() + 3


_RENDERERS: dict[str, Callable[[TemplatePDF, dict[str, Any], dict[str, Any]], None]] = {
    "business_report": _render_business_report,
    "project_proposal": _render_project_proposal,
    "invoice": _render_invoice,
    "academic_paper": _render_academic_paper,
}


def list_templates(templates_dir: str | Path = TEMPLATES_DIR) -> list[dict[str, Any]]:
    manifest_path = Path(templates_dir) / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    templates = manifest.get("templates")
    if not isinstance(templates, list):
        raise ValueError("Template manifest requires a `templates` array")
    return templates


def _load_template(template_id: str, templates_dir: Path) -> dict[str, Any]:
    if not re.fullmatch(r"[a-z0-9-]+", template_id):
        raise ValueError("Template ID must contain only lowercase letters, digits, and hyphens")
    template_path = templates_dir / f"{template_id}.json"
    if not template_path.is_file():
        available = [item["id"] for item in list_templates(templates_dir)]
        raise ValueError(f"Unknown template {template_id!r}; available templates: {available}")
    spec = json.loads(template_path.read_text(encoding="utf-8"))
    if spec.get("id") != template_id:
        raise ValueError(f"Template file ID mismatch in {template_path}")
    return spec


def generate_from_template(
    template_id: str,
    data_path: str | Path,
    output_pdf_path: str | Path,
    *,
    templates_dir: str | Path = TEMPLATES_DIR,
) -> dict[str, Any]:
    template_root = Path(templates_dir).expanduser().resolve()
    input_path = Path(data_path).expanduser().resolve()
    output_path = Path(output_pdf_path).expanduser().resolve()
    if input_path == output_path:
        raise ValueError("Template data and PDF output paths must differ")
    if output_path.suffix.lower() != ".pdf":
        raise ValueError("Template output must use a .pdf extension")

    spec = _load_template(template_id, template_root)
    data = json.loads(input_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Template data must contain a JSON object")
    missing = [key for key in spec.get("required", []) if key not in data]
    if missing:
        raise ValueError(f"Missing required template data: {missing}")
    renderer_name = spec.get("renderer")
    renderer = _RENDERERS.get(str(renderer_name))
    if renderer is None:
        raise ValueError(f"Unsupported template renderer: {renderer_name!r}")

    pdf = TemplatePDF(
        palette=spec["palette"],
        page_size=spec.get("page_size", "A4"),
        orientation=spec.get("orientation", "P"),
    )
    needs_cjk = _contains_cjk(spec.get("labels")) or _contains_cjk(data)
    pdf.configure_fonts(needs_cjk=needs_cjk)
    if not needs_cjk and spec.get("font_family"):
        pdf.font_family = _text(spec["font_family"])
    pdf.footer_label = _brand_name(data)
    pdf.footer_variant = template_id.replace("-", "_")
    pdf.set_title(_text(data.get("document_title") or data.get("project_name") or data.get("title") or template_id))
    authors = data.get("authors")
    author = ", ".join(_text(item) for item in authors) if isinstance(authors, list) else ""
    pdf.set_author(author or _text(data.get("prepared_by") or data.get("seller", {}).get("name") or _brand_name(data)))
    pdf.set_creator("PDF Template Engine")
    renderer(pdf, spec, data)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix=f".{output_path.stem}-",
            suffix=".tmp.pdf",
            dir=output_path.parent,
            delete=False,
        ) as stream:
            temporary_path = Path(stream.name)
        pdf.output(str(temporary_path))
        reader = PdfReader(str(temporary_path), strict=False)
        page_count = len(reader.pages)
        if page_count < 1:
            raise ValueError("Generated PDF has no pages")
        os.replace(temporary_path, output_path)
        output_path.chmod(0o644)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)

    return {
        "template": template_id,
        "output": str(output_path),
        "page_count": page_count,
        "bytes": output_path.stat().st_size,
        "validated": True,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate polished PDFs from reusable template data")
    subparsers = parser.add_subparsers(dest="command", required=True)
    list_parser = subparsers.add_parser("list", help="List installed PDF templates")
    list_parser.add_argument("--templates-dir", default=str(TEMPLATES_DIR))
    render_parser = subparsers.add_parser("render", help="Render a PDF template")
    render_parser.add_argument("template_id")
    render_parser.add_argument("data_json")
    render_parser.add_argument("output_pdf")
    render_parser.add_argument("--templates-dir", default=str(TEMPLATES_DIR))
    args = parser.parse_args()

    if args.command == "list":
        print(json.dumps({"templates": list_templates(args.templates_dir)}, ensure_ascii=False))
        return
    result = generate_from_template(
        args.template_id,
        args.data_json,
        args.output_pdf,
        templates_dir=args.templates_dir,
    )
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
