"""Tests for the central external-API-version pin registry.

Two layers of safety:
  1. Smoke that every entry in ``ALL`` parses + has a sane shape.
  2. Verify each MCP wrapper / channel adapter pulls its version
     literal from this central module, not a private constant.
     This is what stops a future contributor from re-pinning inline
     and bypassing the CI freshness check.
"""

from __future__ import annotations

from datetime import date

import pytest

from packages.core.external_api_versions import (
    ALL,
    GITHUB,
    LINKEDIN,
    META_GRAPH,
    APIVersion,
)


# ── 1. Shape / sanity ───────────────────────────────────────────────


def test_all_pins_have_required_fields() -> None:
    """Every pin must be a fully-populated APIVersion."""
    assert ALL, "ALL is empty — registry would be useless"
    for v in ALL:
        assert isinstance(v, APIVersion)
        assert v.name and v.value
        assert v.released <= date.today(), f"{v.name} released in the future"
        assert v.eol_months > 0
        assert v.notes.startswith("http"), f"{v.name} notes should link to vendor changelog"


def test_known_pins_present() -> None:
    """Hard-pin the names so accidental renames break a test."""
    keys = {v.name for v in ALL}
    assert "Meta Graph API" in keys
    assert "LinkedIn API" in keys
    assert "GitHub API" in keys


def test_meta_pin_format() -> None:
    """Meta Graph version is `vNN.0` — anything else is a typo."""
    assert META_GRAPH.value.startswith("v") and META_GRAPH.value.endswith(".0"), (
        f"Meta pin {META_GRAPH.value!r} doesn't look like 'vNN.0'"
    )


def test_linkedin_pin_format() -> None:
    """LinkedIn-Version is YYYYMM (6 digits)."""
    assert LINKEDIN.value.isdigit() and len(LINKEDIN.value) == 6, f"LinkedIn pin {LINKEDIN.value!r} should be YYYYMM"


def test_github_pin_format() -> None:
    """GitHub uses YYYY-MM-DD."""
    parts = GITHUB.value.split("-")
    assert len(parts) == 3 and all(p.isdigit() for p in parts), f"GitHub pin {GITHUB.value!r} should be YYYY-MM-DD"


def test_stale_pct_math() -> None:
    """Round-trip the percentage helper."""
    v = APIVersion(name="x", value="v1.0", released=date(2020, 1, 1), eol_months=12)
    # ~6 months in (180/360 = 0.5)
    midway = date(2020, 7, 1)
    pct = v.stale_pct(midway)
    assert 0.4 < pct < 0.6, f"midway should be near 50%, got {pct:.2f}"


# ── 2. Wrapper bindings — make sure no one re-pinned inline ────────


def test_linkedin_wrapper_uses_central_pin() -> None:
    from packages.core.ai.mcp import linkedin

    assert linkedin._VERSION == LINKEDIN.value


def test_facebook_mcp_wrapper_uses_central_pin() -> None:
    from packages.core.ai.mcp import facebook

    assert facebook._API_VERSION == META_GRAPH.value


def test_whatsapp_adapter_uses_central_pin() -> None:
    from packages.core.services.channels import whatsapp_adapter

    assert whatsapp_adapter.DEFAULT_API_VERSION == META_GRAPH.value


def test_integration_health_uses_central_pin() -> None:
    from packages.core.services import integration_health

    assert META_GRAPH.value in integration_health._META_BASE


def test_github_wrapper_header_uses_central_pin() -> None:
    """github.py builds headers per request, so we instead grep the
    module source for the literal — confirms the import is wired."""
    import inspect
    from packages.core.ai.mcp import github

    src = inspect.getsource(github)
    assert "_GITHUB_PIN.value" in src, "github.py should reference _GITHUB_PIN.value, not a literal"


# ── 3. Coverage guard — every Meta-Graph URL in the repo lives behind the pin


@pytest.mark.parametrize(
    "module_path,attr",
    [
        ("packages.core.ai.mcp.linkedin", "_VERSION"),
        ("packages.core.ai.mcp.facebook", "_API_VERSION"),
        ("packages.core.services.channels.whatsapp_adapter", "DEFAULT_API_VERSION"),
    ],
)
def test_no_regression_to_inline_literal(module_path: str, attr: str) -> None:
    """If someone re-pins an inline constant, this catches it before
    the slow freshness CI step does."""
    import importlib

    mod = importlib.import_module(module_path)
    val = getattr(mod, attr)
    if attr == "_VERSION":
        assert val == LINKEDIN.value
    else:
        assert val == META_GRAPH.value
