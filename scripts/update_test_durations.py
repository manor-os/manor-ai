#!/usr/bin/env python3
"""Merge complete successful test reports into pytest-split duration weights."""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any


MIN_DURATION_SECONDS = 0.001
PHASES = ("setup", "call", "teardown")


@dataclass(frozen=True)
class Report:
    directory: Path
    lane: str
    markers: str
    group: int | None
    splits: int | None
    environment: dict[str, Any]
    durations: dict[str, float]


def parser() -> argparse.ArgumentParser:
    argument_parser = argparse.ArgumentParser(description=__doc__)
    argument_parser.add_argument(
        "--durations-path",
        type=Path,
        default=Path(".test_durations"),
        help="existing pytest-split duration JSON to refresh",
    )
    argument_parser.add_argument(
        "report_dirs",
        type=Path,
        nargs="+",
        help="explicit report directories from one successful complete run",
    )
    return argument_parser


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise ValueError(f"Missing required report file: {path}") from error
    except json.JSONDecodeError as error:
        raise ValueError(f"Invalid JSON in {path}: {error.msg}") from error


def _positive_shard_value(value: object, name: str, directory: Path) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        parsed = value
    elif isinstance(value, str) and value.isdecimal():
        parsed = int(value)
    else:
        raise ValueError(f"{directory}: shard {name} must be an integer")
    if parsed < 1:
        raise ValueError(f"{directory}: shard {name} must be positive")
    return parsed


def _shard_identity(summary: dict[str, Any], directory: Path) -> tuple[int | None, int | None]:
    shard = summary.get("shard")
    if not isinstance(shard, dict):
        raise ValueError(f"{directory}: summary has no shard identity")
    group, splits = shard.get("group"), shard.get("splits")
    if group is None and splits is None:
        return None, None
    if group is None or splits is None:
        raise ValueError(f"{directory}: shard group and splits must be set together")
    group_value = _positive_shard_value(group, "group", directory)
    splits_value = _positive_shard_value(splits, "splits", directory)
    if group_value > splits_value:
        raise ValueError(f"{directory}: shard group exceeds split count")
    return group_value, splits_value


def _duration_value(value: object, nodeid: str, directory: Path) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{directory}: {nodeid} has an invalid duration")
    if value < 0:
        raise ValueError(f"{directory}: {nodeid} has a negative duration")
    return float(value)


def _validate_summary_outcomes(summary: dict[str, Any], directory: Path) -> None:
    phases = summary.get("phases")
    if not isinstance(phases, dict):
        raise ValueError(f"{directory}: summary has no phase outcomes")
    for phase in PHASES:
        value = phases.get(phase)
        if not isinstance(value, dict) or not isinstance(value.get("outcomes"), dict):
            raise ValueError(f"{directory}: summary has no {phase} outcomes")
        outcomes = value["outcomes"]
        if outcomes.get("failed", 0) or outcomes.get("xpassed", 0):
            raise ValueError(f"{directory}: report contains failed or xpassed tests")


def _validate_node_execution(
    nodeid: str, outcomes: object, directory: Path,
) -> None:
    if not isinstance(outcomes, dict) or not all(
        isinstance(phase, str) and isinstance(outcome, str)
        for phase, outcome in outcomes.items()
    ):
        raise ValueError(f"{directory}: {nodeid} has invalid execution outcomes")
    if set(outcomes) - set(PHASES) or not {"setup", "teardown"} <= outcomes.keys():
        raise ValueError(f"{directory}: {nodeid} has incomplete execution phases")
    if outcomes["setup"] not in {"passed", "skipped", "xfailed"} or outcomes["teardown"] != "passed":
        raise ValueError(f"{directory}: {nodeid} did not complete successfully")
    if outcomes["setup"] == "passed":
        if outcomes.get("call") not in {"passed", "skipped", "xfailed"}:
            raise ValueError(f"{directory}: {nodeid} has an invalid call outcome")
    elif "call" in outcomes:
        raise ValueError(f"{directory}: {nodeid} ran after a skipped setup")


def _runtime_identity(environment: dict[str, Any]) -> dict[str, Any]:
    # Shards of one run may land on runners with different kernel patch
    # releases; Python, lock, packages and OS/libc must still match.
    return {
        key: value
        for key, value in environment.items()
        if key not in {"ci", "deployment_mode", "kernel"}
    }


def _lane_identity(environment: dict[str, Any]) -> dict[str, Any]:
    # Within a lane the deployment mode must match as well.
    return {key: value for key, value in environment.items() if key != "kernel"}


def _ci_identity(environment: dict[str, Any], directory: Path) -> tuple[str | None, str | None, str | None]:
    value = environment.get("ci")
    if value is None:
        return None, None, None
    if not isinstance(value, dict):
        raise ValueError(f"{directory}: CI identity is invalid")
    values = tuple(value.get(name) for name in ("run_id", "run_attempt", "sha"))
    if not all(item is None or isinstance(item, str) for item in values):
        raise ValueError(f"{directory}: CI identity is invalid")
    return values


def load_report(directory: Path) -> Report:
    directory = directory.resolve()
    summary = _read_json(directory / "summary.json")
    environment = _read_json(directory / "environment.json")
    duration_report = _read_json(directory / "node-durations.json")
    if not isinstance(summary, dict) or not isinstance(environment, dict) or not isinstance(duration_report, dict):
        raise ValueError(f"{directory}: report files must contain JSON objects")
    if summary.get("exit_code") != 0:
        raise ValueError(f"{directory}: report did not succeed")
    if isinstance(summary.get("selected_count"), bool) or not isinstance(summary.get("selected_count"), int):
        raise ValueError(f"{directory}: summary has no selected test count")
    pytest_args = summary.get("pytest_args")
    if not isinstance(pytest_args, list) or not all(isinstance(argument, str) for argument in pytest_args):
        raise ValueError(f"{directory}: summary has invalid pytest arguments")
    if summary["selected_count"] <= 0 or any(
        argument == "--collect-only" or argument.startswith("--collect-only=")
        for argument in pytest_args
    ):
        raise ValueError(f"{directory}: report did not execute selected tests")
    _validate_summary_outcomes(summary, directory)
    collection = summary.get("collection")
    if not isinstance(collection, dict) or collection.get("skipped_count") != 0:
        raise ValueError(f"{directory}: collection skips cannot update duration weights")
    lane, markers = summary.get("lane"), summary.get("markers")
    if not isinstance(lane, str) or not lane or not isinstance(markers, str) or not markers:
        raise ValueError(f"{directory}: summary has no lane marker identity")
    nodeids = (directory / "selected-nodeids.txt").read_text(encoding="utf-8").splitlines()
    if len(nodeids) != summary["selected_count"] or not nodeids or len(set(nodeids)) != len(nodeids):
        raise ValueError(f"{directory}: selected node IDs are incomplete or duplicated")
    if (
        duration_report.get("schema_version") != 1
        or not isinstance(duration_report.get("node_durations"), dict)
        or not isinstance(duration_report.get("node_outcomes"), dict)
    ):
        raise ValueError(f"{directory}: unsupported node duration report")
    recorded = duration_report["node_durations"]
    outcomes = duration_report["node_outcomes"]
    if set(recorded) != set(nodeids) or set(outcomes) != set(nodeids):
        raise ValueError(f"{directory}: node reports do not exactly cover selected tests")
    durations: dict[str, float] = {}
    for nodeid in nodeids:
        _validate_node_execution(nodeid, outcomes[nodeid], directory)
        phases = recorded[nodeid]
        if not isinstance(phases, dict):
            raise ValueError(f"{directory}: {nodeid} has an invalid phase record")
        phase_total = sum(_duration_value(phases.get(phase), nodeid, directory) for phase in PHASES)
        recorded_total = _duration_value(phases.get("total"), nodeid, directory)
        if abs(phase_total - recorded_total) > 0.00001:
            raise ValueError(f"{directory}: {nodeid} total does not match phase durations")
        durations[nodeid] = max(recorded_total, MIN_DURATION_SECONDS)
    group, splits = _shard_identity(summary, directory)
    return Report(directory, lane, markers, group, splits, environment, durations)


def validate_reports(report_dirs: list[Path]) -> list[Report]:
    if len({directory.resolve() for directory in report_dirs}) != len(report_dirs):
        raise ValueError("Report directories must be unique")
    reports = [load_report(directory) for directory in report_dirs]
    runtime_identity = _runtime_identity(reports[0].environment)
    if any(_runtime_identity(report.environment) != runtime_identity for report in reports[1:]):
        raise ValueError("Reports must use the same runtime, lock, package, and platform identity")
    ci_identities = [_ci_identity(report.environment, report.directory) for report in reports]
    if any(any(value is not None for value in identity) for identity in ci_identities):
        if (
            any(any(not value for value in identity) for identity in ci_identities)
            or len(set(ci_identities)) != 1
        ):
            raise ValueError("Reports must come from the same complete CI run identity")
    lanes: dict[str, list[Report]] = {}
    seen_nodeids: set[str] = set()
    for report in reports:
        if report.lane in lanes and any(item.markers != report.markers for item in lanes[report.lane]):
            raise ValueError(f"{report.lane}: reports disagree on the marker expression")
        overlap = seen_nodeids & set(report.durations)
        if overlap:
            raise ValueError(f"Duplicate selected test across reports: {sorted(overlap)[0]}")
        seen_nodeids.update(report.durations)
        lanes.setdefault(report.lane, []).append(report)
    for lane, lane_reports in lanes.items():
        environment = _lane_identity(lane_reports[0].environment)
        if any(_lane_identity(report.environment) != environment for report in lane_reports[1:]):
            raise ValueError(f"{lane}: reports must use the same environment identity")
        sharded = [report for report in lane_reports if report.group is not None]
        if sharded and len(sharded) != len(lane_reports):
            raise ValueError(f"{lane}: cannot mix sharded and unsharded reports")
        if not sharded:
            if len(lane_reports) != 1:
                raise ValueError(f"{lane}: expected one unsharded report")
            continue
        splits = sharded[0].splits
        if any(report.splits != splits for report in sharded):
            raise ValueError(f"{lane}: reports disagree on shard count")
        groups = {report.group for report in sharded}
        expected = set(range(1, (splits or 0) + 1))
        if groups != expected or len(sharded) != len(expected):
            raise ValueError(f"{lane}: reports do not include every shard")
    return reports


def load_duration_cache(path: Path) -> dict[str, float]:
    if not path.exists():
        return {}
    value = _read_json(path)
    if not isinstance(value, dict):
        raise ValueError(f"{path}: duration cache must be a JSON object")
    return {
        nodeid: _duration_value(duration, nodeid, path)
        for nodeid, duration in value.items()
        if isinstance(nodeid, str)
    }


def merge_duration_reports(existing: dict[str, float], reports: list[Report]) -> dict[str, float]:
    refreshed = dict(existing)
    for report in reports:
        refreshed.update(report.durations)
    return dict(sorted(refreshed.items()))


def main(argv: list[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    try:
        reports = validate_reports(arguments.report_dirs)
        existing = load_duration_cache(arguments.durations_path)
        merged = merge_duration_reports(existing, reports)
    except ValueError as error:
        parser().error(str(error))
    arguments.durations_path.write_text(json.dumps(merged, indent=4, sort_keys=True) + "\n", encoding="utf-8")
    measured = sum(len(report.durations) for report in reports)
    print(f"Refreshed {measured} measured test durations from {len(reports)} report(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
