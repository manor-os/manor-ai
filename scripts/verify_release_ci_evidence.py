#!/usr/bin/env python3
"""Verify GitHub Actions regression evidence for an immutable source revision."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

SHA_RE = re.compile(r"[0-9a-f]{40}\Z")
RUN_ID_RE = re.compile(r"[1-9][0-9]*\Z")


def fail(message: str) -> None:
    print(f"Release CI evidence verification failed: {message}", file=sys.stderr)
    raise SystemExit(2)


def gh_json(endpoint: str) -> dict[str, Any]:
    completed = subprocess.run(
        ["gh", "api", "--paginate", "--slurp", endpoint],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode:
        fail("GitHub API query failed.")
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        fail("GitHub API returned malformed JSON.")
    if isinstance(payload, dict):
        return payload
    if not isinstance(payload, list) or not all(isinstance(page, dict) for page in payload):
        fail("GitHub API returned an unexpected JSON payload.")
    merged: dict[str, Any] = {}
    for page in payload:
        for key, value in page.items():
            if isinstance(value, list):
                merged.setdefault(key, []).extend(value)
            elif key not in merged:
                merged[key] = value
    return merged


def belongs_to_repository(run: dict[str, Any], repository: str) -> bool:
    for key in ("repository", "head_repository"):
        value = run.get(key)
        if not isinstance(value, dict) or value.get("full_name") != repository:
            return False
    return True


def matching_identity(run: Any, args: argparse.Namespace) -> bool:
    if not isinstance(run, dict):
        return False
    return (
        str(run.get("id", "")).isdigit()
        and isinstance(run.get("head_sha"), str)
        and SHA_RE.fullmatch(run["head_sha"]) is not None
        and run.get("event") in args.events
        and run.get("head_branch") == args.branch
        and belongs_to_repository(run, args.repository)
    )


def latest_run(runs: list[dict[str, Any]]) -> dict[str, Any]:
    return max(runs, key=lambda run: (str(run.get("created_at", "")), int(run["id"])))


def validate_candidate_trees(args: argparse.Namespace) -> None:
    """Fail closed unless every alternate candidate has the release source tree."""
    alternate_shas = [sha for sha in args.candidate_shas if sha != args.source_sha]
    if not alternate_shas:
        return
    source_commit = gh_json(f"repos/{args.repository}/git/commits/{args.source_sha}")
    source_tree_data = source_commit.get("tree")
    source_tree = source_tree_data.get("sha") if isinstance(source_tree_data, dict) else None
    if not isinstance(source_tree, str):
        fail("could not resolve the release source tree.")
    for candidate_sha in alternate_shas:
        candidate_commit = gh_json(f"repos/{args.repository}/git/commits/{candidate_sha}")
        candidate_tree_data = candidate_commit.get("tree")
        candidate_tree = candidate_tree_data.get("sha") if isinstance(candidate_tree_data, dict) else None
        if candidate_tree != source_tree:
            fail("candidate SHA does not have a tree identical to the release source.")


def runs_for_candidate(args: argparse.Namespace, candidate_sha: str) -> list[dict[str, Any]]:
    query = (
        f"repos/{args.repository}/actions/workflows/{args.workflow}/runs"
        f"?branch={args.branch}&head_sha={candidate_sha}&per_page=100"
    )
    runs = gh_json(query).get("workflow_runs")
    if not isinstance(runs, list):
        fail("GitHub API response has no workflow run list.")
    return [
        run
        for run in runs
        if matching_identity(run, args) and run["head_sha"] == candidate_sha
    ]


def select_run_once(args: argparse.Namespace) -> tuple[dict[str, Any] | None, bool]:
    """Return selected evidence and whether a matching CI attempt is still running."""
    source_runs = runs_for_candidate(args, args.source_sha)
    candidates = source_runs
    if not candidates:
        candidates = [
            latest_run(runs)
            for candidate_sha in args.candidate_shas
            if candidate_sha != args.source_sha
            if (runs := runs_for_candidate(args, candidate_sha))
        ]
    if candidates:
        selected = latest_run(candidates)
        if args.expected_run_id and str(selected["id"]) != args.expected_run_id:
            fail("the expected workflow run is not the latest matching CI evidence.")
        if selected.get("status") != "completed":
            return None, True
        if selected.get("conclusion") != "success":
            fail("the latest matching CI evidence is a completed failure.")
        return selected, False
    return None, False


def select_run(args: argparse.Namespace) -> dict[str, Any]:
    deadline = time.monotonic() + args.wait_seconds
    while True:
        selected, pending = select_run_once(args)
        if selected is not None:
            return selected
        if time.monotonic() >= deadline:
            if pending:
                fail("matching CI evidence did not complete before the release evidence timeout.")
            fail("no matching workflow run exists for the release source candidates before the release evidence timeout.")
        state = "to complete" if pending else "to be created"
        print(f"Waiting for matching CI evidence {state}; retrying in {args.poll_interval_seconds:g} seconds.", file=sys.stderr)
        time.sleep(min(args.poll_interval_seconds, max(0.0, deadline - time.monotonic())))


def require_gate(args: argparse.Namespace, run_id: str) -> None:
    jobs = gh_json(f"repos/{args.repository}/actions/runs/{run_id}/jobs?per_page=100").get("jobs")
    if not isinstance(jobs, list):
        fail("GitHub API response has no job list.")
    gates = [job for job in jobs if isinstance(job, dict) and job.get("name") == args.gate_name]
    if len(gates) != 1 or gates[0].get("conclusion") != "success":
        fail(f"expected exactly one successful {args.gate_name!r} job.")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--workflow", default="ci.yml")
    parser.add_argument("--branch", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--event", dest="events", action="append", default=["push"])
    parser.add_argument("--expected-run-id")
    parser.add_argument(
        "--candidate-sha",
        dest="candidate_shas",
        action="append",
        help="Exact source SHA whose CI may prove the release source. May be repeated.",
    )
    parser.add_argument("--wait-seconds", type=float, default=0)
    parser.add_argument("--poll-interval-seconds", type=float, default=15)
    parser.add_argument("--gate-name", default="Python regression gate")
    parser.add_argument("--github-output")
    args = parser.parse_args()

    if not SHA_RE.fullmatch(args.source_sha):
        fail("source SHA must be a full lowercase Git SHA.")
    args.candidate_shas = list(dict.fromkeys([args.source_sha, *(args.candidate_shas or [])]))
    if not args.candidate_shas or any(SHA_RE.fullmatch(sha) is None for sha in args.candidate_shas):
        fail("candidate SHA must be a full lowercase Git SHA.")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", args.workflow):
        fail("workflow must be a workflow file name.")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", args.branch):
        fail("branch name is invalid.")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", args.repository):
        fail("repository must be an owner/name value.")
    if args.expected_run_id and not RUN_ID_RE.fullmatch(args.expected_run_id):
        fail("expected run ID must be a positive GitHub Actions run ID.")
    if (
        not math.isfinite(args.wait_seconds)
        or not math.isfinite(args.poll_interval_seconds)
        or args.wait_seconds < 0
        or args.poll_interval_seconds <= 0
    ):
        fail("wait duration must be non-negative and poll interval must be positive.")
    if os.environ.get("GITHUB_REPOSITORY") != args.repository:
        fail("repository does not match this GitHub Actions repository.")

    validate_candidate_trees(args)
    run = select_run(args)
    run_id = str(run["id"])
    require_gate(args, run_id)
    output = f"ci_run_id={run_id}\nci_source_sha={run['head_sha']}\nrelease_source_sha={args.source_sha}\n"
    if args.github_output:
        with Path(args.github_output).open("a", encoding="utf-8") as file:
            file.write(output)
    else:
        print(output, end="")


if __name__ == "__main__":
    main()
