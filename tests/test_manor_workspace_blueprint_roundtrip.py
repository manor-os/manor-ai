from __future__ import annotations

import importlib.util
from pathlib import Path


_PATH = Path(__file__).parents[1] / ".agents/skills/manor-workspace-blueprint/scripts/compare_roundtrip.py"
_SPEC = importlib.util.spec_from_file_location("blueprint_roundtrip", _PATH)
assert _SPEC and _SPEC.loader
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)


def test_roundtrip_ignores_generated_ids_and_install_metadata() -> None:
    source = {
        "recipe": {
            "workflows": [{"slug": "daily", "steps": [{"id": "start", "next": []}]}],
            "scheduled_jobs": [{"job_id": "daily", "execution_target": {"service_key": "x"}}],
        }
    }
    installed = {
        "recipe": {
            "workflows": [{"id": "01INSTALLED", "slug": "daily", "steps": [{"id": "start", "next": []}]}],
            "scheduled_jobs": [{"job_id": "daily-1ABC23XY", "execution_target": {"service_key": "x", "binding_id": "01BINDING"}}],
        },
        "settings": {"_blueprint": {"install_mode": "live"}},
    }
    assert _MODULE.compare(source, installed) == []


def test_roundtrip_reports_workflow_graph_change() -> None:
    source = {"recipe": {"workflows": [{"slug": "daily", "steps": [{"id": "a", "next": ["b"]}, {"id": "b", "next": []}]}]}}
    installed = {"recipe": {"workflows": [{"slug": "daily", "steps": [{"id": "a", "next": []}, {"id": "b", "next": []}]}]}}
    assert _MODULE.compare(source, installed)


def test_roundtrip_reports_portable_step_id_change() -> None:
    source = {"recipe": {"workflows": [{"slug": "daily", "steps": [{"id": "produce", "next": []}]}]}}
    installed = {"recipe": {"workflows": [{"slug": "daily", "steps": [{"id": "publish", "next": []}]}]}}
    assert _MODULE.compare(source, installed)


def test_roundtrip_preserves_hyphens_in_portable_job_id() -> None:
    source = {"recipe": {"scheduled_jobs": [{"job_id": "morning-draft"}]}}
    installed = {
        "recipe": {"scheduled_jobs": [{"job_id": "morning-draft-1ABC23XY"}]}
    }
    changed = {"recipe": {"scheduled_jobs": [{"job_id": "morning-post-1ABC23XY"}]}}

    assert _MODULE.compare(source, installed) == []
    assert _MODULE.compare(source, changed)


def test_roundtrip_excludes_one_shot_install_declarations() -> None:
    source = {
        "manifest": {"title": "Source", "kind": "studio"},
        "contract": {
            "variables": [{"key": "brand", "required": True}],
            "channels": [{"channel_type": "telegram"}],
        },
        "recipe": {"workflows": []},
        "policy": {
            "governance": {"max_risk_level": "medium"},
            "post_install_checks": [{"kind": "check_agent_callable"}],
            "expected_baseline": {"runnable_in_simulation": True},
        },
    }
    installed = {
        "manifest": {"title": "Remix", "kind": "studio"},
        "contract": {"variables": [], "channels": []},
        "recipe": {"workflows": []},
        "policy": {"governance": {"max_risk_level": "medium"}},
    }
    assert _MODULE.compare(source, installed) == []
