"""Deterministic visual templates for Marketplace Blueprint covers.

The service intentionally returns a small semantic template specification
instead of generating or storing raster media.  The web client renders the
specification as theme-aware SVG, which keeps empty Marketplace listings fast,
offline-capable, and visually consistent without a model call at view time.
"""
from __future__ import annotations

from hashlib import sha256
from typing import Literal, TypedDict


BlueprintCoverMotif = Literal[
    "analytics",
    "commerce",
    "content",
    "distribution",
    "service",
    "video",
    "workspace",
]
BlueprintCoverPalette = Literal["blue", "peach", "sage", "stone"]


class BlueprintCoverTemplate(TypedDict):
    motif: BlueprintCoverMotif
    palette: BlueprintCoverPalette
    variant: int
    seed: int


_MOTIF_SIGNALS: tuple[tuple[BlueprintCoverMotif, tuple[str, ...]], ...] = (
    (
        "distribution",
        (
            "distribution",
            "dispatch",
            "publish",
            "social media",
            "linkedin",
            "newsletter",
            "分发",
            "分发内容",
            "发布",
            "社媒",
        ),
    ),
    (
        "video",
        (
            "video",
            "camera",
            "film",
            "recording",
            "shorts",
            "youtube",
            "视频",
            "拍摄",
            "剪辑",
            "相机",
        ),
    ),
    (
        "commerce",
        (
            "store",
            "shop",
            "commerce",
            "ecommerce",
            "product sales",
            "inventory",
            "checkout",
            "商店",
            "电商",
            "商品",
            "销售",
            "库存",
        ),
    ),
    (
        "service",
        (
            "service",
            "client",
            "revenue",
            "offer",
            "delivery",
            "consulting",
            "agency",
            "服务",
            "客户",
            "收入",
            "交付",
            "咨询",
        ),
    ),
    (
        "analytics",
        (
            "analytics",
            "analysis",
            "quality assurance",
            "qa runtime",
            "testing",
            "metrics",
            "report",
            "分析",
            "测试",
            "指标",
            "报告",
            "质检",
        ),
    ),
    (
        "content",
        (
            "content",
            "editorial",
            "writing",
            "script",
            "creative",
            "studio",
            "内容",
            "写作",
            "脚本",
            "创作",
        ),
    ),
)

_PALETTES: tuple[BlueprintCoverPalette, ...] = (
    "sage",
    "peach",
    "blue",
    "stone",
)


def _signal_score(text: str, signals: tuple[str, ...]) -> int:
    return sum(text.count(signal) for signal in signals)


def build_blueprint_cover_template(
    *,
    title: str,
    description: str | None,
    tags: list[str] | tuple[str, ...] | None,
    identity: str,
) -> BlueprintCoverTemplate:
    """Resolve Blueprint copy into a stable, renderer-agnostic cover spec.

    Description is deliberately weighted twice so a generic product title does
    not overpower the Blueprint's actual operating purpose.  The identity only
    controls visual variation; changing copy can change the semantic motif.
    """

    normalized_title = title.casefold()
    normalized_description = (description or "").casefold()
    normalized_tags = " ".join(tags or ()).casefold()
    semantic_text = (
        f"{normalized_title} "
        f"{normalized_description} {normalized_description} "
        f"{normalized_tags}"
    )

    motif: BlueprintCoverMotif = "workspace"
    best_score = 0
    for candidate, signals in _MOTIF_SIGNALS:
        score = _signal_score(semantic_text, signals)
        if score > best_score:
            motif = candidate
            best_score = score

    digest = sha256(
        f"blueprint-cover-v1\0{identity}\0{semantic_text}".encode("utf-8")
    ).digest()
    seed = int.from_bytes(digest[:4], "big")
    return {
        "motif": motif,
        "palette": _PALETTES[digest[4] % len(_PALETTES)],
        "variant": digest[5] % 3,
        "seed": seed,
    }
