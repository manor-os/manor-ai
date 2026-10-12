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
DOCS_CI = ROOT / ".github" / "workflows" / "docs-ci.yml"
WEB_PACKAGE = ROOT / "apps" / "web" / "package.json"


def load_ci() -> dict:
    text = CI.read_text()
    # YAML 1.1 treats "on" as a boolean unless we quote it or normalize it.
    return yaml.safe_load(text.replace("\non:", "\n'on':", 1))




def load_docs_deploy() -> dict:
    text = DOCS_DEPLOY.read_text()
    return yaml.safe_load(text.replace("\non:", "\n'on':", 1))


def load_docs_ci() -> dict:
    text = DOCS_CI.read_text()
    return yaml.safe_load(text.replace("\non:", "\n'on':", 1))


def test_ci_uses_private_three_stage_triggers_and_public_pr_triggers() -> None:
    workflow = load_ci()
    triggers = workflow["on"]

    assert triggers["workflow_dispatch"]["inputs"]["test_level"]["default"] == "smoke"
    assert triggers["workflow_dispatch"]["inputs"]["test_level"]["options"] == ["smoke", "regression"]
    if IS_PUBLIC_EXPORT:
        assert triggers["pull_request"]["branches"] == ["dev", "main"]
        assert triggers["push"]["branches"] == ["main"]
    else:
        assert triggers["pull_request"]["branches"] == ["dev", "main"]
        assert triggers["push"]["branches"] == ["dev"]


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




def test_python_smoke_tests_are_the_required_first_phase_python_gate() -> None:
    smoke_job = load_ci()["jobs"]["python-smoke"]
    run_commands = "\n".join(step.get("run", "") for step in smoke_job["steps"] if isinstance(step, dict))

    assert "continue-on-error" not in smoke_job
    assert "python scripts/run_python_tests.py smoke" in run_commands
    assert "--report-dir artifacts/smoke" in run_commands
    if not IS_PUBLIC_EXPORT:
        assert "python scripts/ci_impact.py --run-focused" in run_commands


def test_email_proof_smoke_installs_its_isolated_redis_binary() -> None:
    jobs = load_ci()["jobs"]
    required_jobs = {"python-smoke", "python-regression"}
    for name, job in jobs.items():
        steps = [step for step in job.get("steps", []) if isinstance(step, dict)]
        installs = [step for step in steps if step.get("name") == "Install Redis binary for isolated email proof tests"]
        if name not in required_jobs:
            assert not installs
            continue
        assert len(installs) == 1
        install = installs[0]
        assert "if" not in install and not install.get("continue-on-error")
        command = install["run"]
        assert "sudo systemctl mask redis-server.service" in command
        assert "sudo apt-get install --yes --no-install-recommends redis-server" in command
        assert "redis-server --version" in command
        assert command.index("mask redis-server.service") < command.index("apt-get install")
        test_step = next(step for step in steps if "scripts/run_python_tests.py" in step.get("run", ""))
        assert steps.index(install) < steps.index(test_step)

    source = (ROOT / "tests/test_user_email_proof.py").read_text()
    assert "pytestmark = pytest.mark.oss_smoke" in source
    assert 'dir="/private/tmp"' not in source
    assert "pytest.skip" not in source and "pytest.mark.skip" not in source


def test_python_ci_jobs_use_bounded_runner_failures() -> None:
    jobs = load_ci()["jobs"]
    job_names = ["python-smoke", "python-regression"]

    for job_name in job_names:
        run_commands = "\n".join(
            step.get("run", "") for step in jobs[job_name]["steps"] if isinstance(step, dict)
        )
        assert "scripts/run_python_tests.py" in run_commands
        assert re.search(r"(?:^|\s)-x(?:\s|$)", run_commands) is None

    regression_run = next(
        step["run"]
        for step in jobs["python-regression"]["steps"]
        if isinstance(step, dict) and step.get("name") == "Run Python regression suite"
    )
    if not IS_PUBLIC_EXPORT:
        assert "--maxfail=5" in regression_run


def test_python_regression_suite_uses_the_environment_specific_full_gate() -> None:
    regression_job = load_ci()["jobs"]["python-regression"]
    run_commands = "\n".join(step.get("run", "") for step in regression_job["steps"] if isinstance(step, dict))

    assert "continue-on-error" not in regression_job
    assert "regression" in regression_job["name"].lower()
    condition = regression_job["if"]
    assert "inputs.test_level == 'regression'" in condition
    if not IS_PUBLIC_EXPORT:
        assert "github.event_name == 'push'" in condition
        assert "github.ref == 'refs/heads/dev'" in condition
        assert "github.base_ref == 'main'" not in condition
        assert "python scripts/run_python_tests.py regression" in run_commands
        assert "--maxfail=5" in run_commands
    else:
        assert "github.event_name == 'push'" in condition
        assert "github.ref == 'refs/heads/main'" in condition
        assert "python scripts/run_python_tests.py regression" in run_commands
        assert "tests/test_ci_first_phase.py" not in run_commands
        assert "tests/test_integrations.py" not in run_commands
        public_gate = load_ci()["jobs"]["oss-regression-gate"]
        assert public_gate["name"] == "Python regression gate"
    assert "DEPLOYMENT_MODE" not in regression_job["steps"][-1].get("env", {})








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


