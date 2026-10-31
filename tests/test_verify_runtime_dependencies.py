"""Final-image dependency parity, independent of Docker or live services."""

import json
import shlex
from pathlib import Path

import pytest

from scripts import verify_runtime_dependencies as verifier


pytestmark = [pytest.mark.unit, pytest.mark.oss_regression]

HASH = " --hash=sha256:" + "a" * 64
REQUIREMENTS = "sample-package==1.2.3" + HASH
INSTALLED = [("sample_package", "1.2.3"), ("manor", "0.1.1"), ("pip", "26.2.1")]


def verify(text=REQUIREMENTS, installed=INSTALLED):
    return verifier.verify_runtime_dependencies(
        text, installed, expected_project_version="0.1.1", expected_pip_version="26.2.1",
        marker_environment={"python_version": "3.12", "python_full_version": "3.12.15", "sys_platform": "linux"},
    )


def test_hashed_export_keeps_target_markers_extras_and_compatible_duplicate_pins():
    text = (
        "# Generated export\n"
        "sample-package[feature]==1.2.3 ; python_version == '3.12' \\\n"
        + HASH + "\n"
        + REQUIREMENTS + "\n"
        + "sample-package==9.0 ; sys_platform == 'win32'" + HASH + "\n"
        + "windows-only==1.0 ; sys_platform == 'win32'" + HASH + "\n"
    )
    result = verify(text, [*INSTALLED, ("Manor", "0.1.1")])
    assert result["status"] == "passed"
    assert result["runtime_dependency_count"] == 1
    assert result["installed_unique_distribution_count"] == 3


@pytest.mark.parametrize("installed, error", [
    (INSTALLED[1:], "missing"),
    ([("sample-package", "2.0"), *INSTALLED[1:]], "version_mismatches"),
    ([*INSTALLED, ("setuptools", "84.0.0")], "unexpected"),
    ([*INSTALLED[:2], ("pip", "26.2.0")], "version_mismatches"),
    ([INSTALLED[0], ("manor", "0.1.2"), INSTALLED[2]], "version_mismatches"),
    ([*INSTALLED, ("sample-package", "9.0")], "conflicting installed versions"),
])
def test_installed_environment_drift_fails_closed(installed, error):
    with pytest.raises(ValueError, match=error):
        verify(installed=installed)


@pytest.mark.parametrize("text", [
    "sample-package>=1.2.3" + HASH,
    "sample-package==1.*" + HASH,
    "sample-package==1.2.3",
    "sample-package==1.2.3 --hash=sha256:not-a-hash",
    "sample-package @ https://example.invalid/sample.whl" + HASH,
    REQUIREMENTS + "\nsample-package==2.0" + HASH,
    REQUIREMENTS + "\n--index-url https://example.invalid/simple" + HASH,
    REQUIREMENTS + "\nunfinished==1.0 \\",
    REQUIREMENTS + "\npip==26.2.1" + HASH,
])
def test_invalid_or_non_locked_export_is_rejected(text):
    with pytest.raises(ValueError):
        verify(text)


def test_changed_export_pin_does_not_accept_the_old_installed_version():
    with pytest.raises(ValueError, match="version_mismatches"):
        verify("sample-package==1.2.4" + HASH)


def test_cli_uses_project_metadata_and_returns_nonzero_for_unexpected_packages(tmp_path, monkeypatch, capsys):
    project = tmp_path / "pyproject.toml"
    project.write_text('[project]\nname = "manor"\nversion = "0.1.1"\n')
    requirements = tmp_path / "runtime-requirements.txt"
    requirements.write_text(REQUIREMENTS)
    arguments = ["--requirements", str(requirements), "--project", str(project),
                 "--expected-pip-version", "26.2.1"]
    monkeypatch.setattr(verifier, "installed_distribution_versions", lambda: INSTALLED)
    assert verifier.main(arguments) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "passed"
    monkeypatch.setattr(verifier, "installed_distribution_versions", lambda: [*INSTALLED, ("wheel", "1.0")])
    assert verifier.main(arguments) == 1
    assert "unexpected" in json.loads(capsys.readouterr().out)["error"]


def test_api_dockerfile_installs_the_hashed_runtime_export_without_reresolving():
    """Docker instructions are the boundary preventing the former second resolver."""
    root = Path(__file__).parents[1]
    dockerfile = (root / "docker/Dockerfile.api").read_text().replace("\\\n", " ")
    instructions = [
        shlex.split(line) for line in dockerfile.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    commands = []
    for index, tokens in enumerate(instructions):
        if tokens[0] != "RUN":
            continue
        command = []
        for token in [*tokens[1:], "&&"]:
            if token == "&&":
                commands.append((index, command))
                command = []
            else:
                command.append(token)

    exports = [command for _, command in commands if command[:2] == ["uv", "export"]]
    assert len(exports) == 1
    assert {"--locked", "--no-dev", "--no-default-groups", "--no-emit-project"} <= set(exports[0])
    assert "--no-hashes" not in exports[0]
    assert any(tokens[0] == "COPY" and "uv.lock" in tokens for tokens in instructions)

    installs = [(index, command) for index, command in commands if command[:4] == ["python", "-m", "pip", "install"]]
    assert installs
    for _, command in installs:
        assert "--no-deps" in command
        assert "." not in command
    runtime_installs = [
        command for _, command in installs
        if "-r" in command and command[command.index("-r") + 1].endswith("/runtime-requirements.txt")
    ]
    assert len(runtime_installs) == 1
    assert {"--require-hashes", "--no-deps", "--only-binary=:all:"} <= set(runtime_installs[0])
    wheels = [command for _, command in commands if command[:4] == ["python", "-m", "pip", "wheel"]]
    assert len(wheels) == 1
    assert {"--no-deps", "--no-build-isolation"} <= set(wheels[0])

    gates = [(index, command) for index, command in commands
             if command[:2] == ["python", "scripts/verify_runtime_dependencies.py"]]
    assert len(gates) == 1
    gate_index, gate = gates[0]
    assert gate_index > max(index for index, _ in installs)
    assert gate[gate.index("--requirements") + 1] == runtime_installs[0][runtime_installs[0].index("-r") + 1]
    assert "--project" in gate
    build_pins = verifier.runtime_pins((root / "docker/python-build-requirements.txt").read_text())
    assert gate[gate.index("--expected-pip-version") + 1] == str(build_pins["pip"])
