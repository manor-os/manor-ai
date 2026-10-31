"""The model-facing contract for image pixels returned by file reads.

A tool that wants the model to see an image returns
``{"image": {"data_url": ..., "delivery": FILE_IMAGE_DELIVERY_CONTRACT}}``.
The agentic loop is the single place that turns such a payload into a visual
observation, whatever tool, wrapper or addressing form produced it. When the
pixels are not sent, the result carries ``file_image_not_delivered(reason)``
instead, so the model is never left to infer from bare metadata.
"""
from __future__ import annotations

from typing import Any

FILE_IMAGE_DELIVERY_CONTRACT = "ephemeral_multimodal"

FILE_IMAGE_NOT_DELIVERED_NOTE = (
    "No image pixels were sent to you for this read. Do not describe or judge its "
    "visual content from this metadata."
)
FILE_IMAGE_DELIVERED_NOTE = (
    "The image pixels are attached as an image in the user message that follows "
    "these tool results, in your next model request only; later requests keep "
    "only this metadata. Record what you see there before moving on, and read "
    "the file again to look at it later. If no image follows, it was not "
    "delivered: read the file again rather than assessing it from metadata."
)


def file_image_not_delivered(reason: str, **details: Any) -> dict[str, Any]:
    """The one model-facing status for an image read whose pixels were not sent."""

    return {
        "delivered": False,
        "reason": reason,
        **details,
        "note": FILE_IMAGE_NOT_DELIVERED_NOTE,
    }


def file_image_delivered(*, mime_type: str, size: int) -> dict[str, Any]:
    """The transcript status for pixels attached to the next model request."""

    return {
        "delivered": True,
        "mime_type": mime_type,
        "bytes": size,
        "delivery": "attached_to_next_model_request",
        "note": FILE_IMAGE_DELIVERED_NOTE,
    }
