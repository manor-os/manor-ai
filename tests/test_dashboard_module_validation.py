from __future__ import annotations

from packages.core.ai.runtime.dashboard_module_validation import (
    dashboard_module_code_hash,
    validate_dashboard_module_code,
)


def _code() -> dict:
    return {
        "version": 1,
        "runtime": "sandboxed_html",
        "html": '<div class="rows" data-rows></div>',
        "css": ".rows{color:var(--module-text);border:1px solid var(--module-border)}",
        "javascript": (
            "window.renderDashboardModule=function(data){"
            "const root=document.querySelector('[data-rows]');"
            "root.textContent=String(data.items||'');};"
        ),
        "data_requests": [
            {"key": "items", "source": "tasks", "params": {"limit": 5}}
        ],
    }


def test_dashboard_module_validation_accepts_platform_safe_code() -> None:
    code = _code()
    result = validate_dashboard_module_code(code)

    assert result["valid"] is True
    assert result["platform_ready"] is True
    assert result["errors"] == []
    assert result["code_hash"] == dashboard_module_code_hash(code)


def test_dashboard_module_validation_accepts_generated_public_json_request() -> None:
    code = _code()
    code["data_requests"] = [
        {
            "key": "live_data",
            "source": "http_json",
            "params": {},
            "url": "https://api.example.com/v1/status?region=west",
            "refresh_seconds": 300,
        }
    ]

    result = validate_dashboard_module_code(code)

    assert result["platform_ready"] is True
    assert result["contract_version"] == 2


def test_dashboard_module_validation_rejects_unsafe_public_json_request() -> None:
    code = _code()
    code["data_requests"] = [
        {
            "key": "live_data",
            "source": "http_json",
            "params": {},
            "url": "http://127.0.0.1:8000/private",
            "refresh_seconds": 5,
        }
    ]

    result = validate_dashboard_module_code(code)
    issue_codes = {issue["code"] for issue in result["errors"]}

    assert {"data_request_http_url", "data_request_refresh"}.issubset(issue_codes)


def test_dashboard_module_validation_keeps_public_json_domain_logic_out_of_tools() -> None:
    code = _code()
    code["data_requests"] = [
        {
            "key": "live_weather",
            "source": "http_json",
            "params": {},
            "url": "https://api.open-meteo.com/v1/forecast?latitude=47.61&longitude=-122.33&current=temperature_2m",
            "tool_name": "weather_search",
            "tool_arguments": {},
            "refresh_seconds": 300,
        }
    ]

    result = validate_dashboard_module_code(code)

    assert "data_request_http_scope" in {
        issue["code"] for issue in result["errors"]
    }


def test_dashboard_module_validation_rejects_capabilities_and_off_brand_css() -> None:
    code = _code()
    code["css"] += ".hero{background:linear-gradient(red,blue);font-size:2vw}"
    code["javascript"] += "fetch('https://example.com')"

    result = validate_dashboard_module_code(code)
    issue_codes = {issue["code"] for issue in result["errors"]}

    assert result["valid"] is False
    assert {"platform_gradient", "viewport_type", "javascript_capability"}.issubset(issue_codes)


def test_dashboard_module_validation_reports_platform_style_warnings() -> None:
    code = _code()
    code["css"] = (
        ".rows{color:#111;border-radius:16px;width:640px;box-shadow:0 2px 8px #000;"
        "font-family:Arial;font-size:40px}"
    )

    result = validate_dashboard_module_code(code)
    warning_codes = {issue["code"] for issue in result["warnings"]}

    assert result["valid"] is True
    assert result["platform_ready"] is False
    assert {
        "platform_tokens",
        "hardcoded_color",
        "nested_elevation",
        "platform_font",
        "oversized_type",
        "large_radius",
        "fixed_width",
    }.issubset(warning_codes)
