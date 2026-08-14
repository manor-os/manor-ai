from __future__ import annotations

import argparse
import json
import math
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from fpdf import FPDF
from fpdf.enums import XPos, YPos
from PIL import Image, ImageOps
from pypdf import PdfReader

try:
    from .generate_from_template import _contains_cjk, _resolve_unicode_fonts, _rgb, _text
except ImportError:  # pragma: no cover - direct script execution
    from generate_from_template import _contains_cjk, _resolve_unicode_fonts, _rgb, _text


DEFAULT_THEME = {
    "primary": "245783",
    "accent": "C7931D",
    "ink": "20252B",
    "muted": "687078",
    "line": "D2D6DA",
    "surface": "F3F4F5",
    "paper": "FFFFFF",
    "success": "DDEED8",
    "warning": "FFF0C8",
    "info": "E4EEF8",
}
ALLOWED_BLOCKS = {"paragraph", "key_value_table", "callout", "image", "gallery", "contact_list", "spacer"}
URL_PATTERN = re.compile(r"^(?:https?://|mailto:|tel:)", re.IGNORECASE)


def _theme(payload: Any) -> dict[str, str]:
    result = dict(DEFAULT_THEME)
    if payload is None:
        return result
    if not isinstance(payload, dict):
        raise ValueError("theme must be an object")
    for key, value in payload.items():
        if key not in result:
            raise ValueError(f"Unknown theme color: {key}")
        _rgb(str(value))
        result[key] = str(value).strip().lstrip("#").upper()
    return result


def _require_text(value: Any, label: str) -> str:
    text = _text(value).strip()
    if not text:
        raise ValueError(f"{label} must be a non-empty string")
    return text


def _normalize_rows(value: Any, label: str) -> list[dict[str, str]]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"{label} must be a non-empty list")
    rows: list[dict[str, str]] = []
    for index, item in enumerate(value):
        if isinstance(item, (list, tuple)) and len(item) == 2:
            item = {"label": item[0], "value": item[1]}
        if not isinstance(item, dict):
            raise ValueError(f"{label}[{index}] must be an object or [label, value]")
        rows.append(
            {
                "label": _require_text(item.get("label"), f"{label}[{index}].label"),
                "value": _require_text(item.get("value"), f"{label}[{index}].value"),
                "link": _text(item.get("link")).strip(),
            }
        )
    return rows


def validate_recipe(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("Visual document recipe must be a JSON object")
    normalized = dict(payload)
    normalized["title"] = _require_text(payload.get("title"), "title")
    normalized["theme"] = _theme(payload.get("theme"))
    page_size = _text(payload.get("page_size") or "LETTER").upper()
    if page_size not in {"A4", "LETTER"}:
        raise ValueError("page_size must be A4 or LETTER")
    normalized["page_size"] = page_size
    sections = payload.get("sections")
    if not isinstance(sections, list) or not sections:
        raise ValueError("sections must be a non-empty list")

    image_count = 0
    block_count = 0
    for section_index, section in enumerate(sections):
        if not isinstance(section, dict):
            raise ValueError(f"sections[{section_index}] must be an object")
        _require_text(section.get("title"), f"sections[{section_index}].title")
        blocks = section.get("blocks")
        if not isinstance(blocks, list) or not blocks:
            raise ValueError(f"sections[{section_index}].blocks must be a non-empty list")
        for block_index, block in enumerate(blocks):
            if not isinstance(block, dict):
                raise ValueError(f"sections[{section_index}].blocks[{block_index}] must be an object")
            block_type = _text(block.get("type")).strip()
            if block_type not in ALLOWED_BLOCKS:
                raise ValueError(f"Unsupported visual document block: {block_type!r}")
            block_count += 1
            if block_type == "key_value_table":
                _normalize_rows(block.get("rows"), f"sections[{section_index}].blocks[{block_index}].rows")
            elif block_type == "image":
                _require_text(block.get("path"), f"sections[{section_index}].blocks[{block_index}].path")
                image_count += 1
            elif block_type == "gallery":
                items = block.get("items")
                if not isinstance(items, list) or not items:
                    raise ValueError(f"sections[{section_index}].blocks[{block_index}].items must be non-empty")
                for item_index, item in enumerate(items):
                    if not isinstance(item, dict):
                        raise ValueError("gallery items must be objects")
                    _require_text(item.get("path"), f"gallery.items[{item_index}].path")
                image_count += len(items)
            elif block_type == "contact_list":
                items = block.get("items")
                if not isinstance(items, list) or not items:
                    raise ValueError("contact_list.items must be a non-empty list")
            elif block_type not in {"spacer"}:
                _require_text(block.get("text"), f"sections[{section_index}].blocks[{block_index}].text")
    normalized["_block_count"] = block_count
    normalized["_image_count"] = image_count
    return normalized


class VisualDocumentPDF(FPDF):
    def __init__(self, *, page_size: str, theme: dict[str, str], needs_cjk: bool, footer_label: str) -> None:
        super().__init__(orientation="P", unit="mm", format=page_size)
        self.theme = theme
        self.footer_label = footer_label
        self.font_family = "Helvetica"
        self.set_margins(16, 15, 16)
        self.set_auto_page_break(False)
        self.alias_nb_pages()
        if needs_cjk:
            regular, bold, regular_index, bold_index = _resolve_unicode_fonts()
            self.add_font("VisualSans", "", str(regular), collection_font_number=regular_index)
            self.add_font("VisualSans", "B", str(bold), collection_font_number=bold_index)
            self.font_family = "VisualSans"

    def color(self, name_or_hex: str) -> tuple[int, int, int]:
        return _rgb(self.theme.get(name_or_hex, name_or_hex))

    def use_font(self, size: float, *, bold: bool = False, color: str = "ink") -> None:
        self.set_font(self.font_family, "B" if bold else "", size)
        self.set_text_color(*self.color(color))

    def footer(self) -> None:
        self.set_y(-11)
        self.set_draw_color(*self.color("line"))
        self.set_line_width(0.25)
        self.line(16, self.get_y(), self.w - 16, self.get_y())
        self.set_y(-8.5)
        self.use_font(7.3, color="muted")
        self.set_x(16)
        self.cell((self.w - 32) * 0.72, 4, self.footer_label)
        self.cell(0, 4, f"{self.page_no()} / {{nb}}", align="R")


class VisualDocumentComposer:
    def __init__(self, pdf: VisualDocumentPDF, *, recipe_path: Path, temp_dir: Path) -> None:
        self.pdf = pdf
        self.recipe_path = recipe_path
        self.temp_dir = temp_dir
        self.content_bottom = pdf.h - 16
        self.current_section = ""
        self.image_serial = 0

    @property
    def content_width(self) -> float:
        return self.pdf.w - 32

    def wrap(self, text: Any, width: float) -> list[str]:
        value = _text(text).replace("\r", "")
        result: list[str] = []
        for paragraph in value.split("\n"):
            if not paragraph:
                result.append("")
                continue
            tokens = re.findall(r"[\u2e80-\u9fff\uf900-\ufaff]|[^\s\u2e80-\u9fff\uf900-\ufaff]+|\s+", paragraph)
            line = ""
            for token in tokens:
                candidate = line + token
                if self.pdf.get_string_width(candidate.rstrip()) <= width:
                    line = candidate
                    continue
                if line.strip():
                    result.append(line.rstrip())
                    line = token.lstrip()
                else:
                    remainder = token
                    while remainder:
                        cut = 1
                        while cut < len(remainder) and self.pdf.get_string_width(remainder[: cut + 1]) <= width:
                            cut += 1
                        result.append(remainder[:cut])
                        remainder = remainder[cut:]
                    line = ""
            if line.strip() or not result:
                result.append(line.rstrip())
        return result or [""]

    def text_height(self, text: Any, width: float, *, size: float, bold: bool = False, line_h: float = 5.2) -> float:
        self.pdf.use_font(size, bold=bold)
        return max(line_h, len(self.wrap(text, width)) * line_h)

    def draw_text(
        self,
        x: float,
        y: float,
        width: float,
        text: Any,
        *,
        size: float,
        line_h: float,
        bold: bool = False,
        color: str = "ink",
        link: str = "",
    ) -> float:
        self.pdf.use_font(size, bold=bold, color=color)
        lines = self.wrap(text, width)
        for index, line in enumerate(lines):
            ly = y + index * line_h
            self.pdf.set_xy(x, ly)
            self.pdf.cell(width, line_h, line, link=link or "")
        return max(line_h, len(lines) * line_h)

    def new_page(self, *, continuation: bool = False) -> None:
        self.pdf.add_page()
        self.pdf.set_fill_color(*self.pdf.color("paper"))
        self.pdf.rect(0, 0, self.pdf.w, self.pdf.h, style="F")
        self.pdf.set_y(15)
        if continuation and self.current_section:
            continuation_label = "续" if _contains_cjk(self.current_section) else "continued"
            self.pdf.use_font(8, bold=True, color="muted")
            self.pdf.cell(
                0,
                5,
                f"{self.current_section} · {continuation_label}",
                new_x=XPos.LMARGIN,
                new_y=YPos.NEXT,
            )
            self.pdf.set_draw_color(*self.pdf.color("accent"))
            self.pdf.line(16, self.pdf.get_y() + 1, self.pdf.w - 16, self.pdf.get_y() + 1)
            self.pdf.ln(5)

    def ensure_space(self, height: float, *, continuation: bool = True) -> None:
        if self.pdf.get_y() + height > self.content_bottom:
            self.new_page(continuation=continuation)

    def document_header(self, data: dict[str, Any]) -> None:
        self.new_page()
        brand = _text(data.get("brand") or "").strip()
        if brand:
            self.pdf.use_font(9, bold=True, color="primary")
            self.pdf.cell(0, 5, brand, align="C", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            self.pdf.ln(2)
        title = data["title"]
        title_size = 23
        while title_size > 17:
            self.pdf.use_font(title_size, bold=True)
            if self.pdf.get_string_width(title) <= self.content_width:
                break
            title_size -= 1
        self.pdf.use_font(title_size, bold=True, color="primary")
        self.pdf.cell(0, 11, title, align="C", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        subtitle = _text(data.get("subtitle")).strip()
        if subtitle:
            self.pdf.use_font(10.5, color="muted")
            self.pdf.multi_cell(0, 5.8, subtitle, align="C", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        meta = data.get("meta") or []
        if isinstance(meta, dict):
            meta = [f"{key}: {value}" for key, value in meta.items()]
        if meta:
            self.pdf.use_font(8.8, color="muted")
            self.pdf.multi_cell(0, 5, " | ".join(_text(item) for item in meta), align="C", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        self.pdf.ln(3)
        self.pdf.set_draw_color(*self.pdf.color("accent"))
        self.pdf.set_line_width(0.6)
        self.pdf.line(16, self.pdf.get_y(), self.pdf.w - 16, self.pdf.get_y())
        self.pdf.ln(6)

    def start_section(self, section: dict[str, Any], first_block_height: float) -> None:
        title = _require_text(section.get("title"), "section.title")
        subtitle = _text(section.get("subtitle")).strip()
        header_height = 23 if subtitle else 16
        maximum_first_block = self.content_bottom - 15 - header_height
        keep_with_height = first_block_height if first_block_height <= maximum_first_block else 45
        self.ensure_space(header_height + keep_with_height, continuation=False)
        self.current_section = title
        y = self.pdf.get_y()
        self.pdf.set_fill_color(*self.pdf.color("primary"))
        self.pdf.rect(16, y, self.content_width, 12, style="F")
        self.draw_text(20, y + 2.5, self.content_width - 8, title, size=14.5, line_h=6, bold=False, color="paper")
        self.pdf.set_y(y + 14)
        if subtitle:
            self.draw_text(18, self.pdf.get_y(), self.content_width - 4, subtitle, size=8.8, line_h=4.8, color="muted")
            self.pdf.set_y(self.pdf.get_y() + 7)
        self.pdf.ln(2)

    def estimate_block(self, block: dict[str, Any]) -> float:
        block_type = block["type"]
        if block_type == "paragraph":
            heading = _text(block.get("heading")).strip()
            return self.text_height(block.get("text"), self.content_width, size=9.5) + (9 if heading else 0) + 5
        if block_type == "key_value_table":
            rows = _normalize_rows(block.get("rows"), "rows")
            label_width = self.content_width * float(block.get("label_width", 0.2))
            total = 0.0
            for row in rows:
                label_h = self.text_height(row["label"], label_width - 6, size=8.5, line_h=4.7)
                value_h = self.text_height(row["value"], self.content_width - label_width - 8, size=9.1, line_h=4.9)
                total += max(10, label_h + 5, value_h + 5)
            return total
        if block_type == "callout":
            return self.text_height(block.get("text"), self.content_width - 12, size=8.8, line_h=4.8) + 12
        if block_type == "image":
            return float(block.get("height", 82)) + (8 if block.get("caption") else 0) + 5
        if block_type == "gallery":
            return self.gallery_height(block)
        if block_type == "contact_list":
            items = block.get("items") or []
            if not items or not isinstance(items[0], dict):
                return 20
            first = items[0]
            details_height = 5 if _text(first.get("details")).strip() else 0
            note_height = self.text_height(first.get("note"), self.content_width, size=8.5, line_h=4.7)
            return 10 + details_height + note_height
        return float(block.get("height", 6))

    def render_paragraph(self, block: dict[str, Any]) -> None:
        height = self.estimate_block(block)
        self.ensure_space(height)
        heading = _text(block.get("heading")).strip()
        if heading:
            self.pdf.use_font(11, bold=True, color="primary")
            self.pdf.cell(0, 7, heading, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        y = self.pdf.get_y()
        used = self.draw_text(16, y, self.content_width, block.get("text"), size=9.5, line_h=5.2, color="ink")
        self.pdf.set_y(y + used + 5)

    def render_key_value_table(self, block: dict[str, Any]) -> None:
        rows = _normalize_rows(block.get("rows"), "rows")
        label_width = self.content_width * float(block.get("label_width", 0.2))
        value_width = self.content_width - label_width
        total_height = self.estimate_block(block)
        if total_height <= self.content_bottom - 22:
            self.ensure_space(total_height)
        for index, row in enumerate(rows):
            label_h = self.text_height(row["label"], label_width - 6, size=8.5, line_h=4.7)
            value_h = self.text_height(row["value"], value_width - 8, size=9.1, line_h=4.9)
            row_h = max(10.5, label_h + 5, value_h + 5)
            self.ensure_space(row_h + 1)
            y = self.pdf.get_y()
            fill = "paper" if index % 2 == 0 else "surface"
            self.pdf.set_fill_color(*self.pdf.color(fill))
            self.pdf.set_draw_color(*self.pdf.color("line"))
            self.pdf.rect(16, y, label_width, row_h, style="DF")
            self.pdf.rect(16 + label_width, y, value_width, row_h, style="DF")
            self.draw_text(19, y + 2.5, label_width - 6, row["label"], size=8.5, line_h=4.7, color="muted")
            link = row["link"]
            if link and not URL_PATTERN.match(link):
                raise ValueError(f"Unsupported row link: {link}")
            self.draw_text(19 + label_width, y + 2.5, value_width - 8, row["value"], size=9.1, line_h=4.9, color="ink", link=link)
            self.pdf.set_y(y + row_h)
        self.pdf.ln(4)

    def render_callout(self, block: dict[str, Any]) -> None:
        tone = _text(block.get("tone") or "info").lower()
        if tone not in {"success", "warning", "info", "surface"}:
            raise ValueError(f"Unsupported callout tone: {tone}")
        title = _text(block.get("title")).strip()
        text = _require_text(block.get("text"), "callout.text")
        title_h = 5 if title else 0
        body_h = self.text_height(text, self.content_width - 12, size=8.8, line_h=4.8)
        height = 8 + title_h + body_h
        self.ensure_space(height + 5)
        y = self.pdf.get_y()
        self.pdf.set_fill_color(*self.pdf.color(tone))
        self.pdf.rect(16, y, self.content_width, height, style="F")
        self.pdf.set_fill_color(*self.pdf.color("accent"))
        self.pdf.rect(16, y, 2.2, height, style="F")
        ty = y + 3.5
        if title:
            self.draw_text(21, ty, self.content_width - 10, title, size=9, line_h=4.8, bold=True, color="primary")
            ty += 5
        self.draw_text(21, ty, self.content_width - 10, text, size=8.8, line_h=4.8, color="muted")
        self.pdf.set_y(y + height + 5)

    def resolve_image(self, value: Any) -> Path:
        raw = _require_text(value, "image.path")
        if re.match(r"^[a-z]+://", raw, re.IGNORECASE):
            raise ValueError("Remote image URLs are not supported; prepare a local asset first")
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = (self.recipe_path.parent / path).resolve()
        if not path.is_file():
            raise FileNotFoundError(f"Image not found: {path}")
        with Image.open(path) as image:
            image.verify()
        return path

    def prepared_image(self, item: dict[str, Any], width_mm: float, height_mm: float) -> Path:
        source = self.resolve_image(item.get("path"))
        fit = _text(item.get("fit") or "cover").lower()
        if fit not in {"cover", "contain"}:
            raise ValueError("image.fit must be cover or contain")
        self.image_serial += 1
        output = self.temp_dir / f"image-{self.image_serial:04d}.jpg"
        target = (max(320, round(width_mm / 25.4 * 180)), max(240, round(height_mm / 25.4 * 180)))
        focal_x = min(1.0, max(0.0, float(item.get("focal_x", 0.5))))
        focal_y = min(1.0, max(0.0, float(item.get("focal_y", 0.5))))
        with Image.open(source) as image:
            image = ImageOps.exif_transpose(image).convert("RGB")
            if fit == "cover":
                prepared = ImageOps.fit(image, target, method=Image.Resampling.LANCZOS, centering=(focal_x, focal_y))
            else:
                image.thumbnail(target, Image.Resampling.LANCZOS)
                prepared = Image.new("RGB", target, "white")
                prepared.paste(image, ((target[0] - image.width) // 2, (target[1] - image.height) // 2))
            prepared.save(output, "JPEG", quality=91, optimize=True)
        return output

    def draw_image(self, item: dict[str, Any], x: float, y: float, width: float, height: float) -> None:
        prepared = self.prepared_image(item, width, height)
        self.pdf.set_draw_color(*self.pdf.color("line"))
        self.pdf.set_line_width(0.35)
        self.pdf.rect(x, y, width, height)
        self.pdf.image(str(prepared), x=x + 0.5, y=y + 0.5, w=width - 1, h=height - 1)

    def render_image(self, block: dict[str, Any]) -> None:
        height = min(float(block.get("height", 82)), self.content_bottom - 20)
        caption = _text(block.get("caption")).strip()
        extra = (8 if caption else 0) + 5
        total = height + extra
        remaining = self.content_bottom - self.pdf.get_y()
        minimum_height = max(35, float(block.get("min_height", 52)))
        if total > remaining and remaining >= minimum_height + extra:
            height = remaining - extra
            total = height + extra
        else:
            self.ensure_space(total)
        y = self.pdf.get_y()
        self.draw_image(block, 16, y, self.content_width, height)
        self.pdf.set_y(y + height + 1.5)
        if caption:
            self.pdf.use_font(7.8, color="muted")
            self.pdf.cell(0, 5, caption, align="C", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        self.pdf.ln(4)

    def gallery_geometry(self, block: dict[str, Any], item_count: int) -> tuple[int, float, float, float]:
        requested = block.get("columns")
        columns = int(requested) if requested else (1 if item_count == 1 else 2 if item_count in {2, 4} else 3)
        columns = min(4, max(1, columns))
        gap = float(block.get("gap", 3))
        cell_width = (self.content_width - gap * (columns - 1)) / columns
        aspect = float(block.get("aspect_ratio", 1.5))
        if aspect <= 0:
            raise ValueError("gallery.aspect_ratio must be positive")
        image_height = cell_width / aspect
        rows = math.ceil(item_count / columns)
        caption_h = 7
        total = rows * (image_height + caption_h) + (rows - 1) * gap
        max_height = self.content_bottom - 22
        if total > max_height:
            image_height = max(28, (max_height - rows * caption_h - (rows - 1) * gap) / rows)
            total = rows * (image_height + caption_h) + (rows - 1) * gap
        return columns, gap, image_height, total

    def gallery_height(self, block: dict[str, Any]) -> float:
        items = block.get("items") or []
        chunks = [items[index : index + 6] for index in range(0, len(items), 6)]
        return sum(self.gallery_geometry(block, len(chunk))[3] + 3 for chunk in chunks)

    def render_gallery(self, block: dict[str, Any]) -> None:
        items = block.get("items") or []
        for chunk_index in range(0, len(items), 6):
            chunk = items[chunk_index : chunk_index + 6]
            columns, gap, image_height, total = self.gallery_geometry(block, len(chunk))
            self.ensure_space(total + 3)
            start_y = self.pdf.get_y()
            cell_width = (self.content_width - gap * (columns - 1)) / columns
            for index, item in enumerate(chunk):
                col = index % columns
                row = index // columns
                x = 16 + col * (cell_width + gap)
                y = start_y + row * (image_height + 7 + gap)
                self.draw_image(item, x, y, cell_width, image_height)
                caption = _text(item.get("caption")).strip()
                if caption:
                    self.draw_text(x, y + image_height + 1, cell_width, caption, size=7.4, line_h=4.2, color="muted")
            self.pdf.set_y(start_y + total + 3)

    def render_contact_list(self, block: dict[str, Any]) -> None:
        items = block.get("items") or []
        for item in items:
            if not isinstance(item, dict):
                raise ValueError("contact_list items must be objects")
            name = _require_text(item.get("name"), "contact.name")
            details = _text(item.get("details")).strip()
            note = _text(item.get("note")).strip()
            height = 7 + (5 if details else 0) + self.text_height(note, self.content_width, size=8.5, line_h=4.7) + 3
            self.ensure_space(height)
            y = self.pdf.get_y()
            self.draw_text(18, y, self.content_width - 4, name, size=10.2, line_h=5.2, color="primary")
            y += 5.5
            if details:
                self.draw_text(18, y, self.content_width - 4, details, size=8.4, line_h=4.6, color="muted")
                y += 5
            if note:
                used = self.draw_text(18, y, self.content_width - 4, note, size=8.5, line_h=4.7, color="muted")
                y += used
            self.pdf.set_draw_color(*self.pdf.color("line"))
            self.pdf.line(18, y + 1.5, self.pdf.w - 18, y + 1.5)
            self.pdf.set_y(y + 4.5)
        self.pdf.ln(2)

    def render_block(self, block: dict[str, Any]) -> None:
        block_type = block["type"]
        renderer = getattr(self, f"render_{block_type}", None)
        if renderer is None:
            if block_type == "spacer":
                height = max(0, min(30, float(block.get("height", 6))))
                self.ensure_space(height)
                self.pdf.ln(height)
                return
            raise ValueError(f"Unsupported block: {block_type}")
        renderer(block)

    def render(self, data: dict[str, Any]) -> None:
        self.document_header(data)
        for section in data["sections"]:
            blocks = section["blocks"]
            first_height = self.estimate_block(blocks[0])
            self.start_section(section, first_height)
            for block in blocks:
                self.render_block(block)


def generate_visual_document(data_path: str | Path, output_path: str | Path) -> dict[str, Any]:
    source = Path(data_path).expanduser().resolve()
    destination = Path(output_path).expanduser().resolve()
    payload = validate_recipe(json.loads(source.read_text(encoding="utf-8")))
    destination.parent.mkdir(parents=True, exist_ok=True)
    footer_label = _text(payload.get("footer") or payload.get("brand") or payload["title"])
    pdf = VisualDocumentPDF(
        page_size=payload["page_size"],
        theme=payload["theme"],
        needs_cjk=_contains_cjk(payload),
        footer_label=footer_label,
    )
    pdf.set_title(payload["title"])
    pdf.set_author(_text(payload.get("author") or payload.get("brand") or ""))
    pdf.set_creator(_text(payload.get("creator") or "Visual Document Composer"))
    pdf.set_subject(_text(payload.get("subtitle") or ""))

    with tempfile.TemporaryDirectory(prefix="visual-document-") as scratch:
        composer = VisualDocumentComposer(pdf, recipe_path=source, temp_dir=Path(scratch))
        composer.render(payload)
        fd, temp_name = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent)
        os.close(fd)
        temp_output = Path(temp_name)
        try:
            pdf.output(str(temp_output))
            reader = PdfReader(temp_output)
            if not reader.pages:
                raise ValueError("Generated visual document has no pages")
            os.chmod(temp_output, 0o644)
            os.replace(temp_output, destination)
        finally:
            temp_output.unlink(missing_ok=True)

    return {
        "path": str(destination),
        "page_count": len(PdfReader(destination).pages),
        "block_count": payload["_block_count"],
        "image_count": payload["_image_count"],
        "validated": True,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Compose an image-first PDF from semantic content blocks")
    parser.add_argument("data_json")
    parser.add_argument("output_pdf")
    args = parser.parse_args()
    print(json.dumps(generate_visual_document(args.data_json, args.output_pdf), ensure_ascii=False))


if __name__ == "__main__":
    main()
