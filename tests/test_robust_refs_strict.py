"""``resolve_refs(strict=True)``: publish consumers never get a degraded value."""
import pytest

from packages.core.plans.refs import ReferenceError, resolve_refs


PRIOR = {"draft": {"text": "hello", "meta": {"n": 1}}, "count": {"value": 3}}


def test_lenient_mode_still_degrades_missing_field_to_text_sibling():
    out = resolve_refs({"body": "${{ steps.draft.result.tweet_text }}"}, PRIOR)
    assert out == {"body": "hello"}


def test_strict_mode_raises_on_missing_field():
    with pytest.raises(ReferenceError, match="tweet_text"):
        resolve_refs({"body": "${{ steps.draft.result.tweet_text }}"}, PRIOR, strict=True)


def test_strict_mode_raises_on_scalar_descent():
    with pytest.raises(ReferenceError, match="cannot descend"):
        resolve_refs("${{ steps.count.result.value.deeper }}", PRIOR, strict=True)


def test_strict_mode_resolves_present_paths_unchanged():
    out = resolve_refs(
        {"n": "${{ steps.draft.result.meta.n }}", "t": "x ${{ steps.draft.result.text }}"},
        PRIOR,
        strict=True,
    )
    assert out == {"n": 1, "t": "x hello"}


def test_strict_flag_propagates_through_lists_and_nested_dicts():
    with pytest.raises(ReferenceError):
        resolve_refs({"items": [{"v": "${{ steps.draft.result.nope }}"}]}, PRIOR, strict=True)
