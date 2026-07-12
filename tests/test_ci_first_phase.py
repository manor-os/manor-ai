"""Contract tests for the first-phase CI gate.

These tests keep the PR gate intentionally small and stable: source checks,
unit tests, type/build checks, and lightweight source smoke tests. Full Docker
stack E2E remains outside the required pull-request path.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
CI = ROOT / ".github" / "workflows" / "ci.yml"
RELEASE = ROOT / ".github" / "workflows" / "release.yml"
OSS_MIRROR = ROOT / ".github" / "workflows" / "oss-mirror.yml"
WEB_PACKAGE = ROOT / "apps" / "web" / "package.json"


def load_ci() -> dict:
    text = CI.read_text()
    # YAML 1.1 treats "on" as a boolean unless we quote it or normalize it.
    return yaml.safe_load(text.replace("\non:", "\n'on':", 1))


def load_oss_mirror() -> dict:
    text = OSS_MIRROR.read_text()
    return yaml.safe_load(text.replace("\non:", "\n'on':", 1))


def test_ci_runs_first_phase_on_pull_requests_and_branch_pushes() -> None:
    workflow = load_ci()
    triggers = workflow["on"]

    assert triggers["workflow_dispatch"]["inputs"]["test_level"]["default"] == "smoke"
    assert triggers["workflow_dispatch"]["inputs"]["test_level"]["options"] == ["smoke", "regression"]
    assert triggers["pull_request"]["branches"] == ["dev", "main"]
    assert triggers["push"]["branches"] == ["dev", "main"]


def test_pull_request_gate_keeps_docker_e2e_out_of_required_jobs() -> None:
    jobs = load_ci()["jobs"]

    assert "e2e" not in jobs
    assert {
        "lint",
        "api-versions",
        "typecheck-frontend",
        "web-source-smoke",
        "python-smoke",
        "python-regression",
    } <= set(jobs)
    assert "docker compose" not in CI.read_text()




def test_ruff_is_advisory_until_the_existing_baseline_is_clean() -> None:
    lint_job = load_ci()["jobs"]["lint"]
    ruff_steps = {
        step["name"]: step["run"]
        for step in lint_job["steps"]
        if isinstance(step, dict) and step.get("name", "").startswith("Ruff ")
    }

    assert lint_job["continue-on-error"] is True
    assert "advisory" in lint_job["name"].lower()
    assert "Ruff lint" in ruff_steps
    assert "Ruff format check" in ruff_steps
    assert "::warning::" in ruff_steps["Ruff lint"]
    assert "::warning::" in ruff_steps["Ruff format check"]


def test_python_smoke_tests_are_the_required_first_phase_python_gate() -> None:
    smoke_job = load_ci()["jobs"]["python-smoke"]
    run_commands = "\n".join(step.get("run", "") for step in smoke_job["steps"] if isinstance(step, dict))

    assert "continue-on-error" not in smoke_job
    assert re.search(r"python -m pytest\s+tests/(\s|$)", run_commands) is not None
    assert "not e2e and not manual and not slow and not network and not docker and not cloud" in run_commands


def test_python_regression_suite_runs_only_for_main_or_manual_opt_in() -> None:
    regression_job = load_ci()["jobs"]["python-regression"]
    run_commands = "\n".join(step.get("run", "") for step in regression_job["steps"] if isinstance(step, dict))

    assert "continue-on-error" not in regression_job
    assert "regression" in regression_job["name"].lower()
    condition = regression_job["if"]
    assert "github.ref_name == 'main'" in condition
    assert "github.base_ref == 'main'" in condition
    assert "inputs.test_level == 'regression'" in condition
    assert re.search(r"python -m pytest\s+tests/(\s|$)", run_commands) is not None
    assert "not manual and not network and not docker and not cloud" in run_commands
    assert "DEPLOYMENT_MODE" not in regression_job["steps"][-1].get("env", {})




def test_web_package_exposes_source_smoke_entrypoint() -> None:
    package = json.loads(WEB_PACKAGE.read_text())

    assert package["scripts"]["test:source"] == "node --test scripts/*.test.mjs"


def test_release_workflow_only_creates_github_release_for_tags() -> None:
    workflow = yaml.safe_load(RELEASE.read_text().replace("\non:", "\n'on':", 1))
    jobs = workflow["jobs"]

    assert set(jobs) == {"release"}
    assert "needs" not in jobs["release"]
    run_commands = "\n".join(
        step.get("run", "")
        for step in jobs["release"]["steps"]
        if isinstance(step, dict)
    )
    uses = [
        step.get("uses")
        for step in jobs["release"]["steps"]
        if isinstance(step, dict)
    ]
    assert "make oss-check" not in run_commands
    assert "softprops/action-gh-release@v2" in uses


def test_oss_mirror_installs_python_dependencies_before_export_checks() -> None:
    workflow = load_oss_mirror()
    steps = workflow["jobs"]["export"]["steps"]

    step_names = [step.get("name", step.get("uses", "")) for step in steps if isinstance(step, dict)]
    check_index = step_names.index("Check OSS boundary")
    previous_steps = steps[:check_index]
    previous_run_commands = "\n".join(
        step.get("run", "") for step in previous_steps if isinstance(step, dict)
    )

    assert any(
        isinstance(step, dict)
        and step.get("uses") == "actions/setup-python@v5"
        and step.get("with", {}).get("python-version") == "3.12"
        for step in previous_steps
    )
    assert 'pip install ".[dev]"' in previous_run_commands


def test_oss_mirror_artifact_includes_hidden_public_files() -> None:
    workflow = load_oss_mirror()
    steps = workflow["jobs"]["export"]["steps"]
    verify_step = next(step for step in steps if step.get("name") == "Verify exported tree")
    upload_step = next(step for step in steps if step.get("uses") == "actions/upload-artifact@v4")

    assert "missing = [path for path in manifest_paths" in verify_step["run"]
    assert 'raise SystemExit("Manifest paths missing from OSS export:' in verify_step["run"]
    assert upload_step["with"]["include-hidden-files"] is True


def test_cloud_source_keeps_private_runtime_surfaces_for_export_to_strip() -> None:
    root = ROOT

    entrypoint = (root / "docker/entrypoint.sh").read_text()
    assert "Base.metadata.create_all(engine)" in entrypoint
    assert "alembic stamp heads" in entrypoint
    assert "20260516_01_merge_commerce_and_repair_heads.py" in entrypoint

    integration_resolution = (root / "packages/core/services/integration_resolution.py").read_text()
    assert "from packages.core.models.worker import Worker" in integration_resolution
    assert "worker_supports_provider" in integration_resolution
    assert "keys.add(_BROWSER_PROVIDER)" in integration_resolution

    api_main = (root / "apps/api/main.py").read_text()

    workspace_setup = (root / "packages/core/services/workspace_setup_service.py").read_text()
    assert "marketplace_agents" in workspace_setup
    assert '"source": _PUBLIC_TEMPLATE_AGENT_SOURCE' in workspace_setup
    assert '_PUBLIC_TEMPLATE_AGENT_SOURCE = "marketplace"' in workspace_setup
