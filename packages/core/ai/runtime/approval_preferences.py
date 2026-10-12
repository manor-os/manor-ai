"""Retired personal approval preference entrypoints.

The settings service owns the single policy protocol. Old callers fail visibly
instead of writing configuration ignored by the new evaluator.
"""
from __future__ import annotations


async def runtime_approval_preference_mode(db, **kwargs):
    raise ValueError("Retired approval preferences; use the unified approval policy")


async def set_runtime_approval_preference(db, **kwargs):
    raise ValueError("Retired approval write; use approval settings with expected_version")
