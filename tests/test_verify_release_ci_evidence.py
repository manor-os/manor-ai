from __future__ import annotations

import json
import os
import subprocess
import sys
from argparse import Namespace
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.oss_smoke

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "verify_release_ci_evidence.py"
RELEASE_WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"
DOCS_WORKFLOW = ROOT / ".github" / "workflows" / "deploy-docs.yml"
SHA = "a" * 40
REPOSITORY = "example/manor"


def load_verifier_module():
    spec = spec_from_file_location("verify_release_ci_evidence", SCRIPT)
    assert spec and spec.loader
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_verifier(
    tmp_path: Path,
    runs: list[dict],
    jobs: list[dict],
    *extra: str,
    trees: dict[str, str] | None = None,
    run_pages: list[list[dict]] | None = None,
) -> subprocess.CompletedProcess[str]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    payload = json.dumps(
        [{"workflow_runs": page} for page in run_pages] if run_pages is not None else {"workflow_runs": runs}
    )
    job_payload = json.dumps({"jobs": jobs})
    tree_payload = repr(trees or {SHA: "tree-a"})
    gh = fake_bin / "gh"
    gh.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        f"runs = {payload!r}\n"
        f"jobs = {job_payload!r}\n"
        f"trees = {tree_payload}\n"
        "endpoint = sys.argv[-1]\n"
        "if '/jobs?' in endpoint:\n"
        " print(jobs)\n"
        "elif '/git/commits/' in endpoint:\n"
        " print(json.dumps({'tree': {'sha': trees.get(endpoint.rsplit('/', 1)[-1])}}))\n"
        "else:\n"
        " print(runs)\n",
        encoding="utf-8",
    )
    gh.chmod(0o755)
    env = os.environ | {
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "GITHUB_REPOSITORY": REPOSITORY,
    }
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--source-sha",
            SHA,
            "--branch",
            "dev",
            "--repository",
            REPOSITORY,
            *extra,
        ],
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )


def evidence_run(**overrides: object) -> dict:
    return {
        "id": 42,
        "head_sha": SHA,
        "event": "push",
        "head_branch": "dev",
        "status": "completed",
        "conclusion": "success",
        "created_at": "2026-10-08T00:00:00Z",
        "repository": {"full_name": REPOSITORY},
        "head_repository": {"full_name": REPOSITORY},
    } | overrides


def test_verifier_emits_matching_successful_regression_evidence(tmp_path: Path) -> None:
    completed = run_verifier(tmp_path, [evidence_run()], [{"name": "Python regression gate", "conclusion": "success"}])
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == (f"ci_run_id=42\nci_source_sha={SHA}\nrelease_source_sha={SHA}\n")


@pytest.mark.parametrize(
    "run_overrides",
    [
        {"event": "pull_request"},
        {"head_branch": "main"},
        {"head_sha": "b" * 40},
        {"repository": {"full_name": "other/repo"}},
    ],
)
def test_verifier_rejects_untrusted_run_identity(tmp_path: Path, run_overrides: dict) -> None:
    completed = run_verifier(
        tmp_path, [evidence_run(**run_overrides)], [{"name": "Python regression gate", "conclusion": "success"}]
    )
    assert completed.returncode != 0
    assert not completed.stdout


@pytest.mark.parametrize(
    "jobs",
    [
        [],
        [{"name": "Python regression gate", "conclusion": "failure"}],
        [{"name": "Python regression gate", "conclusion": "success"}] * 2,
    ],
)
def test_verifier_requires_one_successful_gate(tmp_path: Path, jobs: list[dict]) -> None:
    completed = run_verifier(tmp_path, [evidence_run()], jobs)
    assert completed.returncode != 0
    assert "expected exactly one successful" in completed.stderr


def test_verifier_requires_the_expected_run_id(tmp_path: Path) -> None:
    completed = run_verifier(
        tmp_path,
        [evidence_run()],
        [{"name": "Python regression gate", "conclusion": "success"}],
        "--expected-run-id",
        "99",
    )
    assert completed.returncode != 0
    assert "expected workflow run" in completed.stderr


def test_verifier_reads_paginated_runs_and_rejects_a_newer_failed_attempt(tmp_path: Path) -> None:
    completed = run_verifier(
        tmp_path,
        [],
        [{"name": "Python regression gate", "conclusion": "success"}],
        run_pages=[
            [evidence_run(id=42, created_at="2026-10-08T00:00:00Z")],
            [evidence_run(id=43, created_at="2026-10-09T00:00:00Z", conclusion="failure")],
        ],
    )
    assert completed.returncode != 0
    assert "latest matching CI evidence is a completed failure" in completed.stderr


def test_verifier_does_not_accept_an_old_expected_run_after_a_retry(tmp_path: Path) -> None:
    completed = run_verifier(
        tmp_path,
        [
            evidence_run(id=42, created_at="2026-10-08T00:00:00Z"),
            evidence_run(id=43, created_at="2026-10-09T00:00:00Z", conclusion="failure"),
        ],
        [{"name": "Python regression gate", "conclusion": "success"}],
        "--expected-run-id",
        "42",
    )
    assert completed.returncode != 0
    assert "not the latest" in completed.stderr


def test_verifier_accepts_an_explicit_promotion_candidate(tmp_path: Path) -> None:
    dev_sha = "b" * 40
    completed = run_verifier(
        tmp_path,
        [evidence_run(id=43, head_sha=dev_sha)],
        [{"name": "Python regression gate", "conclusion": "success"}],
        "--candidate-sha",
        dev_sha,
        trees={SHA: "shared-tree", dev_sha: "shared-tree"},
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == (f"ci_run_id=43\nci_source_sha={dev_sha}\nrelease_source_sha={SHA}\n")


def test_verifier_does_not_search_unlisted_tree_equivalent_commits(tmp_path: Path) -> None:
    dev_sha = "b" * 40
    completed = run_verifier(
        tmp_path,
        [evidence_run(head_sha=dev_sha)],
        [{"name": "Python regression gate", "conclusion": "success"}],
        trees={SHA: "main-tree", dev_sha: "main-tree"},
    )
    assert completed.returncode != 0
    assert "no matching workflow run exists" in completed.stderr


def test_verifier_rejects_an_explicit_candidate_with_a_different_tree(tmp_path: Path) -> None:
    dev_sha = "b" * 40
    completed = run_verifier(
        tmp_path,
        [evidence_run(head_sha=dev_sha)],
        [{"name": "Python regression gate", "conclusion": "success"}],
        "--candidate-sha",
        dev_sha,
        trees={SHA: "release-tree", dev_sha: "other-tree"},
    )

    assert completed.returncode != 0
    assert "candidate SHA does not have a tree identical" in completed.stderr


def test_verifier_gives_exact_source_failure_precedence_over_a_parent_green(tmp_path: Path) -> None:
    dev_sha = "b" * 40
    completed = run_verifier(
        tmp_path,
        [
            evidence_run(id=42, conclusion="failure", created_at="2026-10-09T00:00:00Z"),
            evidence_run(id=41, head_sha=dev_sha, created_at="2026-10-08T00:00:00Z"),
        ],
        [{"name": "Python regression gate", "conclusion": "success"}],
        "--candidate-sha",
        dev_sha,
        trees={SHA: "shared-tree", dev_sha: "shared-tree"},
    )

    assert completed.returncode != 0
    assert "latest matching CI evidence is a completed failure" in completed.stderr


def test_verifier_rejects_newer_failed_parent_before_an_older_green_parent(monkeypatch: pytest.MonkeyPatch) -> None:
    verifier = load_verifier_module()
    old_sha = "b" * 40
    failed_sha = "c" * 40
    runs = {
        SHA: [],
        old_sha: [evidence_run(id=41, head_sha=old_sha, created_at="2026-10-08T00:00:00Z")],
        failed_sha: [
            evidence_run(
                id=42,
                head_sha=failed_sha,
                created_at="2026-10-09T00:00:00Z",
                conclusion="failure",
            )
        ],
    }
    args = Namespace(candidate_shas=[SHA, old_sha, failed_sha], source_sha=SHA, expected_run_id=None)
    monkeypatch.setattr(verifier, "runs_for_candidate", lambda _args, sha: runs[sha])

    with pytest.raises(SystemExit, match="2"):
        verifier.select_run_once(args)


def test_verifier_polls_a_queued_exact_candidate_without_sleeping_in_tests(monkeypatch: pytest.MonkeyPatch) -> None:
    verifier = load_verifier_module()
    queued = evidence_run(status="queued", conclusion=None)
    successful = evidence_run()
    observations = iter([([queued]), ([successful])])
    clock = iter([0.0, 0.0, 0.1, 0.1])
    sleeps: list[float] = []
    args = Namespace(
        candidate_shas=[SHA],
        source_sha=SHA,
        expected_run_id=None,
        wait_seconds=1,
        poll_interval_seconds=0.25,
    )
    monkeypatch.setattr(verifier, "runs_for_candidate", lambda _args, _sha: next(observations))
    monkeypatch.setattr(verifier.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(verifier.time, "sleep", sleeps.append)

    assert verifier.select_run(args) == successful
    assert sleeps == [0.25]


def test_verifier_reports_a_bounded_timeout_for_queued_ci(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    verifier = load_verifier_module()
    args = Namespace(
        candidate_shas=[SHA],
        source_sha=SHA,
        expected_run_id=None,
        wait_seconds=1,
        poll_interval_seconds=0.25,
    )
    monkeypatch.setattr(verifier, "runs_for_candidate", lambda _args, _sha: [evidence_run(status="in_progress")])
    clock = iter([0.0, 0.0, 1.0, 1.0])
    monkeypatch.setattr(verifier.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(verifier.time, "sleep", lambda _seconds: None)

    with pytest.raises(SystemExit, match="2"):
        verifier.select_run(args)

    assert "did not complete before the release evidence timeout" in capsys.readouterr().err


@pytest.mark.parametrize("workflow_path", [RELEASE_WORKFLOW, DOCS_WORKFLOW])
@pytest.mark.parametrize("merged", [True, False])
def test_publish_source_step_requires_a_main_ancestor(tmp_path: Path, workflow_path: Path, merged: bool) -> None:
    origin = tmp_path / "origin.git"
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "--bare", str(origin)], check=True, capture_output=True)
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)

    def git(*args: str) -> str:
        return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()

    git("config", "user.name", "Release Test")
    git("config", "user.email", "release-test@example.test")
    git("remote", "add", "origin", str(origin))
    (repo / "release.txt").write_text("base\n", encoding="utf-8")
    git("add", "release.txt")
    git("commit", "-m", "base")
    git("branch", "-M", "main")
    base_sha = git("rev-parse", "HEAD")
    git("push", "-u", "origin", "main")

    if merged:
        (repo / "release.txt").write_text("main\n", encoding="utf-8")
        git("commit", "-am", "main follow-up")
        git("push", "origin", "main")
        git("checkout", "--detach", base_sha)
    else:
        git("checkout", "-b", "release-candidate")
        (repo / "release.txt").write_text("candidate\n", encoding="utf-8")
        git("commit", "-am", "candidate")

    workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8").replace("\non:", "\n'on':", 1))
    expected_timeout = 65 if workflow_path == RELEASE_WORKFLOW else 75
    assert workflow["jobs"]["release" if workflow_path == RELEASE_WORKFLOW else "build-and-deploy"]["timeout-minutes"] == expected_timeout
    step = next(
        item
        for item in workflow["jobs"]["release" if workflow_path == RELEASE_WORKFLOW else "build-and-deploy"]["steps"]
        if item.get("name") == "Resolve published source"
    )
    output = tmp_path / "github-output"
    completed = subprocess.run(
        ["bash", "-c", step["run"]],
        cwd=repo,
        env=os.environ | {"GITHUB_OUTPUT": str(output)},
        text=True,
        capture_output=True,
        check=False,
    )

    assert (completed.returncode == 0) is merged, completed.stderr
    if merged:
        assert output.read_text(encoding="utf-8") == f"release_sha={base_sha}\n"
    else:
        assert "not included in main" in completed.stderr
