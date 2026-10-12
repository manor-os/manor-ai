import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


pytestmark = pytest.mark.oss_smoke


@pytest.fixture
def runner_module():
    path = Path(__file__).parents[1] / "scripts" / "run_python_tests.py"
    spec = importlib.util.spec_from_file_location("run_python_tests", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_regression_lane_keeps_explicit_test_target_and_forwards_sharding(runner_module):
    arguments = runner_module.pytest_arguments(
        runner_module.LANE_MARKERS["regression"],
        ["tests/test_run_python_tests.py", "--splits", "8", "--group", "2"],
        None,
    )

    assert arguments[:5] == ["tests/test_run_python_tests.py", "--splits", "8", "--group", "2"]
    assert "tests/" not in arguments
    assert "--maxfail=1" in arguments
    assert arguments[arguments.index("-m") + 1] == runner_module.LANE_MARKERS["regression"]


@pytest.mark.parametrize(
    "pytest_args",
    [
        ["--ignore", "tests/test_ignored.py"],
        ["--ignore=tests/test_ignored.py"],
        ["--deselect", "tests/test_ignored.py::test_case"],
        ["--confcutdir", "tests"],
    ],
)
def test_path_valued_pytest_options_do_not_replace_default_test_target(runner_module, pytest_args):
    arguments = runner_module.pytest_arguments(runner_module.LANE_MARKERS["smoke"], pytest_args, None)

    assert arguments[0] == "tests/"


def test_runner_reports_only_pytest_final_marker_selection(runner_module, tmp_path):
    (tmp_path / "pytest.ini").write_text(
        "[pytest]\nmarkers =\n    oss_smoke: selected smoke test\n    cloud: deselected cloud test\n",
        encoding="utf-8",
    )
    test_file = tmp_path / "test_selection.py"
    test_file.write_text(
        """import pytest


@pytest.mark.oss_smoke
def test_selected():
    pass


@pytest.mark.cloud
def test_deselected():
    pass
""",
        encoding="utf-8",
    )
    report_dir = tmp_path / "report"
    result = subprocess.run(
        [
            sys.executable,
            str(Path(runner_module.__file__)),
            "smoke",
            "--report-dir",
            str(report_dir),
            str(test_file),
        ],
        cwd=Path(runner_module.__file__).parents[1],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert (report_dir / "selected-nodeids.txt").read_text().endswith("test_selection.py::test_selected\n")
    summary = json.loads((report_dir / "summary.json").read_text())
    assert summary["selected_count"] == 1
    assert summary["selection"] == {
        "collected_count": 2,
        "deselected_by_marker": {"cloud": 1},
        "deselected_count": 1,
        "marker_deselection_attributable": True,
        "selected_count": 1,
    }
    assert summary["markers"] == "oss_smoke"
    assert summary["phases"]["call"]["outcomes"] == {"passed": 1}
    assert summary["slowest"][0]["nodeid"].endswith("test_selection.py::test_selected")
    durations = json.loads((report_dir / "node-durations.json").read_text())
    nodeid = next(iter(durations["node_durations"]))
    assert nodeid.endswith("test_selection.py::test_selected")
    assert durations["schema_version"] == 1
    assert set(durations["node_durations"][nodeid]) == {"setup", "call", "teardown", "total"}
    assert durations["node_durations"][nodeid]["total"] > 0


@pytest.mark.parametrize("conflicting_marker", ["manual", "network", "docker"])
def test_runner_rejects_smoke_tests_excluded_from_full_regression(runner_module, tmp_path, conflicting_marker):
    (tmp_path / "pytest.ini").write_text(
        "[pytest]\nmarkers =\n"
        "    oss_smoke: smoke test\n"
        "    manual: manual prerequisite\n"
        "    network: network prerequisite\n"
        "    docker: docker prerequisite\n",
        encoding="utf-8",
    )
    test_file = tmp_path / "test_invalid_smoke.py"
    test_file.write_text(
        f"import pytest\n\n\n@pytest.mark.oss_smoke\n@pytest.mark.{conflicting_marker}\ndef test_invalid():\n    pass\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        [sys.executable, str(Path(runner_module.__file__)), "regression", str(test_file)],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "oss_smoke tests must not also require" in result.stderr
    assert conflicting_marker in result.stderr


def test_runner_allows_smoke_membership_in_cloud_and_media_lanes(runner_module, tmp_path):
    (tmp_path / "pytest.ini").write_text(
        "[pytest]\nmarkers =\n"
        "    oss_smoke: smoke test\n"
        "    cloud: cloud lane\n"
        "    media_integration: media lane\n",
        encoding="utf-8",
    )
    test_file = tmp_path / "test_split_smoke.py"
    test_file.write_text(
        "import pytest\n\n\n"
        "@pytest.mark.oss_smoke\n@pytest.mark.cloud\ndef test_cloud():\n    pass\n\n"
        "@pytest.mark.oss_smoke\n@pytest.mark.media_integration\ndef test_media():\n    pass\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        [sys.executable, str(Path(runner_module.__file__)), "smoke", str(test_file)],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "2 passed" in result.stdout
    assert "oss_smoke tests must not also require" not in result.stderr


@pytest.mark.parametrize(
    ("media_body", "ordinary_body", "fail_on_skip", "expected_exit", "expected_runtime_skips"),
    [
        pytest.param(
            "pytest.skip('media prerequisite unavailable')",
            "pass",
            "--fail-on-skip",
            1,
            {"test_media_skip.py::test_missing_media_prerequisite": "skipped"},
            id="all-selected-skips-fail",
        ),
        pytest.param(
            "pytest.skip('media prerequisite unavailable')",
            "pass",
            "--fail-on-skip=media_integration",
            1,
            {"test_media_skip.py::test_missing_media_prerequisite": "skipped"},
            id="marker-mode-media-skip-fails",
        ),
        pytest.param(
            "pass",
            "pytest.skip('ordinary platform alternative')",
            "--fail-on-skip=media_integration",
            0,
            {"test_media_skip.py::test_ordinary_platform_alternative": "skipped"},
            id="marker-mode-allows-unmarked-skip",
        ),
    ],
)
def test_fail_on_skip_filters_runtime_skips_by_marker(
    runner_module, tmp_path, media_body, ordinary_body, fail_on_skip, expected_exit, expected_runtime_skips
):
    (tmp_path / "pytest.ini").write_text(
        "[pytest]\nmarkers =\n    media_integration: media prerequisite\n",
        encoding="utf-8",
    )
    test_file = tmp_path / "test_media_skip.py"
    test_file.write_text(
        "import pytest\n\n\n"
        "@pytest.mark.media_integration\ndef test_missing_media_prerequisite():\n"
        f"    {media_body}\n\n"
        "def test_ordinary_platform_alternative():\n"
        f"    {ordinary_body}\n",
        encoding="utf-8",
    )
    report_dir = tmp_path / "report"

    result = subprocess.run(
        [
            sys.executable,
            str(Path(runner_module.__file__)),
            "regression",
            "--markers",
            "media_integration or not media_integration",
            fail_on_skip,
            "--report-dir",
            str(report_dir),
            str(test_file),
        ],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == expected_exit
    summary = json.loads((report_dir / "summary.json").read_text())
    assert summary["exit_code"] == expected_exit
    expected_fail_on_skip = "*" if fail_on_skip == "--fail-on-skip" else fail_on_skip.removeprefix("--fail-on-skip=")
    assert summary["fail_on_skip"] == expected_fail_on_skip
    assert summary["collection"] == {"skipped_count": 0, "skipped_nodeids": []}
    assert summary["runtime_skips"]["count"] == len(expected_runtime_skips)
    assert summary["skip_enforcement"] == {
        "collection_skipped_count": 0,
        "runtime_skipped_count": expected_exit,
    }
    for nodeid, outcome in expected_runtime_skips.items():
        assert summary["phases"]["call"]["outcomes"][outcome] >= 1
        assert nodeid in summary["runtime_skips"]["nodeids"]


@pytest.mark.parametrize("include_selected_media_test", [True, False])
def test_fail_on_skip_rejects_collection_skips_even_in_marker_mode(
    runner_module, tmp_path, include_selected_media_test
):
    (tmp_path / "pytest.ini").write_text(
        "[pytest]\nmarkers =\n    media_integration: media prerequisite\n",
        encoding="utf-8",
    )
    if include_selected_media_test:
        (tmp_path / "test_media_pass.py").write_text(
            "import pytest\n\n\n@pytest.mark.media_integration\ndef test_media_passes():\n    pass\n",
            encoding="utf-8",
        )
    (tmp_path / "test_collection_skip.py").write_text(
        "import pytest\n\n\npytest.skip('optional module unavailable', allow_module_level=True)\n",
        encoding="utf-8",
    )
    report_dir = tmp_path / "report"

    result = subprocess.run(
        [
            sys.executable,
            str(Path(runner_module.__file__)),
            "regression",
            "--markers",
            "media_integration",
            "--fail-on-skip=media_integration",
            "--report-dir",
            str(report_dir),
            str(tmp_path),
        ],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "collection target(s) skipped with --fail-on-skip" in result.stderr
    summary = json.loads((report_dir / "summary.json").read_text())
    assert summary["exit_code"] == 1
    assert summary["collection"]["skipped_count"] == 1
    assert summary["collection"]["skipped_nodeids"] == ["test_collection_skip.py"]
    assert summary["skip_enforcement"] == {
        "collection_skipped_count": 1,
        "runtime_skipped_count": 0,
    }


def test_fail_on_skip_rejects_unknown_marker_value(runner_module):
    with pytest.raises(SystemExit, match="2"):
        runner_module.main(["regression", "--fail-on-skip=media_typo"])


def test_runner_keeps_a_relative_non_root_test_directory_as_the_explicit_target(runner_module, tmp_path):
    (tmp_path / "pytest.ini").write_text(
        "[pytest]\nmarkers =\n    oss_smoke: selected smoke test\n",
        encoding="utf-8",
    )
    suite = tmp_path / "example_suite"
    suite.mkdir()
    (suite / "test_selection.py").write_text(
        "import pytest\n\n\n@pytest.mark.oss_smoke\ndef test_selected():\n    pass\n",
        encoding="utf-8",
    )
    report_dir = tmp_path / "report"

    result = subprocess.run(
        [
            sys.executable,
            str(Path(runner_module.__file__)),
            "smoke",
            "--report-dir",
            str(report_dir),
            "--",
            "example_suite",
        ],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    summary = json.loads((report_dir / "summary.json").read_text())
    assert summary["pytest_args"][-2:] == ["--", "example_suite"]
    assert "tests/" not in summary["pytest_args"]


def test_runner_treats_pytest_marker_option_as_a_lane_override(runner_module, monkeypatch):
    received: list[list[str]] = []

    def fake_main(arguments, plugins):
        received.append(arguments)
        plugins[0].pytest_collection_finish(SimpleNamespace(items=[]))
        return 0

    monkeypatch.setitem(sys.modules, "pytest", SimpleNamespace(main=fake_main))

    assert runner_module.main(["smoke", "-m", "unit"]) == 0
    assert received[0][received[0].index("-m") + 1] == "unit"


def test_runner_records_explicit_marker_prerequisite_deselection(runner_module, tmp_path):
    (tmp_path / "pytest.ini").write_text(
        "[pytest]\nmarkers =\n    oss_smoke: selected smoke test\n    manual: opt-in test\n",
        encoding="utf-8",
    )
    test_file = tmp_path / "test_manual.py"
    test_file.write_text(
        "import pytest\n\n\n@pytest.mark.manual\ndef test_manual_only():\n    pass\n",
        encoding="utf-8",
    )
    report_dir = tmp_path / "report"

    result = subprocess.run(
        [sys.executable, str(Path(runner_module.__file__)), "smoke", "--report-dir", str(report_dir), str(test_file)],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 5
    summary = json.loads((report_dir / "summary.json").read_text())
    assert summary["selection"]["deselected_by_marker"] == {"manual": 1}
    assert summary["selection"]["marker_deselection_attributable"] is True
    assert (report_dir / "deselected-nodeids.txt").read_text().endswith("test_manual.py::test_manual_only\tmanual\n")


def test_report_dir_records_the_selected_lane_from_the_pytest_invocation(runner_module, monkeypatch, tmp_path):
    received: list[list[str]] = []

    def fake_main(arguments, plugins):
        received.append(arguments)
        plugins[0].pytest_collection_finish(
            SimpleNamespace(items=[SimpleNamespace(nodeid="tests/test_example.py::test_selected")])
        )
        return 0

    monkeypatch.setitem(sys.modules, "pytest", SimpleNamespace(main=fake_main))
    monkeypatch.setenv("DEPLOYMENT_MODE", "cloud")
    monkeypatch.setenv("TEST_DATABASE_URL", "postgresql://user:secret@db.example:5432/manor_test")
    step_summary = tmp_path / "github-step-summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(step_summary))

    marker_override = "oss_smoke or oss_regression"
    assert (
        runner_module.main(["smoke", "--markers", marker_override, "--report-dir", str(tmp_path), "--maxfail=5"]) == 0
    )

    assert received[0][0] == "tests/"
    assert "--maxfail=1" not in received[0]
    assert received[0][received[0].index("-m") + 1] == marker_override
    assert (tmp_path / "junit.xml").as_posix() in received[0]
    assert (tmp_path / "selected-nodeids.txt").read_text() == "tests/test_example.py::test_selected\n"
    durations = json.loads((tmp_path / "node-durations.json").read_text())
    assert durations == {
        "schema_version": 1,
        "node_durations": {
            "tests/test_example.py::test_selected": {
                "setup": 0,
                "call": 0,
                "teardown": 0,
                "total": 0,
            }
        },
        "node_outcomes": {"tests/test_example.py::test_selected": {}},
    }
    environment = json.loads((tmp_path / "environment.json").read_text())
    assert environment["deployment_mode"] == "cloud"
    assert environment["services"]["database"] == {"configured": True, "host": "db.example", "port": 5432}
    assert "secret" not in (tmp_path / "environment.json").read_text()
    summary = json.loads((tmp_path / "summary.json").read_text())
    assert summary["lane"] == "smoke"
    assert summary["markers"] == marker_override
    assert summary["selected_count"] == 1
    assert summary["exit_code"] == 0
    assert step_summary.read_text().startswith("### Python smoke lane\n\n- Selected tests: 1\n- Exit code: 0\n")


def test_report_advises_duration_weight_coverage_for_shards(runner_module, monkeypatch, tmp_path):
    def fake_main(arguments, plugins):
        plugins[0].pytest_collection_finish(
            SimpleNamespace(
                items=[
                    SimpleNamespace(nodeid="tests/test_example.py::test_measured"),
                    SimpleNamespace(nodeid="tests/test_example.py::test_new"),
                ]
            )
        )
        return 0

    duration_path = tmp_path / "durations.json"
    duration_path.write_text(json.dumps({"tests/test_example.py::test_measured": 1.2}))
    step_summary = tmp_path / "github-step-summary.md"
    monkeypatch.setitem(sys.modules, "pytest", SimpleNamespace(main=fake_main))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(step_summary))

    assert runner_module.main(
        [
            "regression",
            "--report-dir",
            str(tmp_path),
            "--splits",
            "2",
            "--group",
            "1",
            "--durations-path",
            str(duration_path),
        ]
    ) == 0

    summary = json.loads((tmp_path / "summary.json").read_text())
    assert summary["duration_weights"] == {
        "path": str(duration_path),
        "exact_count": 1,
        "estimated_count": 1,
    }
    assert "Duration weights: exact=1, estimated=1" in step_summary.read_text()


def test_report_preserves_empty_selection_exit_status(runner_module, monkeypatch, tmp_path):
    def fake_main(arguments, plugins):
        plugins[0].pytest_collection_finish(SimpleNamespace(items=[]))
        return 5

    monkeypatch.setitem(sys.modules, "pytest", SimpleNamespace(main=fake_main))
    (tmp_path / "deselected-nodeids.txt").write_text("stale\n", encoding="utf-8")

    assert runner_module.main(["smoke", "--report-dir", str(tmp_path)]) == 5
    assert json.loads((tmp_path / "summary.json").read_text())["selected_count"] == 0
    assert json.loads((tmp_path / "summary.json").read_text())["exit_code"] == 5
    assert not (tmp_path / "deselected-nodeids.txt").exists()


def test_report_dir_rejects_a_second_junit_destination(runner_module, tmp_path):
    with pytest.raises(SystemExit, match="2"):
        runner_module.main(["smoke", "--report-dir", str(tmp_path), "--junitxml", "other.xml"])
