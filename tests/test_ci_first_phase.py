"""Contract tests for the first-phase CI gate.

These tests keep the PR gate intentionally small and stable: source checks,
unit tests, type/build checks, and lightweight source smoke tests. Full Docker
stack E2E remains outside the required pull-request path.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.oss_smoke


ROOT = Path(__file__).resolve().parents[1]
CI = ROOT / ".github" / "workflows" / "ci.yml"
RELEASE = ROOT / ".github" / "workflows" / "release.yml"
IS_PUBLIC_EXPORT = not (ROOT / ".ossexclude").exists()
DOCS_DEPLOY = ROOT / ".github" / "workflows" / "deploy-docs.yml"
WEB_PACKAGE = ROOT / "apps" / "web" / "package.json"


def load_ci() -> dict:
    text = CI.read_text()
    # YAML 1.1 treats "on" as a boolean unless we quote it or normalize it.
    return yaml.safe_load(text.replace("\non:", "\n'on':", 1))




def load_docs_deploy() -> dict:
    text = DOCS_DEPLOY.read_text()
    return yaml.safe_load(text.replace("\non:", "\n'on':", 1))


def test_ci_runs_first_phase_on_pull_requests_and_manual_dispatch() -> None:
    workflow = load_ci()
    triggers = workflow["on"]

    assert triggers["workflow_dispatch"]["inputs"]["test_level"]["default"] == "smoke"
    assert triggers["workflow_dispatch"]["inputs"]["test_level"]["options"] == ["smoke", "regression"]
    assert triggers["pull_request"]["branches"] == ["dev", "main"]
    assert "push" not in triggers


def test_pull_request_gate_keeps_docker_e2e_out_of_required_jobs() -> None:
    jobs = load_ci()["jobs"]

    assert "e2e" not in jobs
    assert {
        "lint",
        "api-versions",
        "typecheck-frontend",
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


def test_frontend_checks_share_one_install_for_build_and_source_smoke() -> None:
    jobs = load_ci()["jobs"]
    frontend_job = jobs["typecheck-frontend"]
    assert "web-source-smoke" not in jobs
    steps = [step for step in frontend_job["steps"] if isinstance(step, dict)]
    step_names = [step.get("name", "") for step in steps]
    assert "Build" in step_names
    assert "Run source smoke tests" in step_names
    focus_step = next(
        step for step in steps if step.get("name") == "Run File comment focus browser smoke"
    )
    assert focus_step["run"] == "npm run test:file-comment-focus-browser"
    assert focus_step["env"]["PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH"] == "/usr/bin/google-chrome"
    assert sum(step.get("run") == "npm ci" for step in frontend_job["steps"] if isinstance(step, dict)) == 1


def test_python_smoke_tests_are_the_required_first_phase_python_gate() -> None:
    smoke_job = load_ci()["jobs"]["python-smoke"]
    run_commands = "\n".join(step.get("run", "") for step in smoke_job["steps"] if isinstance(step, dict))

    assert "continue-on-error" not in smoke_job
    assert re.search(r"python -m pytest\s+tests/(\s|$)", run_commands) is not None
    assert '-m "oss_smoke"' in run_commands
    assert "tests/test_integrations.py" not in run_commands


def test_python_ci_jobs_stop_on_the_first_test_failure() -> None:
    jobs = load_ci()["jobs"]
    job_names = ["python-smoke", "python-regression"]

    for job_name in job_names:
        run_commands = "\n".join(
            step.get("run", "") for step in jobs[job_name]["steps"] if isinstance(step, dict)
        )
        assert re.search(r"(?:^|\s)-x(?:\s|$)", run_commands) is not None


def test_python_regression_suite_runs_only_for_main_or_manual_opt_in() -> None:
    regression_job = load_ci()["jobs"]["python-regression"]
    run_commands = "\n".join(step.get("run", "") for step in regression_job["steps"] if isinstance(step, dict))

    assert "continue-on-error" not in regression_job
    assert "regression" in regression_job["name"].lower()
    condition = regression_job["if"]
    assert "github.event_name == 'push'" not in condition
    assert "github.ref_name == 'main'" not in condition
    assert "github.base_ref == 'main'" in condition
    assert "inputs.test_level == 'regression'" in condition
    if not IS_PUBLIC_EXPORT:
        assert re.search(r"python -m pytest\s+tests/(\s|$)", run_commands) is not None
        assert "not manual and not network and not docker and not cloud" in run_commands
    else:
        assert re.search(r"python -m pytest\s+tests/(\s|$)", run_commands) is not None
        assert '-m "oss_smoke or oss_regression"' in run_commands
        assert "tests/test_ci_first_phase.py" not in run_commands
        assert "tests/test_integrations.py" not in run_commands
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




def test_docs_deploy_builds_artifact_and_publishes_only_when_requested() -> None:
    workflow = load_docs_deploy()
    triggers = workflow["on"]
    job = workflow["jobs"]["build-and-deploy"]
    steps = job["steps"]

    assert triggers["push"]["tags"] == ["v*"]
    assert "branches" not in triggers["push"]
    assert "paths" not in triggers["push"]
    assert triggers["workflow_dispatch"]["inputs"]["publish"]["default"] is False
    assert triggers["workflow_dispatch"]["inputs"]["publish"]["type"] == "boolean"
    assert job["if"] == (
        "github.event_name == 'workflow_dispatch' || "
        "(github.event_name == 'push' && startsWith(github.ref, 'refs/tags/v'))"
    )
    assert any(
        step.get("uses") == "actions/setup-node@v7"
        and step.get("with", {}).get("node-version") == "20"
        for step in steps
        if isinstance(step, dict)
    )
    install_step = next(step for step in steps if step.get("name") == "Install and build docs")
    assert install_step["working-directory"] == "docs-site"
    assert "npm ci" in install_step["run"]
    assert "npm run build" in install_step["run"]

    upload_step = next(step for step in steps if step.get("uses") == "actions/upload-artifact@v7")
    assert upload_step["with"]["name"] == "manor-ai-docs-${{ github.sha }}"
    assert upload_step["with"]["path"] == "docs-site/build"

    deploy_step = next(step for step in steps if step.get("name") == "Deploy docs")
    assert deploy_step["if"] == "${{ github.event_name == 'push' || inputs.publish == true }}"
    deploy_with = deploy_step["with"]
    assert deploy_step["uses"] == "peaceiris/actions-gh-pages@v4"
    assert deploy_with["external_repository"] == "manor-os/manor-os.github.io"
    assert deploy_with["destination_dir"] == "docs/manor-ai"
    assert deploy_with["keep_files"] is True


