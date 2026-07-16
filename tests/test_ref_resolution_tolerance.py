"""Runtime tolerance for refs that read a field a producer never emitted.

Reading ``${{ steps.X.result.text }}`` where X (a free-form subagent / search
step) returned an object without ``.text`` used to raise ReferenceError and kill
the whole task — the recurring prod failure ("key 'text' missing"). It now
degrades: a text-ish sibling field if present, else the object as JSON, so the
consumer still receives usable content instead of crashing.
"""
import pytest

from packages.core.plans.refs import ReferenceError, resolve_refs


def test_missing_field_falls_back_to_text_sibling():
    prior = {"search": {"content": "the findings"}}  # no .text, but has .content
    assert resolve_refs("${{ steps.search.result.text }}", prior) == "the findings"


def test_missing_field_without_alias_stringifies_object():
    prior = {"search": {"foo": 1, "bar": [2, 3]}}  # no text-ish key at all
    out = resolve_refs("${{ steps.search.result.text }}", prior)
    assert isinstance(out, str) and "foo" in out and "bar" in out


def test_scalar_producer_degrades_to_string():
    prior = {"search": "just a string result"}
    assert resolve_refs("${{ steps.search.result.text }}", prior) == "just a string result"


def test_present_field_still_resolves_normally():
    prior = {"search": {"text": "hello"}}
    assert resolve_refs("${{ steps.search.result.text }}", prior) == "hello"


def test_embedded_ref_missing_field_degrades_inline():
    prior = {"s": {"content": "X"}}
    assert resolve_refs("Use: ${{ steps.s.result.text }}", prior) == "Use: X"


def test_unknown_step_still_raises():
    # A ref to a step that doesn't exist at all is still a hard error — a real
    # wiring bug, not a producer/consumer shape mismatch.
    with pytest.raises(ReferenceError):
        resolve_refs("${{ steps.nope.result.text }}", {})
