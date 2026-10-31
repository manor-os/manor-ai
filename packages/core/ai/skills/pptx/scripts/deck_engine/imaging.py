"""Image preparation for template slots.

``cutout`` removes a plain studio background (white or any flat colour that
touches the image border) so a portrait or product can stand over the slide's
own shapes. The result is cached next to the project by content hash, which
keeps re-planning deterministic and cheap.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter

SENTINEL = (255, 0, 255)
WORK_WIDTH = 520


def cutout(source: Path, project: Path, *, tolerance: int = 38) -> Path:
    data = source.read_bytes()
    digest = hashlib.sha256(data + str(tolerance).encode()).hexdigest()[:20]
    cache = project / ".deck_cache" / f"cutout_{digest}.png"
    if cache.is_file():
        return cache
    cache.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(source) as original:
        image = original.convert("RGBA")
    if image.getchannel("A").getextrema()[0] < 250:
        # Already transparent: just trim it.
        image.crop(image.getbbox() or (0, 0, *image.size)).save(cache)
        return cache
    scale = WORK_WIDTH / image.width
    small = image.convert("RGB").resize((WORK_WIDTH, max(1, int(image.height * scale))), Image.LANCZOS)
    w, h = small.size
    seeds = [(x, y) for x in range(0, w, max(1, w // 12)) for y in (0, h - 1)]
    seeds += [(x, y) for y in range(0, h, max(1, h // 12)) for x in (0, w - 1)]
    for seed in seeds:
        if small.getpixel(seed) != SENTINEL:
            ImageDraw.floodfill(small, seed, SENTINEL, thresh=tolerance)
    mask = Image.new("L", small.size, 255)
    pixels, mask_pixels = small.load(), mask.load()
    for y in range(h):
        for x in range(w):
            if pixels[x, y] == SENTINEL:
                mask_pixels[x, y] = 0
    mask = mask.filter(ImageFilter.MinFilter(3)).filter(ImageFilter.GaussianBlur(1.2))
    alpha = mask.resize(image.size, Image.LANCZOS)
    image.putalpha(ImageChops.multiply(image.getchannel("A"), alpha))
    box = alpha.point(lambda v: 255 if v > 24 else 0).getbbox() or (0, 0, *image.size)
    image.crop(box).save(cache)
    return cache


TREATMENTS = ("soft", "halftone")


def treat(source: Path, project: Path, treatment: str) -> Path:
    """Style treatment for full-bleed photos.

    ``soft`` throws the photo out of focus so it reads as light and colour behind
    type. ``halftone`` adds a dot screen over drifting clouds of the soft photo:
    white dots sized by the photo's own brightness, the texture of a print.
    """
    data = source.read_bytes()
    digest = hashlib.sha256(data + treatment.encode()).hexdigest()[:20]
    cache = project / ".deck_cache" / f"{treatment}_{digest}.jpg"
    if cache.is_file():
        return cache
    cache.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(source) as original:
        image = original.convert("RGB")
    if image.width > 2400:
        image = image.resize((2400, int(image.height * 2400 / image.width)), Image.LANCZOS)
    soft = image.filter(ImageFilter.GaussianBlur(max(6, image.width // 110)))
    if treatment == "halftone":
        soft = _halftone(soft, digest)
    soft.save(cache, "JPEG", quality=90)
    return cache


def _halftone(image: Image.Image, seed: str) -> Image.Image:
    import math
    import random

    rng = random.Random(seed)
    w, h = image.size
    step = max(10, w // 120)
    gray = image.convert("L").resize((w // step + 1, h // step + 1), Image.BILINEAR)
    # Three drifting clouds decide where the screen shows; elsewhere the photo is clean.
    clouds = [(rng.uniform(0.1, 0.9) * w, rng.uniform(0.1, 0.9) * h, rng.uniform(0.18, 0.32) * w) for _ in range(3)]
    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    for gy in range(gray.height):
        for gx in range(gray.width):
            x, y = gx * step + step / 2, gy * step + step / 2
            weight = max(math.exp(-((x - cx) ** 2 + (y - cy) ** 2) / (2 * r * r)) for cx, cy, r in clouds)
            if weight < 0.18:
                continue
            light = gray.getpixel((gx, gy)) / 255
            radius = step * 0.42 * weight * (0.35 + 0.65 * light)
            if radius < 0.9:
                continue
            draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=(255, 255, 255, int(150 + 90 * weight)))
    return Image.alpha_composite(image.convert("RGBA"), overlay).convert("RGB")
