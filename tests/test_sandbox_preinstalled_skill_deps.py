"""Shipped Skills' dependencies come preinstalled in the sandbox image.

The image keys each manifest by a digest of its bytes; the sandbox service
computes the same digest and skips the create-time install on a match. These
tests pin both halves of that contract and that every shipped Skill manifest
is collected by the image build.
"""
import hashlib
import re
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = ROOT / "docker" / "Dockerfile.sandbox"
SANDBOX_SERVICE = ROOT / "sandbox-service"
sys.path.insert(0, str(SANDBOX_SERVICE))

# See test_sandbox_write_base64: a sys.path insert cannot dislodge a name
# already bound in sys.modules, and ``config`` is claimed by another test.
sys.modules.pop("config", None)

docker_backend = pytest.importorskip("sandbox.docker_backend")
models = pytest.importorskip("sandbox.models")

MANIFEST_NAMES = ("requirements.txt", "package.json")


def _shipped_skill_manifests() -> list[Path]:
    """Every dependency manifest at the root of a shipped Skill directory."""
    oss_manifest = ROOT / "OSS_MANIFEST.txt"
    if oss_manifest.is_file():
        tracked = [path for path in oss_manifest.read_text(encoding="utf-8").splitlines() if path.endswith("SKILL.md")]
    else:
        tracked = subprocess.run(
            ["git", "ls-files", "--", "*SKILL.md"],
            cwd=ROOT, capture_output=True, text=True, check=True,
        ).stdout.split()
    manifests = []
    for skill_md in (ROOT / path for path in tracked):
        for name in MANIFEST_NAMES:
            manifest = skill_md.parent / name
            if manifest.is_file():
                manifests.append(manifest)
    return sorted(manifests)


def _collected_by_image(manifest: Path) -> bool:
    """Mirror the skill-manifests stage: COPY packages/core/ai/ then */*/<name>."""
    source = DOCKERFILE.read_text()
    copy = re.search(r"COPY (\S+)/ /src/ai/", source)
    assert copy, "skill-manifests stage no longer copies the Skill roots"
    relative = manifest.relative_to(ROOT / copy.group(1))
    return len(relative.parts) == 3 and f"/src/ai/*/*/{manifest.name}" in source


def test_shipped_skill_manifests_use_oss_manifest_without_git(tmp_path, monkeypatch) -> None:
    skill = tmp_path / "packages/core/ai/skills/pdf"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# PDF\n", encoding="utf-8")
    requirements = skill / "requirements.txt"
    package = skill / "package.json"
    requirements.write_text("pypdf\n", encoding="utf-8")
    package.write_text("{}\n", encoding="utf-8")
    (tmp_path / "OSS_MANIFEST.txt").write_text(
        "packages/core/ai/skills/pdf/SKILL.md\n"
        "packages/core/ai/skills/pdf/requirements.txt\n"
        "packages/core/ai/skills/pdf/package.json\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(sys.modules[__name__], "ROOT", tmp_path)

    assert _shipped_skill_manifests() == [package, requirements]


def test_every_shipped_skill_manifest_is_preinstalled_by_the_image() -> None:
    manifests = _shipped_skill_manifests()
    assert manifests, "expected shipped Skills with dependency manifests"
    missing = []
    for manifest in manifests:
        try:
            collected = _collected_by_image(manifest)
        except ValueError:
            collected = False
        if not collected:
            missing.append(str(manifest.relative_to(ROOT)))
    assert not missing, (
        "These Skill manifests are not collected by docker/Dockerfile.sandbox, so "
        f"every sandbox for them installs at create time: {missing}"
    )


def test_image_and_service_key_manifests_the_same_way() -> None:
    source = DOCKERFILE.read_text()
    root = docker_backend.PREINSTALLED_SKILL_DEPS_ROOT

    assert f"COPY --from=skill-manifests /out {root}" in source
    for kind, layout in (("python", '"/out/python/${digest}.txt"'), ("node", '"/out/node/${digest}/package.json"')):
        assert layout in source, f"{kind} manifests left the layout the service reads"
    digests = re.findall(r'digest="\$\(sha256sum "\$manifest" \| cut -c1-(\d+)\)"', source)
    assert digests and set(digests) == {"16"}

    content = b"python-pptx>=1.0.2\n"
    assert docker_backend.preinstalled_manifest_digest(content) == hashlib.sha256(content).hexdigest()[:16]


def test_image_installs_python_manifests_together_and_checks_them() -> None:
    source = DOCKERFILE.read_text()

    assert "/opt/manor/skill-deps/python/*.txt" in source
    # One pip run resolves the Skills' requirements with the toolset's.
    assert "pip install --no-cache-dir -r /opt/manor/toolset/requirements.txt $requirements" in source
    assert "pip check" in source
    assert "/opt/manor/skill-deps/node/*/" in source
    # Installed before the image drops to the unprivileged sandbox user.
    assert source.index("COPY --from=skill-manifests") < source.index("USER sandbox")


def _sandbox(tmp_path: Path, files: dict[str, str]):
    for name, content in files.items():
        (tmp_path / name).write_text(content)
    sandbox = docker_backend.DockerSandbox("sandbox-test", models.ContainerConfig())
    skill = models.SkillManifest(
        name="pdf",
        skill_dir=str(tmp_path),
        requirements_txt="requirements.txt" if "requirements.txt" in files else None,
        package_json="package.json" if "package.json" in files else None,
    )
    return sandbox, skill


def _exec_returning(codes: dict[str, int]):
    def result(command: str, **_kwargs):
        code = next((value for marker, value in codes.items() if marker in command), 0)
        return models.ExecResponse(stdout="", stderr="", exit_code=code)

    return AsyncMock(side_effect=result)


@pytest.mark.asyncio
async def test_preinstalled_manifests_skip_the_create_time_install(tmp_path, monkeypatch):
    requirements = "pandas>=2.2\n"
    package = '{"dependencies": {"remotion": "4.0.506"}}\n'
    sandbox, skill = _sandbox(tmp_path, {"requirements.txt": requirements, "package.json": package})
    exec_mock = _exec_returning({})
    monkeypatch.setattr(sandbox, "_run_in_container", exec_mock)

    result = await sandbox._install_dependencies(skill)

    assert result.exit_code == 0
    commands = [call.args[0] for call in exec_mock.await_args_list]
    python_digest = docker_backend.preinstalled_manifest_digest(requirements.encode())
    node_digest = docker_backend.preinstalled_manifest_digest(package.encode())
    assert any(f"/python/{python_digest}.txt" in command for command in commands)
    assert any(f"/node/{node_digest}/node_modules" in command for command in commands)
    assert not any("pip install" in command or "npm install" in command for command in commands)


@pytest.mark.asyncio
async def test_unknown_manifests_still_install(tmp_path, monkeypatch):
    sandbox, skill = _sandbox(
        tmp_path,
        {"requirements.txt": "left-pad-py==1.0\n", "package.json": '{"dependencies": {"x": "1"}}\n'},
    )
    exec_mock = _exec_returning({"test -f": 1, "exit 64": 64})
    monkeypatch.setattr(sandbox, "_run_in_container", exec_mock)

    await sandbox._install_dependencies(skill)

    commands = [call.args[0] for call in exec_mock.await_args_list]
    assert any(command.startswith("pip install") for command in commands)
    assert any(command.startswith("npm install") for command in commands)


@pytest.mark.asyncio
async def test_linking_keeps_entries_the_skill_installed_itself(tmp_path, monkeypatch):
    sandbox, skill = _sandbox(tmp_path, {"package.json": '{"dependencies": {"docx": "9.7.1"}}\n'})
    exec_mock = _exec_returning({})
    monkeypatch.setattr(sandbox, "_run_in_container", exec_mock)

    assert await sandbox._link_preinstalled_node_modules(skill)

    script = exec_mock.await_args.args[0]
    # Each Skill links its own node_modules in its own /skills/<name>.
    assert "dst=/skills/pdf/node_modules" in script
    # Never replace something already in node_modules.
    assert 'link() { [ -e "$2" ] || [ -L "$2" ] || ln -s "$1" "$2"; }' in script
    # Per-package links inside real directories, never a link over
    # node_modules, a scope or .bin (npm renames entries inside them).
    assert 'mkdir -p "$dst" ' in script
    assert '@*|.bin) mkdir -p "$dst/$name"' in script


@pytest.mark.asyncio
async def test_linking_runs_in_a_real_shell(tmp_path, monkeypatch):
    """Execute the link script against a fake image tree."""
    import subprocess as sp

    image = tmp_path / "image" / "node_modules"
    for package in ("react", "@remotion/cli", "@remotion/captions"):
        (image / package).mkdir(parents=True)
    (image / ".bin").mkdir()
    (image / ".bin" / "remotion").symlink_to("../@remotion/cli")
    workdir = tmp_path / "skill"
    (workdir / "node_modules" / "react").mkdir(parents=True)  # installed by the Skill itself

    sandbox, skill = _sandbox(tmp_path, {"package.json": "{}\n"})
    captured = AsyncMock(return_value=models.ExecResponse(stdout="", stderr="", exit_code=0))
    monkeypatch.setattr(sandbox, "_run_in_container", captured)
    await sandbox._link_preinstalled_node_modules(skill)
    script = captured.await_args.args[0]
    script = script.replace(
        f"{docker_backend.PREINSTALLED_SKILL_DEPS_ROOT}/node/", f"{tmp_path}/digest-",
    ).replace("/skills/pdf/node_modules", str(workdir / "node_modules"))
    digest = docker_backend.preinstalled_manifest_digest(b"{}\n")
    (tmp_path / f"digest-{digest}").mkdir()
    (tmp_path / f"digest-{digest}" / "node_modules").symlink_to(image)

    assert sp.run(["sh", "-c", script]).returncode == 0
    linked = workdir / "node_modules"
    assert not (linked / "react").is_symlink()
    assert (linked / "@remotion").is_dir() and not (linked / "@remotion").is_symlink()
    assert (linked / "@remotion" / "cli").is_symlink()
    assert (linked / ".bin").is_dir() and (linked / ".bin" / "remotion").is_symlink()

    assert sp.run(["sh", "-c", script.replace(f"{tmp_path}/digest-", f"{tmp_path}/missing-")]).returncode == 64
