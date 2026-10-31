"""The sandbox toolset manifest is installed, verified and described as one.

``packages/core/ai/runtime/sandbox_toolset.toml`` is the single source of
truth for the tools the sandbox image ships. These static checks guard the
class, not one tool:

* every entry names an install source and at least one probe (the parser
  rejects anything else, and the mutations below prove it does);
* the Dockerfile installs every entry through the manifest's exported lists
  and installs no system package from anywhere else;
* the release runs every probe inside the built image;
* the Agent-visible summary is generated from the same entries, so it can
  never name a tool the image (and its probes) do not have.
"""

from __future__ import annotations

import io
import re
import sys
from pathlib import Path

import pytest

from packages.core.ai.runtime import sandbox_toolset
from packages.core.ai.runtime.sandbox_toolset import (
    MANIFEST_PATH,
    ToolsetError,
    export_lists,
    load_manifest,
    parse_manifest,
    render_summary,
    sandbox_toolset_summary,
    verify,
)

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = ROOT / "docker" / "Dockerfile.sandbox"
VERIFY_SCRIPT = ROOT / "scripts" / "sandbox_toolset_verify.sh"
MANIFEST_REL = MANIFEST_PATH.relative_to(ROOT).as_posix()
MODULE_REL = Path(sandbox_toolset.__file__).resolve().relative_to(ROOT).as_posix()


def _final_stage(source: str) -> str:
    return source[source.rindex("\nFROM "):]


# ── the manifest itself ──────────────────────────────────────────────────


def test_manifest_parses_and_every_entry_is_installed_and_probed() -> None:
    toolset = load_manifest()

    assert len(toolset.entries) >= 30
    for entry in toolset.entries:
        assert entry.apt or entry.pip or entry.npm or entry.dockerfile, entry.id
        assert entry.probe or entry.imports, entry.id
    used = {entry.category for entry in toolset.entries}
    assert used == set(toolset.categories), "every category lists at least one tool"


def test_manifest_covers_the_verified_gaps_and_keeps_cjk() -> None:
    toolset = load_manifest()
    apt, pip = set(toolset.apt_packages()), " ".join(toolset.pip_requirements())

    # HEIC/HEIF: two decoders plus a Python opener.
    assert {"libheif-examples", "imagemagick"} <= apt
    assert "pillow-heif" in pip
    assert {"fonts-noto-cjk", "tesseract-ocr-chi-sim"} <= apt
    for package in ("pandas", "numpy", "scipy", "matplotlib", "openpyxl", "python-docx", "PyMuPDF"):
        assert package in pip, package


_MINIMAL = '''
[categories]
images = "Images"

[[tool]]
id = "imagemagick"
category = "images"
label = "ImageMagick"
apt = ["imagemagick"]
probe = ["convert -version"]
'''


def _mutate(old: str, new: str) -> str:
    assert old in _MINIMAL
    return _MINIMAL.replace(old, new, 1)


@pytest.mark.parametrize(
    ("text", "message"),
    [
        (_mutate('probe = ["convert -version"]', ""), "needs a probe or an import"),
        (_mutate('apt = ["imagemagick"]', ""), "no install source"),
        (_mutate('apt = ["imagemagick"]', 'pip = ["Wand>=0.6"]'), "pip packages need an import probe"),
        (
            _mutate('apt = ["imagemagick"]\nprobe = ["convert -version"]', 'npm = ["svgo@^3"]\nimport = ["x"]'),
            "npm packages need a command probe",
        ),
        (_mutate('category = "images"', 'category = "photos"'), "unknown category"),
        (_mutate('label = "ImageMagick"', ""), "needs a label"),
        (_mutate('probe = ["convert -version"]', 'probes = ["convert -version"]'), "unknown keys"),
        (_MINIMAL + _MINIMAL[_MINIMAL.index("[[tool]]"):], "duplicate tool id"),
    ],
)
def test_parser_rejects_every_entry_that_is_not_installed_and_probed(text, message) -> None:
    assert parse_manifest(_MINIMAL).entries[0].id == "imagemagick"
    with pytest.raises(ToolsetError, match=message):
        parse_manifest(text)


# ── the Dockerfile installs from the manifest, and only from it ───────────


def test_export_lists_every_package_of_every_entry(tmp_path) -> None:
    toolset = load_manifest()
    export_lists(toolset, tmp_path)

    apt = (tmp_path / sandbox_toolset.APT_LIST).read_text().split()
    pip = (tmp_path / sandbox_toolset.PIP_REQUIREMENTS).read_text().splitlines()
    npm = (tmp_path / sandbox_toolset.NPM_LIST).read_text().split()
    for entry in toolset.entries:
        assert set(entry.apt) <= set(apt), entry.id
        assert set(entry.pip) <= set(pip), entry.id
        assert set(entry.npm) <= set(npm), entry.id
    assert len(apt) == len(set(apt)) and len(pip) == len(set(pip))


def test_dockerfile_exports_the_manifest_in_its_own_stage() -> None:
    source = DOCKERFILE.read_text()

    stage = source[source.index("AS toolset-lists"):source.index("\nFROM ", source.index("AS toolset-lists"))]
    assert f"COPY {MANIFEST_REL} {MODULE_REL} /src/" in stage
    assert "sandbox_toolset.py --manifest /src/sandbox_toolset.toml export --out /out" in stage


def test_dockerfile_installs_every_exported_list() -> None:
    final = _final_stage(DOCKERFILE.read_text())

    for name in (sandbox_toolset.APT_LIST, sandbox_toolset.PIP_REQUIREMENTS, sandbox_toolset.NPM_LIST):
        assert f"COPY --from=toolset-lists /out/{name} /opt/manor/toolset/{name}" in final, name
    assert re.search(
        r"xargs -a /opt/manor/toolset/apt\.txt \\\n\s+apt-get install -y --no-install-recommends", final
    )
    assert "pip install --no-cache-dir -r /opt/manor/toolset/requirements.txt \\\n    && pip check" in final
    assert re.search(
        r"xargs -a /opt/manor/toolset/npm\.txt \\\n\s+npm install --global --prefix /usr/local", final
    )


def test_manifest_is_the_only_source_of_system_packages() -> None:
    final = _final_stage(DOCKERFILE.read_text())

    installs = [line for line in final.splitlines() if "apt-get install" in line]
    assert len(installs) == 1 and "apt-get install -y --no-install-recommends" in installs[0], (
        "Install system packages through sandbox_toolset.toml, not a second apt-get "
        f"install in Dockerfile.sandbox: {installs}"
    )


def test_dockerfile_installed_entries_name_markers_present_in_the_dockerfile() -> None:
    source = DOCKERFILE.read_text()
    entries = [entry for entry in load_manifest().entries if entry.dockerfile]

    assert {entry.id for entry in entries} >= {"python", "node", "hyperframes"}
    for entry in entries:
        for marker in entry.dockerfile:
            assert marker in source, f"{entry.id}: Dockerfile no longer contains {marker!r}"


def test_toolset_layers_cache_independently_of_skill_edits() -> None:
    final = _final_stage(DOCKERFILE.read_text())

    skill_copy = final.index("COPY --from=skill-manifests")
    for name in ("apt.txt", "npm.txt", "requirements.txt"):
        assert final.index(f"/out/{name}") < skill_copy, name
    # The Skill layer resolves its requirements together with the toolset's.
    assert "pip install --no-cache-dir -r /opt/manor/toolset/requirements.txt $requirements" in final
    # The verifier ships in the image, after the heavy layers, with the module
    # it checks the baked built-in Skills with.
    bundle_rel = MODULE_REL.replace("sandbox_toolset.py", "builtin_skill_bundle.py")
    assert f"COPY {MANIFEST_REL} {MODULE_REL} {bundle_rel} /opt/manor/toolset/" in final
    assert final.index(f"COPY {MANIFEST_REL}") > skill_copy


# ── verification ─────────────────────────────────────────────────────────


def test_verify_runs_every_probe_and_import_and_reports_each_failure(monkeypatch) -> None:
    toolset = parse_manifest('''
[categories]
dev = "Dev"

[[tool]]
id = "present"
category = "dev"
label = "present"
apt = ["coreutils"]
probe = ["true"]
import = ["json"]

[[tool]]
id = "missing-cli"
category = "dev"
label = "missing"
apt = ["nope"]
probe = ["definitely-not-a-command-xyz --version"]

[[tool]]
id = "missing-module"
category = "dev"
label = "missing module"
pip = ["nope"]
import = ["definitely_not_a_module_xyz"]
''')
    monkeypatch.setattr(sandbox_toolset, "_run", _fake_pip_check(sandbox_toolset._run))
    out = io.StringIO()

    failures = verify(toolset, stream=out)

    assert [failure.split(":")[0] for failure in failures] == ["missing-cli", "missing-module"]
    assert "ok   present: true" in out.getvalue() and "ok   present: import json" in out.getvalue()
    assert "pip check" in out.getvalue()


def _fake_pip_check(real):
    def run(command):
        if command[1:] == ["-m", "pip", "check"]:
            return True, ""
        return real(command)

    return run


def test_main_exits_non_zero_when_a_probe_fails(tmp_path, monkeypatch) -> None:
    manifest = tmp_path / "toolset.toml"
    manifest.write_text(_MINIMAL.replace("convert -version", "false"))
    monkeypatch.setattr(sandbox_toolset, "_run", _fake_pip_check(sandbox_toolset._run))

    assert sandbox_toolset.main(["--manifest", str(manifest), "verify"]) == 1
    assert sandbox_toolset.main(["--manifest", str(manifest), "check"]) == 0
    manifest.write_text(_MINIMAL.replace('probe = ["convert -version"]', ""))
    assert sandbox_toolset.main(["--manifest", str(manifest), "check"]) == 2




def test_toolset_verify_script_runs_under_the_runner_limits() -> None:
    script = VERIFY_SCRIPT.read_text()
    assert VERIFY_SCRIPT.stat().st_mode & 0o111
    assert "python3 /opt/manor/toolset/sandbox_toolset.py verify" in script
    # Under the runner's limits, as the image's unprivileged user.
    for flag in ("--read-only", "--cap-drop ALL", "--memory 512m", "--network none", "no-new-privileges"):
        assert flag in script, flag
    sys.path.insert(0, str(ROOT / "sandbox-service"))
    from sandbox.docker_backend import SANDBOX_LOCAL_DIR

    assert f"--mount type=volume,dst={SANDBOX_LOCAL_DIR}" in script
    assert "--user" not in script and " -u " not in script


# ── the Agent-visible description ────────────────────────────────────────

# Roughly 4 characters per token: keep the summary near 400 tokens.
SUMMARY_CHAR_BUDGET = 1700


def test_summary_lists_exactly_the_listed_entries_within_budget() -> None:
    toolset = load_manifest()
    summary = sandbox_toolset_summary()

    assert summary == render_summary(toolset)
    for entry in toolset.entries:
        if entry.listed:
            assert entry.label in summary, entry.id
    for entry in toolset.entries:
        if not entry.listed:
            assert f" {entry.label};" not in summary and f": {entry.label}\n" not in summary
    assert len(summary) <= SUMMARY_CHAR_BUDGET, len(summary)


def test_the_manor_cli_is_part_of_the_verified_and_described_toolset() -> None:
    """bash-in-sandbox's manor CLI ships in the image like any other tool:
    the release verifier probes it and the Agent is told it is there."""

    [cli] = [entry for entry in load_manifest().entries if entry.id == "manor-command"]
    assert cli.listed and cli.label in sandbox_toolset_summary()
    assert "manor --help >/dev/null" in cli.probe
    dockerfile = (ROOT / "docker" / "Dockerfile.sandbox").read_text(encoding="utf-8")
    assert all(marker in dockerfile for marker in cli.dockerfile)


def test_summary_never_names_a_tool_the_manifest_dropped() -> None:
    toolset = parse_manifest(_MINIMAL)

    assert "ImageMagick" in render_summary(toolset)
    dropped = parse_manifest(_MINIMAL.replace('label = "ImageMagick"', 'label = "ImageMagick"\nlisted = false'))
    assert "ImageMagick" not in render_summary(dropped)


def test_sandbox_create_and_skill_contract_show_the_generated_summary() -> None:
    from types import SimpleNamespace

    from packages.core.services.skill_service import _sandbox_skill_runtime_contract

    text = _sandbox_skill_runtime_contract(
        skill=SimpleNamespace(name="pdf"), sandbox_id="sbx", skill_info_parts=[],
        expected_workspace_volume=None, skill_instructions="", skill_input="",
    )
    environment = text.split("## Sandbox Environment")[1].split("## Response Language")[0]
    assert sandbox_toolset_summary() in environment

    create_source = (ROOT / "packages/core/ai/tools/sandbox_tools.py").read_text()
    create = create_source[create_source.index("async def _sandbox_create"):]
    create = create[:create.index("\n_SANDBOX_CREATE_SCHEMA")]
    assert "parts.append(sandbox_toolset_summary())" in create
