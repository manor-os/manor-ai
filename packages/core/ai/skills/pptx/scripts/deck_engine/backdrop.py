"""Baked page backdrops: light, texture and atmosphere rendered as one picture.

PowerPoint shapes cannot blur, so glows drawn natively come out hard-edged and
muddy. A style's ``backdrop`` describes its atmosphere instead (a base field,
blurred blooms of colour, soft light bands, halftone dots, stars, a grid, film
grain, a vignette, or the deck's own cover photo out of focus) and the build renders it once per page role into a picture
that sits behind every editable shape. Rendering is deterministic: the same
spec and colours always give the same bytes, cached by content hash.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
from pathlib import Path
from typing import Any

from PIL import Image, ImageChops, ImageDraw, ImageFilter

WIDTH, HEIGHT = 1920, 1080
SMALL = 4  # blooms are drawn and blurred at quarter size, then scaled up


def _rgb(value: str) -> tuple[int, int, int]:
    return tuple(int(value[i : i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def _color(colors: dict[str, str], key: str) -> tuple[int, int, int]:
    return _rgb(colors.get(key, key))


def _luminance(rgb: tuple[int, int, int]) -> float:
    return (0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2]) / 255


def _base(spec: dict[str, Any], colors: dict[str, str]) -> Image.Image:
    keys = spec.get("base") or ["background"]
    first = _color(colors, keys[0])
    if len(keys) == 1:
        return Image.new("RGB", (WIDTH, HEIGHT), first)
    last = _color(colors, keys[-1])
    angle = math.radians(float(spec.get("angle", 90)))
    dx, dy = math.cos(angle), math.sin(angle)
    small = Image.new("RGB", (WIDTH // 8, HEIGHT // 8))
    pixels = small.load()
    w, h = small.size
    span = abs(dx) * w + abs(dy) * h
    for y in range(h):
        for x in range(w):
            t = ((x - (w if dx < 0 else 0)) * dx + (y - (h if dy < 0 else 0)) * dy) / span
            t = min(1.0, max(0.0, t))
            pixels[x, y] = tuple(int(a + (b - a) * t) for a, b in zip(first, last))
    return small.resize((WIDTH, HEIGHT), Image.BICUBIC)


def _blooms(image: Image.Image, blooms: list[list[Any]], colors: dict[str, str], screen: bool) -> Image.Image:
    """Blurred discs of colour: ``[x, y, w, h, colour, strength]`` in fractions of the page."""
    sw, sh = WIDTH // SMALL, HEIGHT // SMALL
    for x, y, w, h, key, strength in blooms:
        mask = Image.new("L", (sw, sh), 0)
        ImageDraw.Draw(mask).ellipse(((x - w / 2) * sw, (y - h / 2) * sh, (x + w / 2) * sw, (y + h / 2) * sh), fill=int(255 * strength))
        mask = mask.filter(ImageFilter.GaussianBlur(max(6, min(w * sw, h * sh) * 0.32))).resize((WIDTH, HEIGHT), Image.BICUBIC)
        solid = Image.new("RGB", (WIDTH, HEIGHT), _color(colors, key))
        if screen:
            image = ImageChops.screen(image, ImageChops.multiply(solid, Image.merge("RGB", (mask, mask, mask))))
        else:
            image = Image.composite(solid, image, mask)
    return image


def _curve(spec: dict[str, Any]) -> list[tuple[float, float]]:
    y, amp, phase = float(spec.get("y", 0.8)), float(spec.get("amp", 0.15)), float(spec.get("phase", 0.6))
    return [(x, (y - amp * math.sin(x / WIDTH * 3.4 + phase)) * HEIGHT) for x in range(-40, WIDTH + 60, 20)]


def _bands(image: Image.Image, bands: list[dict[str, Any]], colors: dict[str, str]) -> Image.Image:
    """Soft curves of light sweeping across the page."""
    for band in bands:
        mask = Image.new("L", (WIDTH, HEIGHT), 0)
        ImageDraw.Draw(mask).line(_curve(band), fill=int(255 * float(band.get("alpha", 0.5))), width=int(band.get("width", 22)))
        mask = mask.filter(ImageFilter.GaussianBlur(float(band.get("blur", 18))))
        image = Image.composite(Image.new("RGB", (WIDTH, HEIGHT), _color(colors, band.get("color", "FFFFFF"))), image, mask)
    return image


def _ribbons(image: Image.Image, ribbons: list[dict[str, Any]], colors: dict[str, str]) -> Image.Image:
    """Wide translucent ribbons of colour (a payments-page wave), slightly softened."""
    for ribbon in ribbons:
        mask = Image.new("L", (WIDTH, HEIGHT), 0)
        draw = ImageDraw.Draw(mask)
        top = _curve(ribbon)
        thick = float(ribbon.get("thickness", 0.08)) * HEIGHT
        polygon = top + [(x, y + thick * (0.6 + 0.4 * math.sin(x / WIDTH * 5))) for x, y in reversed(top)]
        draw.polygon(polygon, fill=int(255 * float(ribbon.get("alpha", 0.8))))
        if ribbon.get("clip_x"):
            draw.rectangle((0, 0, float(ribbon["clip_x"]) * WIDTH, HEIGHT), fill=0)
        mask = mask.filter(ImageFilter.GaussianBlur(float(ribbon.get("blur", 3))))
        image = Image.composite(Image.new("RGB", (WIDTH, HEIGHT), _color(colors, ribbon["color"])), image, mask)
    return image


def _panel(image: Image.Image, spec: dict[str, Any], colors: dict[str, str]) -> Image.Image:
    """A skewed panel filled with its own colour mesh (the hero band of a payments page)."""
    field = _base({"base": spec.get("base", ["FFFFFF"]), "angle": spec.get("angle", 90)}, colors)
    field = _blooms(field, spec.get("blooms", []), colors, screen=False)
    mask = Image.new("L", (WIDTH, HEIGHT), 0)
    ImageDraw.Draw(mask).polygon([(x * WIDTH, y * HEIGHT) for x, y in spec["points"]], fill=255)
    mask = mask.filter(ImageFilter.GaussianBlur(1.5))
    return Image.composite(field, image, mask)


def _dots(image: Image.Image, spec: dict[str, Any], colors: dict[str, str]) -> Image.Image:
    """A halftone patch: print dots, largest at the patch's heart."""
    cx, cy, radius = spec["x"] * WIDTH, spec["y"] * HEIGHT, spec["r"] * WIDTH
    step = int(spec.get("step", 22))
    overlay = Image.new("L", (WIDTH, HEIGHT), 0)
    draw = ImageDraw.Draw(overlay)
    for gy in range(0, HEIGHT, step):
        for gx in range(0, WIDTH, step):
            fall = math.exp(-(((gx - cx) / 1.5) ** 2 + (gy - cy) ** 2) / (2 * radius * radius))
            size = step * 0.42 * fall
            if size >= 1.0:
                draw.ellipse((gx - size, gy - size, gx + size, gy + size), fill=int(255 * float(spec.get("alpha", 0.8))))
    return Image.composite(Image.new("RGB", (WIDTH, HEIGHT), _color(colors, spec.get("color", "FFFFFF"))), image, overlay)


def _stars(image: Image.Image, spec: dict[str, Any], colors: dict[str, str], rng: random.Random) -> Image.Image:
    overlay = Image.new("L", (WIDTH, HEIGHT), 0)
    draw = ImageDraw.Draw(overlay)
    for _ in range(int(spec.get("count", 160))):
        x, y = rng.uniform(0, WIDTH), rng.uniform(0, HEIGHT * float(spec.get("height", 1.0)))
        r = rng.choice((0.8, 1.0, 1.2, 1.6, 2.2))
        draw.ellipse((x - r, y - r, x + r, y + r), fill=rng.randint(90, 255))
    overlay = overlay.filter(ImageFilter.GaussianBlur(0.6))
    return Image.composite(Image.new("RGB", (WIDTH, HEIGHT), _color(colors, spec.get("color", "FFFFFF"))), image, overlay)


def _grid(image: Image.Image, spec: dict[str, Any], colors: dict[str, str]) -> Image.Image:
    overlay = Image.new("L", (WIDTH, HEIGHT), 0)
    draw = ImageDraw.Draw(overlay)
    step = int(spec.get("step", 96))
    value = int(255 * float(spec.get("alpha", 0.08)))
    for x in range(0, WIDTH, step):
        draw.line((x, 0, x, HEIGHT), fill=value)
    for y in range(0, HEIGHT, step):
        draw.line((0, y, WIDTH, y), fill=value)
    if spec.get("fade"):
        fade = Image.linear_gradient("L").resize((WIDTH, HEIGHT))
        overlay = ImageChops.multiply(overlay, fade if spec["fade"] == "down" else ImageChops.invert(fade))
    return Image.composite(Image.new("RGB", (WIDTH, HEIGHT), _color(colors, spec.get("color", "FFFFFF"))), image, overlay)


def _vignette(image: Image.Image, strength: float) -> Image.Image:
    mask = Image.new("L", (WIDTH // 8, HEIGHT // 8), 0)
    ImageDraw.Draw(mask).ellipse((-WIDTH // 32, -HEIGHT // 24, WIDTH // 8 + WIDTH // 32, HEIGHT // 8 + HEIGHT // 24), fill=255)
    mask = mask.filter(ImageFilter.GaussianBlur(28)).resize((WIDTH, HEIGHT), Image.BICUBIC)
    dark = Image.blend(image, Image.new("RGB", (WIDTH, HEIGHT), (0, 0, 0)), strength)
    return Image.composite(image, dark, mask)


def _grain(image: Image.Image, amount: float, rng: random.Random) -> Image.Image:
    """Film grain from a seeded tile, so the same spec always renders the same bytes."""
    size = 256
    tile = Image.new("L", (size, size))
    tile.putdata([int(128 + rng.gauss(0, 42)) for _ in range(size * size)])
    noise = Image.new("L", (WIDTH, HEIGHT))
    for y in range(0, HEIGHT, size):
        for x in range(0, WIDTH, size):
            noise.paste(tile, (x, y))
    textured = ImageChops.overlay(image, Image.merge("RGB", (noise, noise, noise)))
    return Image.blend(image, textured, amount)


def _photo(path: Path, spec: dict[str, Any], colors: dict[str, str]) -> Image.Image:
    """The deck's own photo as the page's light: cover-cropped, thrown out of focus, sunk toward a tint."""
    with Image.open(path) as source:
        image = source.convert("RGB")
    scale = max(WIDTH / image.width, HEIGHT / image.height)
    image = image.resize((max(WIDTH, int(image.width * scale)), max(HEIGHT, int(image.height * scale))), Image.LANCZOS)
    left, top = (image.width - WIDTH) // 2, (image.height - HEIGHT) // 2
    image = image.crop((left, top, left + WIDTH, top + HEIGHT))
    small = image.resize((WIDTH // SMALL, HEIGHT // SMALL), Image.LANCZOS).filter(ImageFilter.GaussianBlur(float(spec.get("blur", 10))))
    image = small.resize((WIDTH, HEIGHT), Image.BICUBIC)
    return Image.blend(image, Image.new("RGB", (WIDTH, HEIGHT), _color(colors, spec.get("tint", "background"))), float(spec.get("dim", 0.7)))


def render(spec: dict[str, Any], colors: dict[str, str], photo: Path | None = None) -> Image.Image:
    rng = random.Random(json.dumps(spec, sort_keys=True))
    image = _photo(photo, spec["photo"], colors) if spec.get("photo") and photo else _base(spec, colors)
    if spec.get("blooms"):
        screen = spec.get("blend", "screen" if _luminance(image.resize((1, 1)).getpixel((0, 0))) < 0.4 else "over") == "screen"
        image = _blooms(image, spec["blooms"], colors, screen)
    if spec.get("grid"):
        image = _grid(image, spec["grid"], colors)
    if spec.get("stars"):
        image = _stars(image, spec["stars"], colors, rng)
    if spec.get("panel"):
        image = _panel(image, spec["panel"], colors)
    if spec.get("ribbons"):
        image = _ribbons(image, spec["ribbons"], colors)
    if spec.get("bands"):
        image = _bands(image, spec["bands"], colors)
    if spec.get("dots"):
        image = _dots(image, spec["dots"], colors)
    if spec.get("vignette"):
        image = _vignette(image, float(spec["vignette"]))
    if spec.get("grain"):
        image = _grain(image, float(spec["grain"]), rng)
    return image


def backdrop(spec: dict[str, Any], colors: dict[str, str], project: Path, photo: Path | None = None) -> Path:
    """The rendered backdrop for ``spec``, cached under the project by content hash.

    A spec with ``photo`` builds on ``photo`` (the deck's cover picture) when the
    deck has one, and on its plain ``base`` otherwise.
    """
    photo = photo if spec.get("photo") else None
    payload = json.dumps({"spec": spec, "colors": colors, "v": 1}, sort_keys=True).encode()
    if photo is not None:
        payload += hashlib.sha256(photo.read_bytes()).digest()
    digest = hashlib.sha256(payload).hexdigest()[:20]
    path = project / ".deck_cache" / f"backdrop_{digest}.jpg"
    if not path.is_file():
        path.parent.mkdir(parents=True, exist_ok=True)
        render(spec, colors, photo).save(path, "JPEG", quality=88, optimize=True)
    return path
