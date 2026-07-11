"""Tests for multi-language i18n support."""

from __future__ import annotations

import pytest
from httpx import AsyncClient


# ── Unit tests for packages.core.i18n ──


def test_translate_en():
    """Default locale is English; basic key lookup works."""
    from packages.core.i18n import set_locale, t

    set_locale("en")
    assert t("error.not_found") == "Not found"
    assert t("task.created") == "Task created successfully"
    assert t("auth.login_success") == "Login successful"


def test_translate_zh():
    """Setting locale to zh returns Chinese translations."""
    from packages.core.i18n import set_locale, t

    set_locale("zh")
    assert t("error.not_found") == "未找到"
    assert t("task.created") == "任务创建成功"
    assert t("auth.login_success") == "登录成功"
    # Reset to avoid leaking into other tests
    set_locale("en")


def test_translate_fallback():
    """Unknown key returns the key itself; missing locale key falls back to English."""
    from packages.core.i18n import set_locale, t

    set_locale("en")
    assert t("this.key.does.not.exist") == "this.key.does.not.exist"

    # Japanese has fewer keys — missing ones should fall back to English
    set_locale("ja")
    assert t("share.created") == "Share link created"  # not in ja, falls back to en
    set_locale("en")


@pytest.mark.asyncio
async def test_locale_detection(client: AsyncClient):
    """Accept-Language header sets locale; /api/v1/auth/locales reflects it."""
    # Request with zh Accept-Language
    resp = await client.get(
        "/api/v1/auth/locales",
        headers={"Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "zh" in data["supported"]
    assert data["current"] == "zh"

    # Request with X-Language header (takes priority over Accept-Language)
    resp = await client.get(
        "/api/v1/auth/locales",
        headers={
            "Accept-Language": "en",
            "X-Language": "es",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["current"] == "es"

    # Query param takes highest priority
    resp = await client.get(
        "/api/v1/auth/locales?lang=ja",
        headers={"X-Language": "zh"},
    )
    assert resp.status_code == 200
    assert resp.json()["current"] == "ja"
