import importlib.util
import json
import sys
from pathlib import Path

import pytest


pytestmark = pytest.mark.oss_smoke


@pytest.fixture
def duration_module():
    path = Path(__file__).parents[1] / "scripts" / "update_test_durations.py"
    spec = importlib.util.spec_from_file_location("update_test_durations", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write_report(
    directory: Path,
    *,
    lane: str = "regression",
    group: int | None = None,
    splits: int | None = None,
    node_durations: dict[str, float] | None = None,
    environment: dict | None = None,
    pytest_args: list[str] | None = None,
) -> Path:
    directory.mkdir()
    node_durations = node_durations or {"tests/test_example.py::test_case": 0.3}
    (directory / "selected-nodeids.txt").write_text("".join(f"{nodeid}\n" for nodeid in node_durations))
    (directory / "summary.json").write_text(
        json.dumps(
            {
                "exit_code": 0,
                "selected_count": len(node_durations),
                "pytest_args": pytest_args or ["tests/"],
                "collection": {"skipped_count": 0},
                "phases": {
                    phase: {"outcomes": {"passed": len(node_durations)}}
                    for phase in ("setup", "call", "teardown")
                },
                "lane": lane,
                "markers": f"marker-{lane}",
                "shard": {"group": group, "splits": splits},
            }
        )
    )
    (directory / "environment.json").write_text(json.dumps(environment or {"lock": "same"}))
    (directory / "node-durations.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "node_durations": {
                    nodeid: (
                        {"setup": 0, "call": 0, "teardown": 0, "total": 0}
                        if duration == 0
                        else {"setup": 0.1, "call": duration - 0.1, "teardown": 0, "total": duration}
                    )
                    for nodeid, duration in node_durations.items()
                },
                "node_outcomes": {
                    nodeid: {"setup": "passed", "call": "passed", "teardown": "passed"}
                    for nodeid in node_durations
                },
            }
        )
    )
    return directory


def test_merges_complete_reports_and_preserves_unmeasured_cache_entries(duration_module, tmp_path):
    first = _write_report(
        tmp_path / "regression-1",
        group=1,
        splits=2,
        node_durations={"tests/test_measured.py::test_first": 0.5},
    )
    second = _write_report(
        tmp_path / "regression-2",
        group=2,
        splits=2,
        node_durations={"tests/test_measured.py::test_second": 0},
    )
    durations_path = tmp_path / "durations.json"
    durations_path.write_text(json.dumps({"tests/test_old.py::test_old": 7, "tests/test_measured.py::test_first": 9}))

    assert duration_module.main(["--durations-path", str(durations_path), str(first), str(second)]) == 0

    assert json.loads(durations_path.read_text()) == {
        "tests/test_measured.py::test_first": 0.5,
        "tests/test_measured.py::test_second": duration_module.MIN_DURATION_SECONDS,
        "tests/test_old.py::test_old": 7.0,
    }


def test_rejects_partial_sharded_lane(duration_module, tmp_path):
    report = _write_report(tmp_path / "regression-1", group=1, splits=2)

    with pytest.raises(ValueError, match="include every shard"):
        duration_module.validate_reports([report])


def test_rejects_collection_only_report(duration_module, tmp_path):
    report = _write_report(tmp_path / "collected", pytest_args=["tests/", "--collect-only"])

    with pytest.raises(ValueError, match="did not execute selected tests"):
        duration_module.validate_reports([report])


@pytest.mark.parametrize(
    "outcomes",
    [
        {"setup": "passed", "call": "passed"},
        {"setup": "passed", "call": "unknown", "teardown": "passed"},
    ],
)
def test_rejects_incomplete_or_unknown_execution_outcomes(duration_module, tmp_path, outcomes):
    report = _write_report(tmp_path / "invalid-outcome")
    path = report / "node-durations.json"
    data = json.loads(path.read_text())
    data["node_outcomes"]["tests/test_example.py::test_case"] = outcomes
    path.write_text(json.dumps(data))

    with pytest.raises(ValueError):
        duration_module.load_report(report)


def test_rejects_environment_mismatch(duration_module, tmp_path):
    first = _write_report(
        tmp_path / "first",
        lane="regression",
        group=1,
        splits=2,
        environment={"lock": "same", "deployment_mode": "oss"},
        node_durations={"tests/test_environment.py::test_first": 0.2},
    )
    second = _write_report(
        tmp_path / "second",
        lane="regression",
        group=2,
        splits=2,
        environment={"lock": "same", "deployment_mode": "cloud"},
        node_durations={"tests/test_environment.py::test_second": 0.2},
    )

    with pytest.raises(ValueError, match="same environment"):
        duration_module.validate_reports([first, second])


def test_allows_lane_specific_deployment_modes_with_the_same_runtime(duration_module, tmp_path):
    regression = _write_report(
        tmp_path / "regression",
        lane="regression",
        environment={"lock": "same", "deployment_mode": "oss"},
        node_durations={"tests/test_runtime.py::test_oss": 0.2},
    )
    cloud = _write_report(
        tmp_path / "cloud",
        lane="cloud",
        environment={"lock": "same", "deployment_mode": "cloud"},
        node_durations={"tests/test_runtime.py::test_cloud": 0.2},
    )

    assert len(duration_module.validate_reports([regression, cloud])) == 2


def test_shards_on_different_kernel_releases_share_one_runtime(duration_module, tmp_path):
    runtime = {"lock": "same", "platform": "Linux-x86_64-glibc-2.39"}
    first = _write_report(
        tmp_path / "first",
        group=1,
        splits=2,
        environment={**runtime, "kernel": "6.17.0-1022-azure"},
        node_durations={"tests/test_kernel.py::test_first": 0.2},
    )
    second = _write_report(
        tmp_path / "second",
        group=2,
        splits=2,
        environment={**runtime, "kernel": "7.0.0-1017-azure"},
        node_durations={"tests/test_kernel.py::test_second": 0.2},
    )
    assert len(duration_module.validate_reports([first, second])) == 2

    other_libc = _write_report(
        tmp_path / "other-libc",
        group=2,
        splits=2,
        environment={"lock": "same", "platform": "Linux-x86_64-glibc-2.41", "kernel": "7.0.0-1017-azure"},
        node_durations={"tests/test_kernel.py::test_second": 0.2},
    )
    with pytest.raises(ValueError, match="same runtime"):
        duration_module.validate_reports([first, other_libc])


def test_rejects_mismatched_ci_run_identity(duration_module, tmp_path):
    first = _write_report(
        tmp_path / "first",
        group=1,
        splits=2,
        environment={"lock": "same", "ci": {"run_id": "1", "run_attempt": "1", "sha": "a"}},
        node_durations={"tests/test_ci_identity.py::test_first": 0.2},
    )
    second = _write_report(
        tmp_path / "second",
        group=2,
        splits=2,
        environment={"lock": "same", "ci": {"run_id": "2", "run_attempt": "1", "sha": "a"}},
        node_durations={"tests/test_ci_identity.py::test_second": 0.2},
    )

    with pytest.raises(ValueError, match="same complete CI run"):
        duration_module.validate_reports([first, second])


@pytest.mark.parametrize("group", [True, 1.5])
def test_rejects_non_integer_shard_identity(duration_module, tmp_path, group):
    report = _write_report(tmp_path / "invalid-shard", group=group, splits=2)

    with pytest.raises(ValueError, match="must be an integer"):
        duration_module.load_report(report)


def test_rejects_failed_execution_outcome(duration_module, tmp_path):
    report = _write_report(tmp_path / "failed")
    summary_path = report / "summary.json"
    summary = json.loads(summary_path.read_text())
    summary["phases"]["call"]["outcomes"] = {"failed": 1}
    summary_path.write_text(json.dumps(summary))

    with pytest.raises(ValueError, match="failed or xpassed"):
        duration_module.load_report(report)
