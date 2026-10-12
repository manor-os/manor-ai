"""Bounded, page-relative fields for the customer PDF signing workflow."""

from __future__ import annotations

import io
import math
import zlib
from datetime import datetime
from pathlib import Path
from typing import Literal

from fastapi import HTTPException
from PIL import Image, ImageDraw, ImageFont
from pydantic import BaseModel, ConfigDict, Field, model_validator


class SignatureField(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    id: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_-]+$")
    type: Literal["signature", "name", "date", "text", "checkbox"]
    label: str = Field(min_length=1, max_length=120)
    required: bool = True
    page: int = Field(ge=1, le=10_000, strict=True)
    x: float = Field(ge=0, le=1, allow_inf_nan=False)
    y: float = Field(ge=0, le=1, allow_inf_nan=False)
    width: float = Field(gt=0, le=1, allow_inf_nan=False)
    height: float = Field(gt=0, le=1, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_box(self):
        if self.x + self.width > 1.000001 or self.y + self.height > 1.000001:
            raise ValueError("Fields must stay within the PDF page")
        if not self.label.strip():
            raise ValueError("Fields need a label")
        return self


def validate_signature_fields(fields: list[SignatureField]) -> None:
    if not 1 <= len(fields) <= 50:
        raise HTTPException(400, "Add between 1 and 50 fields")
    if len({field.id for field in fields}) != len(fields):
        raise HTTPException(400, "Field IDs must be unique")
    if not any(field.type == "signature" and field.required for field in fields):
        raise HTTPException(400, "Add at least one required signature field")


# These fonts ship in Manor's API image. macOS paths support local development.
# A matching missing-glyph bitmap is rejected, never rendered as silent tofu.
_TEXT_FONTS = (
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
)


def _font_for_text(value: str, size: int):
    for path in _TEXT_FONTS:
        if not Path(path).is_file():
            continue
        font = ImageFont.truetype(path, size=size)
        missing = bytes(font.getmask("\U0010ffff"))
        if all(char.isspace() or bytes(font.getmask(char)) != missing for char in set(value)):
            return font
    # Pillow's bundled font supports ordinary Latin text even on minimal hosts.
    font = ImageFont.load_default(size=size)
    missing = bytes(font.getmask("\U0010ffff"))
    if all(char.isspace() or bytes(font.getmask(char)) != missing for char in set(value)):
        return font
    raise HTTPException(400, "A field contains characters unsupported by this server's fonts")


def _text_field_image(value: str, width: float, height: float):
    """Render complete Unicode text at 3x resolution, or reject unreadable fit."""
    scale = 3
    image_width, image_height = math.ceil(width * scale), math.ceil(height * scale)
    if image_width > 6000 or image_height > 6000:
        raise HTTPException(400, "PDF field dimensions are too large")
    if width < 8 or height < 8:
        raise HTTPException(400, "A field is too small for readable text; ask the sender to resize it")
    for point_size in (12, 11, 10, 9, 8):
        font = _font_for_text(value, point_size * scale)
        lines = []
        for paragraph in value.split("\n"):
            line = ""
            for char in paragraph:
                candidate = line + char
                if font.getlength(candidate) > image_width - 4:
                    if not line:
                        break
                    lines.append(line)
                    line = char
                else:
                    line = candidate
            else:
                lines.append(line)
                continue
            break
        else:
            rendered = "\n".join(lines)
            probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
            box = probe.multiline_textbbox((0, 0), rendered, font=font, spacing=2)
            if box[2] - box[0] <= image_width - 4 and box[3] - box[1] <= image_height - 2:
                image = Image.new("RGBA", (image_width, image_height))
                ImageDraw.Draw(image).multiline_text(
                    (2 - box[0], (image_height - (box[3] - box[1])) / 2 - box[1]),
                    rendered, font=font, fill=(17, 24, 39, 255), spacing=2,
                )
                return image
    raise HTTPException(400, "Text does not fit its field; shorten it or ask the sender to resize the field")


def _append_image(writer, page, image, name: str, box: tuple[float, float, float, float]):
    from pypdf.generic import DictionaryObject, NameObject, NumberObject, StreamObject

    resource_ref = page.get("/Resources")
    resources = DictionaryObject(dict(resource_ref.get_object() if resource_ref is not None else {}))
    xobject_ref = resources.get("/XObject")
    xobjects = DictionaryObject(dict(xobject_ref.get_object() if xobject_ref is not None else {}))
    # Original page resources can use any name; never overwrite one.
    while NameObject(name) in xobjects:
        name += "_"
    image_name = NameObject(name)
    streams = []
    for pixels, colorspace in ((image.getchannel("A"), "/DeviceGray"), (image.convert("RGB"), "/DeviceRGB")):
        stream = StreamObject()
        stream._data = zlib.compress(pixels.tobytes())
        stream.update({
            NameObject("/Type"): NameObject("/XObject"),
            NameObject("/Subtype"): NameObject("/Image"),
            NameObject("/Width"): NumberObject(image.width),
            NameObject("/Height"): NumberObject(image.height),
            NameObject("/ColorSpace"): NameObject(colorspace),
            NameObject("/BitsPerComponent"): NumberObject(8),
            NameObject("/Filter"): NameObject("/FlateDecode"),
        })
        if streams:
            stream[NameObject("/SMask")] = streams[0]
        streams.append(writer._add_object(stream))
    xobjects[image_name] = streams[-1]
    resources[NameObject("/XObject")] = xobjects
    page[NameObject("/Resources")] = resources
    x, y, width, height = box
    return f"q {width:.4f} 0 0 {height:.4f} {x:.4f} {y:.4f} cm {name} Do Q\n"


def stamp_pdf_fields(reader, fields, values, images, *, signer_email: str, signed_at: datetime, request_id: str, signature_status: str = "completed") -> bytes:
    """Stamp all fields in visible CropBox coordinates into one PDF revision."""
    from pypdf import PdfWriter
    from pypdf.generic import ArrayObject, NameObject, StreamObject

    writer = PdfWriter()
    writer.clone_document_from_reader(reader)
    commands_by_page: dict[int, list[str]] = {}
    for index, field in enumerate(fields):
        value = values.get(field.id)
        if value in (None, "", False):
            continue
        page = writer.pages[field.page - 1]
        if int(page.get("/Rotate", 0) or 0) % 360:
            page.transfer_rotation_to_content()
        left = max(float(page.mediabox.left), float(page.cropbox.left))
        bottom = max(float(page.mediabox.bottom), float(page.cropbox.bottom))
        width = min(float(page.mediabox.right), float(page.cropbox.right)) - left
        height = min(float(page.mediabox.top), float(page.cropbox.top)) - bottom
        if not all(math.isfinite(number) and number > 0 for number in (width, height)):
            raise HTTPException(400, "PDF page dimensions are invalid")
        bw, bh = width * field.width, height * field.height
        x, y = left + width * field.x, bottom + height * (1 - field.y - field.height)
        if field.type == "signature":
            image = images[field.id]
            ratio = min(bw / image.width, bh / image.height)
            draw_width, draw_height = image.width * ratio, image.height * ratio
            x, y = x + (bw - draw_width) / 2, y + (bh - draw_height) / 2
        elif field.type == "checkbox":
            image = Image.new("RGBA", (96, 96))
            ImageDraw.Draw(image).line([(16, 48), (39, 73), (83, 20)], fill=(17, 24, 39, 255), width=10)
            draw_width = draw_height = min(bw, bh)
            x, y = x + (bw - draw_width) / 2, y + (bh - draw_height) / 2
        else:
            image = _text_field_image(value, bw, bh)
            draw_width, draw_height = bw, bh
        commands_by_page.setdefault(field.page, []).append(_append_image(
            writer, page, image, f"/ManorField{index}", (x, y, draw_width, draw_height),
        ))

    for number, commands in commands_by_page.items():
        page = writer.pages[number - 1]
        overlay = StreamObject()
        overlay._data = "".join(commands).encode("ascii")
        original = page.get("/Contents")
        if original is None:
            page[NameObject("/Contents")] = writer._add_object(overlay)
        else:
            resolved = original.get_object()
            originals = list(resolved) if isinstance(resolved, ArrayObject) else [original]
            save, restore = StreamObject(), StreamObject()
            save._data, restore._data = b"q\n", b"Q\n"
            page[NameObject("/Contents")] = ArrayObject([
                writer._add_object(save), *originals, writer._add_object(restore), writer._add_object(overlay),
            ])
    metadata = {str(k): str(v) for k, v in (reader.metadata or {}).items() if v is not None}
    metadata.update({
        "/ManorSignatureStatus": signature_status,
        "/ManorSignatureRequestId": request_id,
        "/ManorSignedAt": signed_at.isoformat(),
        "/ManorSigner": signer_email,
    })
    writer.add_metadata(metadata)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()
