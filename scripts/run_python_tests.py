#!/usr/bin/env python3
"""Run one marker-defined Python test lane and optionally write its artifacts."""

from __future__ import annotations

import argparse
import heapq
import hashlib
import importlib.metadata
import json
import os
import platform
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


REPO_ROOT = Path(__file__).resolve().parents[1]
for import_path in (REPO_ROOT, REPO_ROOT / "tests"):
    if str(import_path) not in sys.path:
        sys.path.insert(0, str(import_path))


LANE_MARKERS = {
    "default": "not e2e and not manual and not slow and not network and not docker and not cloud",
    "smoke": "oss_smoke",
    "regression": "oss_smoke or oss_regression",
}



class SelectedIdsReporter:
    def __init__(self) -> None:
        self.nodeids: list[str] = []
        self.node_markers: dict[str, frozenset[str]] = {}
        self.deselected: list[tuple[str, tuple[str, ...]]] = []
        self.collection_skips: list[str] = []
        self.skipped_reports: list[tuple[str, str]] = []
        self.phase_outcomes: dict[str, Counter[str]] = {
            phase: Counter() for phase in ("setup", "call", "teardown")
        }
        self.phase_durations: Counter[str] = Counter()
        self.node_phase_durations: dict[str, Counter[str]] = defaultdict(Counter)
        self.node_phase_outcomes: dict[str, dict[str, str]] = defaultdict(dict)
        self.slowest: list[tuple[float, str, str, str]] = []

    def pytest_collection_finish(self, session: Any) -> None:
        self.nodeids = [item.nodeid for item in session.items]
        self.node_markers = {
            item.nodeid: frozenset(marker.name for marker in iter_markers())
            for item in session.items
            if callable(iter_markers := getattr(item, "iter_markers", None))
        }
        self._validate_smoke_membership(session.items)

    def pytest_collectreport(self, report: Any) -> None:
        if report.outcome == "skipped":
            self.collection_skips.append(report.nodeid)

    def _validate_smoke_membership(self, selected_items: list[Any]) -> None:
        invalid: list[tuple[str, tuple[str, ...]]] = []
        for item in [*selected_items, *self._deselected_items()]:
            iter_markers = getattr(item, "iter_markers", None)
            if not callable(iter_markers):
                continue
            marker_names = {marker.name for marker in iter_markers()}
            conflicts = tuple(sorted({"manual", "network", "docker"} & marker_names))
            if "oss_smoke" in marker_names and conflicts:
                invalid.append((item.nodeid, conflicts))
        if invalid:
            import pytest

            details = ", ".join(f"{nodeid} ({', '.join(conflicts)})" for nodeid, conflicts in invalid)
            raise pytest.UsageError(
                "oss_smoke tests must not also require manual, network, or docker prerequisites: " + details
            )

    def _deselected_items(self) -> list[Any]:
        return getattr(self, "_deselected", [])

    def pytest_deselected(self, items: list[Any]) -> None:
        self._deselected = [*self._deselected_items(), *items]
        for item in items:
            self.deselected.append(
                (item.nodeid, tuple(sorted(marker.name for marker in item.iter_markers())))
            )

    def pytest_runtest_logreport(self, report: Any) -> None:
        if report.when not in self.phase_outcomes:
            return
        outcome = report.outcome
        if getattr(report, "wasxfail", None):
            outcome = "xfailed" if report.outcome == "skipped" else "xpassed"
        self.phase_outcomes[report.when][outcome] += 1
        if outcome == "skipped":
            self.skipped_reports.append((report.nodeid, report.when))
        self.phase_durations[report.when] += report.duration
        self.node_phase_durations[report.nodeid][report.when] += report.duration
        self.node_phase_outcomes[report.nodeid][report.when] = outcome
        slowest_item = (report.duration, report.nodeid, report.when, outcome)
        if len(self.slowest) < 20:
            heapq.heappush(self.slowest, slowest_item)
        elif slowest_item > self.slowest[0]:
            heapq.heapreplace(self.slowest, slowest_item)


def parser() -> argparse.ArgumentParser:
    argument_parser = argparse.ArgumentParser(description=__doc__)
    argument_parser.add_argument("lane", choices=sorted(LANE_MARKERS))
    argument_parser.add_argument("--markers", "-m", dest="markers", help="override the lane marker expression")
    argument_parser.add_argument("--report-dir", type=Path, help="write lane artifacts to this directory")
    argument_parser.add_argument(
        "--fail-on-skip",
        nargs="?",
        const="*",
        default=None,
        choices=("*", "media_integration"),
        metavar="MARKER",
        help="fail on selected test skips; limit runtime skips to MARKER, while collection skips always fail",
    )
    return argument_parser


_PYTEST_OPTIONS_WITH_VALUES = {
    "-c",
    "-k",
    "-m",
    "-o",
    "-p",
    "--basetemp",
    "--confcutdir",
    "--deselect",
    "--durations-path",
    "--group",
    "--ignore",
    "--ignore-glob",
    "--junit-xml",
    "--junitxml",
    "--lfnf",
    "--maxfail",
    "--override-ini",
    "--rootdir",
    "--splits",
    "--splitting-algorithm",
    "--tb",
}


def _has_test_target(pytest_args: list[str]) -> bool:
    skip_next = False
    for argument in pytest_args:
        if skip_next:
            skip_next = False
            continue
        if argument in _PYTEST_OPTIONS_WITH_VALUES:
            skip_next = True
            continue
        if argument.startswith("-"):
            continue
        target = argument.split("::", 1)[0]
        if (
            target == "tests"
            or target.startswith("tests/")
            or "/" in target
            or target.endswith(".py")
            or Path(target).exists()
        ):
            return True
    return False


def _has_maxfail(pytest_args: list[str]) -> bool:
    return "-x" in pytest_args or any(
        argument == "--maxfail" or argument.startswith("--maxfail=") for argument in pytest_args
    )


def _has_junitxml(pytest_args: list[str]) -> bool:
    return any(
        argument in {"--junitxml", "--junit-xml"}
        or argument.startswith("--junitxml=")
        or argument.startswith("--junit-xml=")
        for argument in pytest_args
    )


def pytest_arguments(markers: str, pytest_args: list[str], report_dir: Path | None) -> list[str]:
    arguments = list(pytest_args)
    separator_index = arguments.index("--") if "--" in arguments else None
    option_args = arguments if separator_index is None else arguments[:separator_index]
    target_args = [] if separator_index is None else arguments[separator_index + 1 :]
    if not _has_test_target(arguments):
        option_args.insert(0, "tests/")
    runner_options = ["-m", markers, "--strict-markers", "-q", "--tb=short", "-p", "no:warnings"]
    if not _has_maxfail(option_args):
        runner_options.append("--maxfail=1")
    if report_dir is not None:
        runner_options.extend(["--junitxml", str(report_dir / "junit.xml")])
    if separator_index is None:
        return option_args + runner_options
    return option_args + runner_options + ["--"] + target_args


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _service_configuration(value: str | None) -> dict[str, object]:
    if not value:
        return {"configured": False}
    parsed = urlparse(value)
    result: dict[str, object] = {"configured": True}
    if parsed.hostname:
        result["host"] = parsed.hostname
    try:
        if parsed.port is not None:
            result["port"] = parsed.port
    except ValueError:
        result["port"] = "invalid"
    return result


def _environment_identity() -> dict[str, object]:
    packages = {}
    for package in ("manor", "pytest", "pytest-asyncio", "pytest-split"):
        try:
            packages[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            packages[package] = None
    return {
        "ci": {
            "run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT"),
            "run_id": os.environ.get("GITHUB_RUN_ID"),
            "sha": os.environ.get("GITHUB_SHA"),
        },
        "deployment_mode": os.environ.get("DEPLOYMENT_MODE"),
        "packages": packages,
        "platform": platform.platform(),
        "python": sys.version,
        "services": {
            "database": _service_configuration(os.environ.get("TEST_DATABASE_URL")),
            "redis": _service_configuration(os.environ.get("TEST_REDIS_URL") or os.environ.get("REDIS_URL")),
        },
        "uv_lock_sha256": hashlib.sha256((REPO_ROOT / "uv.lock").read_bytes()).hexdigest(),
    }


def _option_value(pytest_args: list[str], option: str) -> str | None:
    for index, argument in enumerate(pytest_args):
        if argument == option and index + 1 < len(pytest_args):
            return pytest_args[index + 1]
        if argument.startswith(f"{option}="):
            return argument.split("=", 1)[1]
    return None


def _duration_weight_coverage(reporter: SelectedIdsReporter, pytest_args: list[str]) -> dict[str, object] | None:
    duration_path = _option_value(pytest_args, "--durations-path")
    if duration_path is None:
        return None
    try:
        durations = json.loads(Path(duration_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"path": duration_path, "status": "unavailable"}
    if not isinstance(durations, dict):
        return {"path": duration_path, "status": "unavailable"}
    exact_count = sum(nodeid in durations for nodeid in reporter.nodeids)
    return {
        "path": duration_path,
        "exact_count": exact_count,
        "estimated_count": len(reporter.nodeids) - exact_count,
    }


def _write_reports(
    report_dir: Path,
    *,
    lane: str,
    markers: str,
    pytest_args: list[str],
    reporter: SelectedIdsReporter,
    fail_on_skip: str | None,
    enforced_runtime_skips: list[tuple[str, str]],
    duration_weight_coverage: dict[str, object] | None,
    exit_code: int,
    started_at: float,
) -> None:
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "selected-nodeids.txt").write_text(
        "".join(f"{nodeid}\n" for nodeid in reporter.nodeids), encoding="utf-8"
    )
    _write_json(
        report_dir / "node-durations.json",
        {
            "schema_version": 1,
            "node_durations": {
                nodeid: {
                    phase: round(reporter.node_phase_durations[nodeid][phase], 6)
                    for phase in ("setup", "call", "teardown")
                }
                | {"total": round(sum(reporter.node_phase_durations[nodeid].values()), 6)}
                for nodeid in reporter.nodeids
            },
            "node_outcomes": {
                nodeid: dict(sorted(reporter.node_phase_outcomes[nodeid].items()))
                for nodeid in reporter.nodeids
            },
        },
    )
    _write_json(
        report_dir / "environment.json",
        _environment_identity(),
    )
    shard = _shard_identity(pytest_args)
    marker_deselection_attributable = not any(shard.values()) and "--deselect" not in pytest_args
    deselected_by_marker: Counter[str] = Counter()
    for _, item_markers in reporter.deselected:
        for marker in item_markers:
            deselected_by_marker[marker] += 1
    selection: dict[str, object] = {
        "collected_count": len(reporter.nodeids) + len(reporter.deselected),
        "deselected_by_marker": dict(sorted(deselected_by_marker.items())),
        "deselected_count": len(reporter.deselected),
        "marker_deselection_attributable": marker_deselection_attributable,
        "selected_count": len(reporter.nodeids),
    }
    if not reporter.nodeids and reporter.deselected:
        deselected_path = report_dir / "deselected-nodeids.txt"
        deselected_path.write_text(
            "".join(f"{nodeid}\t{','.join(markers)}\n" for nodeid, markers in reporter.deselected),
            encoding="utf-8",
        )
        selection["deselected_nodeids_file"] = deselected_path.name
    else:
        (report_dir / "deselected-nodeids.txt").unlink(missing_ok=True)
    _write_json(
        report_dir / "summary.json",
        {
            "duration_seconds": round(time.monotonic() - started_at, 3),
            "duration_weights": duration_weight_coverage,
            "exit_code": exit_code,
            "fail_on_skip": fail_on_skip,
            "lane": lane,
            "markers": markers,
            "selected_count": len(reporter.nodeids),
            "pytest_args": pytest_args,
            "selection": selection,
            "shard": shard,
            "phases": {
                phase: {
                    "duration_seconds": round(reporter.phase_durations[phase], 3),
                    "outcomes": dict(sorted(outcomes.items())),
                }
                for phase, outcomes in reporter.phase_outcomes.items()
            },
            "collection": {
                "skipped_count": len(reporter.collection_skips),
                "skipped_nodeids": reporter.collection_skips,
            },
            "runtime_skips": {
                "count": len(reporter.skipped_reports),
                "nodeids": sorted({nodeid for nodeid, _ in reporter.skipped_reports}),
            },
            "skip_enforcement": {
                "collection_skipped_count": len(reporter.collection_skips) if fail_on_skip else 0,
                "runtime_skipped_count": len(enforced_runtime_skips),
            },
            "slowest": [
                {
                    "duration_seconds": round(duration, 3),
                    "nodeid": nodeid,
                    "outcome": outcome,
                    "phase": phase,
                }
                for duration, nodeid, phase, outcome in sorted(reporter.slowest, reverse=True)[:20]
            ],
        },
    )


def _shard_identity(pytest_args: list[str]) -> dict[str, str | None]:
    values: dict[str, str | None] = {"group": None, "splits": None}
    for index, argument in enumerate(pytest_args):
        for name in values:
            option = f"--{name}"
            if argument == option and index + 1 < len(pytest_args):
                values[name] = pytest_args[index + 1]
            elif argument.startswith(f"{option}="):
                values[name] = argument.split("=", 1)[1]
    return values


def _write_github_step_summary(
    *,
    lane: str,
    reporter: SelectedIdsReporter,
    exit_code: int,
    started_at: float,
    pytest_args: list[str],
    duration_weight_coverage: dict[str, object] | None,
) -> None:
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return
    duration = round(time.monotonic() - started_at, 3)
    shard = _shard_identity(pytest_args)
    phase_summary = ", ".join(
        f"{phase}={round(reporter.phase_durations[phase], 3)}s/{sum(outcomes.values())}"
        for phase, outcomes in reporter.phase_outcomes.items()
    )
    shard_summary = ""
    if any(shard.values()):
        shard_summary = f"- Shard: {shard['group'] or '?'} / {shard['splits'] or '?'}\n"
    duration_summary = ""
    if duration_weight_coverage is not None:
        if duration_weight_coverage.get("status") == "unavailable":
            duration_summary = "- Duration weights: unavailable\n"
        else:
            duration_summary = (
                "- Duration weights: "
                f"exact={duration_weight_coverage['exact_count']}, "
                f"estimated={duration_weight_coverage['estimated_count']}\n"
            )
    with Path(summary_path).open("a", encoding="utf-8") as summary_file:
        summary_file.write(
            f"### Python {lane} lane\n\n"
            f"- Selected tests: {len(reporter.nodeids)}\n"
            f"- Exit code: {exit_code}\n"
            f"- Duration: {duration}s\n"
            f"- Phases (duration/tests): {phase_summary}\n"
            f"{shard_summary}"
            f"{duration_summary}"
        )


def _skipped_reports_for_marker(reporter: SelectedIdsReporter, marker: str) -> list[tuple[str, str]]:
    if marker == "*":
        return reporter.skipped_reports
    return [
        (nodeid, phase)
        for nodeid, phase in reporter.skipped_reports
        if marker in reporter.node_markers.get(nodeid, frozenset())
    ]


def main(argv: list[str] | None = None) -> int:
    arguments, pytest_args = parser().parse_known_args(argv)
    markers = arguments.markers or LANE_MARKERS[arguments.lane]
    if not markers.strip():
        parser().error("--markers must not be empty")
    report_dir = arguments.report_dir
    if report_dir is not None:
        if _has_junitxml(pytest_args):
            parser().error("--report-dir manages the JUnit output; do not pass --junitxml")
        report_dir.mkdir(parents=True, exist_ok=True)

    try:
        import pytest
    except ImportError:
        print("pytest is required; run `uv sync --locked --extra dev` first.", file=sys.stderr)
        return 2

    runner_args = pytest_arguments(markers, pytest_args, report_dir)
    reporter = SelectedIdsReporter()
    started_at = time.monotonic()
    exit_code = int(pytest.main(runner_args, plugins=[reporter]))
    runtime_skips = _skipped_reports_for_marker(reporter, arguments.fail_on_skip) if arguments.fail_on_skip else []
    collection_skips = reporter.collection_skips if arguments.fail_on_skip else []
    if runtime_skips or collection_skips:
        details: list[str] = []
        if runtime_skips:
            details.append(f"{len(runtime_skips)} selected test phase(s) skipped")
        if collection_skips:
            details.append(f"{len(collection_skips)} collection target(s) skipped")
        print(f"{'; '.join(details)} with --fail-on-skip.", file=sys.stderr)
        if exit_code in (0, 5):
            exit_code = 1
    if report_dir is not None:
        duration_weight_coverage = _duration_weight_coverage(reporter, runner_args)
        _write_reports(
            report_dir,
            lane=arguments.lane,
            markers=markers,
            pytest_args=runner_args,
            reporter=reporter,
            fail_on_skip=arguments.fail_on_skip,
            enforced_runtime_skips=runtime_skips,
            duration_weight_coverage=duration_weight_coverage,
            exit_code=exit_code,
            started_at=started_at,
        )
    _write_github_step_summary(
        lane=arguments.lane,
        reporter=reporter,
        exit_code=exit_code,
        started_at=started_at,
        pytest_args=runner_args,
        duration_weight_coverage=_duration_weight_coverage(reporter, runner_args),
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
