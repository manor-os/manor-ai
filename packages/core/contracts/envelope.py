"""Typed step-result envelope: every action returns Success or Failure."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional, Union


@dataclass(frozen=True)
class Success:
    data: Any
    ok: bool = True

    def to_dict(self) -> dict:
        return {"ok": True, "data": self.data}


@dataclass(frozen=True)
class Failure:
    reason: str
    detail: Optional[dict] = None
    ok: bool = False

    def to_dict(self) -> dict:
        out: dict = {"ok": False, "reason": self.reason}
        if self.detail is not None:
            out["detail"] = self.detail
        return out


StepResult = Union[Success, Failure]
