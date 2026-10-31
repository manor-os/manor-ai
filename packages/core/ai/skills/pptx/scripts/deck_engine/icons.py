"""Bundled SVG icons as native, recolorable PowerPoint shapes.

``resolve_icon`` maps a free-form English keyword ("shield", "users", "chart")
to an icon in the style's library, so deck.json never needs exact file names.
``icon_shape_xml`` turns that icon into one ``p:sp`` with custom geometry whose
frame is exactly the planned box, which keeps template verification exact.
"""

from __future__ import annotations

import functools
import re
from dataclasses import dataclass
from pathlib import Path

from defusedxml import ElementTree as SafeET

ICONS_DIR = Path(__file__).resolve().parents[2] / "templates" / "icons"
STYLE_LIBRARIES = ("tabler-outline", "tabler-filled", "phosphor-duotone", "chunk-filled")
EMU_PER_PX = 9525
_SVG_NS = "{http://www.w3.org/2000/svg}"
_TAGS = re.compile(r"tags:\s*\[([^\]]*)\]")

# Common deck concepts → icon names present in the tabler/phosphor libraries.
SYNONYMS = {
    "growth": "trending-up", "increase": "trending-up", "revenue": "coin", "money": "coin", "cost": "receipt",
    "finance": "building-bank", "profit": "chart-line", "data": "database", "analytics": "chart-bar",
    "chart": "chart-bar", "report": "report-analytics", "security": "shield-check", "safety": "shield-check",
    "privacy": "lock", "team": "users", "people": "users", "customer": "user-heart", "user": "user",
    "ai": "brain", "intelligence": "brain", "idea": "bulb", "innovation": "bulb", "goal": "target",
    "target": "target", "strategy": "chess", "speed": "bolt", "fast": "bolt", "time": "clock",
    "schedule": "calendar", "global": "world", "world": "world", "cloud": "cloud", "code": "code",
    "api": "api", "integration": "plug-connected", "workflow": "git-branch", "process": "arrows-right",
    "settings": "settings", "tool": "tool", "support": "headset", "service": "headset", "message": "message",
    "chat": "message-circle", "email": "mail", "search": "search", "quality": "award", "award": "award",
    "check": "circle-check", "success": "circle-check", "risk": "alert-triangle", "warning": "alert-triangle",
    "launch": "rocket", "start": "rocket", "education": "school", "learning": "book", "research": "flask",
    "health": "heart-rate-monitor", "eco": "leaf", "green": "leaf", "nature": "leaf", "tea": "cup",
    "coffee": "coffee", "shop": "shopping-cart", "ecommerce": "shopping-cart", "store": "building-store",
    "location": "map-pin", "map": "map", "home": "home", "mobile": "device-mobile", "phone": "phone",
    "design": "palette", "brand": "sparkles", "star": "star", "gift": "gift", "event": "confetti",
    "partner": "heart-handshake", "handshake": "heart-handshake", "contract": "file-text", "document": "file-text",
    "plan": "list-check", "list": "list-check", "roadmap": "route", "path": "route", "scale": "arrows-maximize",
    "efficiency": "gauge", "performance": "gauge", "energy": "bolt", "building": "building", "factory": "building-factory",
    "truck": "truck", "logistics": "truck", "camera": "camera", "video": "video", "music": "music",
}


@dataclass(frozen=True)
class IconPath:
    d: str
    mode: str  # fill | stroke | faint


@dataclass(frozen=True)
class IconDef:
    width: float
    height: float
    stroke_width: float
    paths: tuple[IconPath, ...]


def _library_files(library: str) -> dict[str, Path]:
    folder = ICONS_DIR / library
    return {path.stem: path for path in folder.glob("*.svg")} if folder.is_dir() else {}


@functools.lru_cache(maxsize=8)
def _index(library: str) -> tuple[dict[str, Path], dict[str, tuple[str, ...]]]:
    files = _library_files(library)
    tags: dict[str, tuple[str, ...]] = {}
    if library.startswith("tabler"):
        for name, path in files.items():
            head = path.read_text(encoding="utf-8", errors="ignore")[:600]
            match = _TAGS.search(head)
            if match:
                tags[name] = tuple(tag.strip().strip("'\"").lower() for tag in match.group(1).split(",") if tag.strip())
    return files, tags


def resolve_icon(keyword: str, library: str) -> str:
    """Return ``library/name`` for a keyword.

    A keyword the style's library lacks is looked up in the outline library
    before falling back to a neutral mark, so a meaningful icon always wins
    over a perfectly matching style.
    """
    name = _resolve_in(keyword, library)
    if name and not name.endswith(_FALLBACK_SUFFIXES):
        return name
    if library != "tabler-outline":
        other = _resolve_in(keyword, "tabler-outline")
        if other and not other.endswith(_FALLBACK_SUFFIXES):
            return other
    return name


_FALLBACK_SUFFIXES = ("/circle-dot", "/point", "/circle", "/sparkle", "/star")


def _resolve_in(keyword: str, library: str) -> str:
    files, tags = _index(library)
    key = re.sub(r"[^a-z0-9]+", "-", keyword.strip().lower()).strip("-")
    candidates = [key, SYNONYMS.get(key, ""), SYNONYMS.get(key.split("-")[0], "")]
    for candidate in candidates:
        if candidate and candidate in files:
            return f"{library}/{candidate}"
    for candidate in candidates:
        if not candidate:
            continue
        prefixed = sorted(name for name in files if name.startswith(candidate))
        if prefixed:
            return f"{library}/{prefixed[0]}"
    for name in sorted(tags):
        if key in tags[name]:
            return f"{library}/{name}"
    for name in sorted(files):
        if key and key in name:
            return f"{library}/{name}"
    for fallback in ("circle-dot", "point", "circle", "sparkle", "star"):
        if fallback in files:
            return f"{library}/{fallback}"
    return f"{library}/{sorted(files)[0]}" if files else ""


def _circle(cx: float, cy: float, rx: float, ry: float) -> str:
    return (
        f"M{cx - rx} {cy}A{rx} {ry} 0 1 0 {cx + rx} {cy}"
        f"A{rx} {ry} 0 1 0 {cx - rx} {cy}Z"
    )


def _rect(x: float, y: float, w: float, h: float, rx: float) -> str:
    if rx <= 0:
        return f"M{x} {y}H{x + w}V{y + h}H{x}Z"
    rx = min(rx, w / 2, h / 2)
    return (
        f"M{x + rx} {y}H{x + w - rx}A{rx} {rx} 0 0 1 {x + w} {y + rx}V{y + h - rx}"
        f"A{rx} {rx} 0 0 1 {x + w - rx} {y + h}H{x + rx}A{rx} {rx} 0 0 1 {x} {y + h - rx}"
        f"V{y + rx}A{rx} {rx} 0 0 1 {x + rx} {y}Z"
    )


def _float(value: str | None, default: float = 0.0) -> float:
    try:
        return float(str(value).replace("px", "")) if value is not None else default
    except ValueError:
        return default


@functools.lru_cache(maxsize=512)
def load_icon(name: str) -> IconDef:
    library, _, stem = name.partition("/")
    path = ICONS_DIR / library / f"{stem}.svg"
    root = SafeET.fromstring(path.read_text(encoding="utf-8"))
    view = [float(v) for v in (root.get("viewBox") or "0 0 24 24").replace(",", " ").split()]
    root_stroke = (root.get("stroke") or "") not in ("", "none") and (root.get("fill") or "") == "none"
    stroke_width = _float(root.get("stroke-width"), 2.0)
    paths: list[IconPath] = []
    for element in root.iter():
        tag = element.tag.replace(_SVG_NS, "")
        if element.get("stroke") == "none" and element.get("fill") == "none":
            continue  # tabler's invisible 24x24 bounding path
        if tag == "path":
            d = element.get("d") or ""
        elif tag == "circle":
            r = _float(element.get("r"))
            d = _circle(_float(element.get("cx")), _float(element.get("cy")), r, r)
        elif tag == "ellipse":
            d = _circle(_float(element.get("cx")), _float(element.get("cy")), _float(element.get("rx")), _float(element.get("ry")))
        elif tag == "rect":
            d = _rect(_float(element.get("x")), _float(element.get("y")), _float(element.get("width")), _float(element.get("height")), _float(element.get("rx")))
        elif tag == "line":
            d = f"M{_float(element.get('x1'))} {_float(element.get('y1'))}L{_float(element.get('x2'))} {_float(element.get('y2'))}"
        elif tag in ("polyline", "polygon"):
            points = re.findall(r"[-\d.]+", element.get("points") or "")
            pairs = list(zip(points[::2], points[1::2]))
            if not pairs:
                continue
            d = "M" + " L".join(f"{x} {y}" for x, y in pairs) + ("Z" if tag == "polygon" else "")
        else:
            continue
        if not d.strip():
            continue
        faint = _float(element.get("opacity"), 1.0) < 0.5 or _float(element.get("fill-opacity"), 1.0) < 0.5
        stroked = root_stroke or ((element.get("stroke") or "") not in ("", "none") and element.get("fill") == "none")
        paths.append(IconPath(d=d, mode="faint" if faint else ("stroke" if stroked else "fill")))
    return IconDef(width=view[2], height=view[3], stroke_width=stroke_width, paths=tuple(paths))


def _path_xml(d: str, scale_x: float, scale_y: float, w_emu: int, h_emu: int, *, stroke: bool) -> str:
    from svg_to_pptx.drawingml_paths import normalize_path_commands, parse_svg_path, svg_path_to_absolute

    commands = normalize_path_commands(svg_path_to_absolute(parse_svg_path(d)))
    parts: list[str] = []

    def point(x: float, y: float) -> str:
        return f'<a:pt x="{int(round(x * scale_x * EMU_PER_PX))}" y="{int(round(y * scale_y * EMU_PER_PX))}"/>'

    for command in commands:
        if command.cmd == "M":
            parts.append(f"<a:moveTo>{point(*command.args[:2])}</a:moveTo>")
        elif command.cmd == "L":
            parts.append(f"<a:lnTo>{point(*command.args[:2])}</a:lnTo>")
        elif command.cmd == "C":
            args = command.args
            parts.append(f"<a:cubicBezTo>{point(args[0], args[1])}{point(args[2], args[3])}{point(args[4], args[5])}</a:cubicBezTo>")
        elif command.cmd == "Z":
            parts.append("<a:close/>")
    fill_attr = ' fill="none"' if stroke else ""
    stroke_attr = "" if stroke else ' stroke="0"'
    return f'<a:path w="{w_emu}" h="{h_emu}"{fill_attr}{stroke_attr}>{"".join(parts)}</a:path>'


def icon_shape_xml(shape_id: int, name: str, label: str, box_px: tuple[int, int, int, int], color: str, *, faint: bool = False) -> str | None:
    """One ``p:sp`` drawing the icon (or only its faint duotone layer)."""
    icon = load_icon(label)
    x, y, w, h = box_px
    w_emu, h_emu = w * EMU_PER_PX, h * EMU_PER_PX
    scale_x, scale_y = w / icon.width, h / icon.height
    wanted = [p for p in icon.paths if (p.mode == "faint") == faint]
    if not wanted:
        return None
    stroke = any(p.mode == "stroke" for p in wanted)
    paths = "".join(_path_xml(p.d, scale_x, scale_y, w_emu, h_emu, stroke=p.mode == "stroke") for p in wanted)
    if stroke:
        width_emu = int(icon.stroke_width * scale_x * EMU_PER_PX)
        fill = "<a:noFill/>"
        line = (
            f'<a:ln w="{width_emu}" cap="rnd"><a:solidFill><a:srgbClr val="{color}"/></a:solidFill>'
            "<a:round/></a:ln>"
        )
    else:
        alpha = '<a:alpha val="25000"/>' if faint else ""
        fill = f'<a:solidFill><a:srgbClr val="{color}">{alpha}</a:srgbClr></a:solidFill>'
        line = "<a:ln><a:noFill/></a:ln>"
    return (
        '<p:sp xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
        'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
        f'<p:nvSpPr><p:cNvPr id="{shape_id}" name="{name}"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr>'
        f'<p:spPr><a:xfrm><a:off x="{x * EMU_PER_PX}" y="{y * EMU_PER_PX}"/><a:ext cx="{w_emu}" cy="{h_emu}"/></a:xfrm>'
        '<a:custGeom><a:avLst/><a:gdLst/><a:ahLst/><a:cxnLst/><a:rect l="l" t="t" r="r" b="b"/>'
        f"<a:pathLst>{paths}</a:pathLst></a:custGeom>{fill}{line}</p:spPr>"
        '<p:txBody><a:bodyPr/><a:lstStyle/><a:p><a:endParaRPr lang="en-US"/></a:p></p:txBody>'
        "</p:sp>"
    )
