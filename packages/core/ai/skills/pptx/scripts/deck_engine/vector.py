"""Native vector decoration: organic blobs, hand-drawn strokes, custom geometry.

Paths are written in the shape's own box coordinates (0..w, 0..h px) so the
shape frame equals the planned box exactly and template verification stays
exact. Everything is deterministic for a given seed.
"""

from __future__ import annotations

import math
import random

EMU_PER_PX = 9525


def blob_path(w: int, h: int, seed: int, *, points: int = 9, wobble: float = 0.16) -> str:
    """A smooth closed organic shape filling (most of) a w x h box."""
    rng = random.Random(seed)
    cx, cy = w / 2, h / 2
    pts = []
    for index in range(points):
        angle = 2 * math.pi * index / points + rng.uniform(-0.12, 0.12)
        scale = 1 - rng.uniform(0, wobble)
        pts.append((cx + math.cos(angle) * cx * scale, cy + math.sin(angle) * cy * scale))
    return _closed_spline(pts)


def brush_path(w: int, h: int, seed: int) -> str:
    """A wide, slightly ragged painted block (like a dry-brush swatch)."""
    rng = random.Random(seed)
    top = [(x, rng.uniform(0, h * 0.12)) for x in _steps(w, 7)]
    bottom = [(x, h - rng.uniform(0, h * 0.12)) for x in reversed(_steps(w, 7))]
    left = (rng.uniform(0, w * 0.04), h / 2)
    right = (w - rng.uniform(0, w * 0.04), h / 2)
    return _closed_spline([left, *top, right, *bottom])


def underline_path(w: int, h: int, seed: int) -> str:
    """An open, gently wavy hand-drawn stroke across the box."""
    rng = random.Random(seed)
    y = h / 2
    pts = [(x, y + rng.uniform(-h * 0.35, h * 0.35)) for x in _steps(w, 5)]
    d = f"M{pts[0][0]:.1f} {pts[0][1]:.1f}"
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        mx = (x0 + x1) / 2
        d += f"C{mx:.1f} {y0:.1f} {mx:.1f} {y1:.1f} {x1:.1f} {y1:.1f}"
    return d


def arc_path(w: int, h: int, start_deg: float, sweep_deg: float) -> str:
    """An open circular arc inscribed in the box, clockwise from ``start_deg`` (0 = 3 o'clock)."""
    cx, cy, r = w / 2, h / 2, min(w, h) / 2
    segments = max(1, math.ceil(abs(sweep_deg) / 90))
    step = math.radians(sweep_deg / segments)
    k = 4 / 3 * math.tan(step / 4)
    angle = math.radians(start_deg)
    x0, y0 = cx + r * math.cos(angle), cy + r * math.sin(angle)
    d = f"M{x0:.1f} {y0:.1f}"
    for _ in range(segments):
        a1 = angle + step
        x1, y1 = cx + r * math.cos(a1), cy + r * math.sin(a1)
        c1 = (x0 - k * r * math.sin(angle), y0 + k * r * math.cos(angle))
        c2 = (x1 + k * r * math.sin(a1), y1 - k * r * math.cos(a1))
        d += f"C{c1[0]:.1f} {c1[1]:.1f} {c2[0]:.1f} {c2[1]:.1f} {x1:.1f} {y1:.1f}"
        angle, x0, y0 = a1, x1, y1
    return d


def horizon_path(w: int, h: int, *, rim: bool = False) -> str:
    """The top of a planet rising from the bottom edge: a filled dome, or just its rim line."""
    d = f"M0 {h:.1f}C{w * 0.22:.1f} {h * 0.05:.1f} {w * 0.78:.1f} {h * 0.05:.1f} {w:.1f} {h:.1f}"
    return d if rim else d + "Z"


def _steps(length: float, count: int) -> list[float]:
    return [length * index / (count - 1) for index in range(count)]


def _closed_spline(pts: list[tuple[float, float]]) -> str:
    """Catmull-Rom through the points, as cubic Beziers."""
    n = len(pts)
    d = f"M{pts[0][0]:.1f} {pts[0][1]:.1f}"
    for i in range(n):
        p0, p1, p2, p3 = pts[(i - 1) % n], pts[i], pts[(i + 1) % n], pts[(i + 2) % n]
        c1 = (p1[0] + (p2[0] - p0[0]) / 6, p1[1] + (p2[1] - p0[1]) / 6)
        c2 = (p2[0] - (p3[0] - p1[0]) / 6, p2[1] - (p3[1] - p1[1]) / 6)
        d += f"C{c1[0]:.1f} {c1[1]:.1f} {c2[0]:.1f} {c2[1]:.1f} {p2[0]:.1f} {p2[1]:.1f}"
    return d + "Z"


def _path_points(d: str, w_emu: int, h_emu: int) -> str:
    from svg_to_pptx.drawingml_paths import normalize_path_commands, parse_svg_path, svg_path_to_absolute

    commands = normalize_path_commands(svg_path_to_absolute(parse_svg_path(d)))

    def pt(x: float, y: float) -> str:
        return f'<a:pt x="{int(round(x * EMU_PER_PX))}" y="{int(round(y * EMU_PER_PX))}"/>'

    parts = []
    for command in commands:
        if command.cmd == "M":
            parts.append(f"<a:moveTo>{pt(*command.args[:2])}</a:moveTo>")
        elif command.cmd == "L":
            parts.append(f"<a:lnTo>{pt(*command.args[:2])}</a:lnTo>")
        elif command.cmd == "C":
            a = command.args
            parts.append(f"<a:cubicBezTo>{pt(a[0], a[1])}{pt(a[2], a[3])}{pt(a[4], a[5])}</a:cubicBezTo>")
        elif command.cmd == "Z":
            parts.append("<a:close/>")
    return "".join(parts)


def custom_shape_xml(
    shape_id: int,
    name: str,
    box: tuple[int, int, int, int],
    d: str,
    *,
    fill: str | None,
    alpha: int = 100,
    line: str | None = None,
    line_px: float = 0.0,
    soft: int = 0,
) -> str:
    x, y, w, h = box
    w_emu, h_emu = w * EMU_PER_PX, h * EMU_PER_PX
    open_path = not d.rstrip().endswith("Z")
    path_attr = ' fill="none"' if open_path or not fill else ""
    alpha_xml = f'<a:alpha val="{alpha * 1000}"/>' if alpha < 100 else ""
    fill_xml = f'<a:solidFill><a:srgbClr val="{fill}">{alpha_xml}</a:srgbClr></a:solidFill>' if fill and not open_path else "<a:noFill/>"
    if line and line_px:
        line_xml = (
            f'<a:ln w="{int(line_px * EMU_PER_PX)}" cap="rnd"><a:solidFill><a:srgbClr val="{line}"/></a:solidFill>'
            "<a:round/></a:ln>"
        )
    else:
        line_xml = "<a:ln><a:noFill/></a:ln>"
    return (
        '<p:sp xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
        'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
        f'<p:nvSpPr><p:cNvPr id="{shape_id}" name="{name}"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr>'
        f'<p:spPr><a:xfrm><a:off x="{x * EMU_PER_PX}" y="{y * EMU_PER_PX}"/><a:ext cx="{w_emu}" cy="{h_emu}"/></a:xfrm>'
        '<a:custGeom><a:avLst/><a:gdLst/><a:ahLst/><a:cxnLst/><a:rect l="l" t="t" r="r" b="b"/>'
        f'<a:pathLst><a:path w="{w_emu}" h="{h_emu}"{path_attr}>{_path_points(d, w_emu, h_emu)}</a:path></a:pathLst>'
        f"</a:custGeom>{fill_xml}{line_xml}{soft_edge_xml(soft)}</p:spPr>"
        '<p:txBody><a:bodyPr/><a:lstStyle/><a:p><a:endParaRPr lang="en-US"/></a:p></p:txBody>'
        "</p:sp>"
    )


def soft_edge_xml(soft_px: int) -> str:
    """Feathered edges: the shape fades out over ``soft_px``, turning a disc into a glow."""
    if not soft_px:
        return ""
    return f'<a:effectLst><a:softEdge rad="{int(soft_px * EMU_PER_PX)}"/></a:effectLst>'


SHADOW_XML = (
    '<a:effectLst xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
    '<a:outerShdw blurRad="190500" dist="38100" dir="5400000" algn="t" rotWithShape="0">'
    '<a:srgbClr val="000000"><a:alpha val="16000"/></a:srgbClr></a:outerShdw></a:effectLst>'
)
