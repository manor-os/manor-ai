"""Generic provider-error legibility (video + LLM providers).

`provider_error_hint` maps recurring failure modes to an actionable hint so the
real cause is not buried under a misleading status string — most importantly
the Volcengine/Seedance case where an out-of-balance account returns
`401 The api key is invalid`. It must NOT duplicate a cause the provider already
named (balance / quota / rate-limit).
"""
from packages.core.tasks.video_adapters import provider_error_hint


def test_401_invalid_key_points_at_account_balance():
    hint = provider_error_hint(401, "The api key is invalid.")
    assert hint and "balance" in hint.lower() and "billing" in hint.lower()


def test_403_treated_like_auth():
    assert provider_error_hint(403, "forbidden")


def test_429_points_at_rate_quota():
    hint = provider_error_hint(429, "")
    assert hint and ("quota" in hint.lower() or "rate" in hint.lower())


def test_5xx_points_at_outage():
    for status in (500, 502, 503, 524):
        hint = provider_error_hint(status, "")
        assert hint and ("outage" in hint.lower() or "timeout" in hint.lower())


def test_html_body_points_at_outage_even_on_odd_status():
    hint = provider_error_hint(200, "<!DOCTYPE html><html>... 502 bad gateway ...</html>")
    assert hint and ("outage" in hint.lower() or "timeout" in hint.lower())


def test_no_hint_for_plain_4xx():
    assert provider_error_hint(400, "bad request") == ""
    assert provider_error_hint(404, "not found") == ""


def test_no_duplicate_when_provider_named_the_cause():
    assert provider_error_hint(401, "your account has an overdue balance") == ""
    assert provider_error_hint(401, "余额不足") == ""
    assert provider_error_hint(429, "quota exceeded for this account") == ""
