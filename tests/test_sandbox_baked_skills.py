"""Built-in Skills ship in the sandbox image; a sandbox links them instead of uploading.

Contract pinned here:

1. The image bakes exactly the loader's built-in set, with the digest the
   control plane computes (one module: builtin_skill_bundle).
2. The control plane sends ``baked_skill`` only on an exact digest match the
   runner advertises; anything else (old runner, other version, a runner that
   refuses) uploads the files as before.
3. Workspace / custom / edited Skills never take the baked path.
4. The runner materializes the image's copy in one function, real writable
   files except declared read-only subtrees, and never quarantines a sandbox
   for a digest it does not hold.
5. The release verifier checks the baked Skills.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from packages.core.ai.runtime import builtin_skill_bundle, sandbox_toolset
from packages.core.services import builtin_skill_loader, sandbox_baked_skills
from packages.core.services.sandbox_baked_skills import (
    BAKED_SKILL_UNAVAILABLE,
    BakedSkillRef,
    create_builtin_sandbox,
    load_builtin_skill,
)
from tests.sandbox_runner_fakes import (
    API_TOKEN,
    FakeDocker,
    load_service_main,
    register_sandbox,
)

import sandbox.baked_skills as runner_baked  # noqa: E402
import sandbox.docker_backend as docker_backend  # noqa: E402
import sandbox.skill_runner as skill_runner_module  # noqa: E402
from sandbox.errors import BakedSkillUnavailable  # noqa: E402
from sandbox.models import ContainerConfig, SandboxStatus, SkillManifest  # noqa: E402
from sandbox.scanner import SkillScanner  # noqa: E402
from sandbox.skill_runner import SkillRunner  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = ROOT / "docker" / "Dockerfile.sandbox"
BUNDLE_MODULE = Path(builtin_skill_bundle.__file__).resolve()
DIGEST = "a" * 64
OTHER = "b" * 64


def _skill(root: Path, name: str, files: dict[str, str], config: dict | None = None) -> Path:
    skill_dir = root / name
    for rel, content in {"SKILL.md": f"---\nname: {name}\n---\nbody", **files}.items():
        path = skill_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    if config is not None:
        (skill_dir / "config.json").write_text(json.dumps(config), encoding="utf-8")
    return skill_dir


@pytest.fixture
def fake_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    skills = repo / builtin_skill_bundle.SKILLS_SUBDIR
    _skill(skills, "alpha", {"scripts/run.py": "print(1)\n", "requirements.txt": "six\n"})
    _skill(
        skills,
        "deck",
        {"scripts/make.py": "print(2)\n", "templates/icons/a.svg": "<svg/>", "templates/layouts/i.json": "{}"},
        {"sandbox_read_only_paths": ["templates/icons"]},
    )
    _skill(skills, "remotion", {"run.py": "retired"})  # retired slug: never baked
    agents = repo / builtin_skill_bundle.AGENT_SKILLS_SUBDIR
    _skill(agents, "intro", {})
    _skill(agents, "manor-release", {})  # repo maintenance: never exposed
    return repo


# ── 1. one source of truth: set, digest, layout ──────────────────────────


def test_bake_holds_exactly_the_loaders_builtin_set(fake_repo: Path, tmp_path: Path) -> None:
    skills_root = fake_repo / builtin_skill_bundle.SKILLS_SUBDIR
    agents_root = fake_repo / builtin_skill_bundle.AGENT_SKILLS_SUBDIR
    out = tmp_path / "out"

    index = builtin_skill_bundle.bake(out, skills_root=skills_root, agent_skills_root=agents_root)
    loader_set = builtin_skill_loader._builtin_skill_dirs(skills_root=skills_root, agent_skills_root=agents_root)

    assert set(index["skills"]) == {path.name for path in loader_set} == {"alpha", "deck", "intro"}
    for skill_dir in loader_set:
        name = skill_dir.name
        # The control plane's digest of the source equals the image's record
        # and a re-digest of the baked copy.
        assert index["skills"][name]["digest"] == builtin_skill_loader._skill_source_sha256(skill_dir)
        assert (out / ".digests" / name).read_text() == index["skills"][name]["digest"]
        assert builtin_skill_bundle.skill_source_sha256(out / name) == index["skills"][name]["digest"]
    assert builtin_skill_bundle.verify_baked(out) == []


def test_loader_selection_and_digest_are_the_bundle_functions() -> None:
    """The loader may not re-implement either: the image would bake another set
    or digest, and every sandbox would silently fall back to uploading."""
    assert builtin_skill_loader._builtin_skill_dirs() == builtin_skill_bundle.builtin_skill_dirs(
        skills_root=builtin_skill_loader._SKILLS_ROOT,
        agent_skills_root=builtin_skill_loader._AGENT_SKILLS_ROOT,
    )
    assert builtin_skill_loader._RETIRED_BUILTIN_SKILL_SLUGS is builtin_skill_bundle.RETIRED_BUILTIN_SKILL_SLUGS
    pdf = builtin_skill_loader._SKILLS_ROOT / "pdf"
    assert builtin_skill_loader._skill_source_sha256(pdf) == builtin_skill_bundle.skill_source_sha256(pdf)
    source = Path(builtin_skill_loader.__file__).read_text()
    assert "hashlib" not in source, "the loader digests Skills only through builtin_skill_bundle"


def _baked_stage() -> str:
    source = DOCKERFILE.read_text()
    match = re.search(r"^FROM \S+ AS baked-skills\n(.*?)(?=^FROM )", source, re.M | re.S)
    assert match, "docker/Dockerfile.sandbox lost its baked-skills stage"
    return match.group(1)


def test_image_bakes_with_the_bundle_module_from_the_loader_roots() -> None:
    stage = _baked_stage()
    rel = BUNDLE_MODULE.relative_to(ROOT).as_posix()
    assert f"COPY {rel} /src/builtin_skill_bundle.py" in stage
    # The stage's repo root mirrors the paths the module reads under it.
    assert f"COPY {builtin_skill_bundle.SKILLS_SUBDIR.as_posix()}/ /src/{builtin_skill_bundle.SKILLS_SUBDIR.as_posix()}/" in stage
    assert (
        f"COPY {builtin_skill_bundle.AGENT_SKILLS_SUBDIR.as_posix()}/ "
        f"/src/{builtin_skill_bundle.AGENT_SKILLS_SUBDIR.as_posix()}/"
    ) in stage
    assert "builtin_skill_bundle.py bake --repo-root /src --out /out" in stage
    assert "builtin_skill_bundle.py verify --root /out" in stage
    source = DOCKERFILE.read_text()
    assert f"COPY --from=baked-skills /out {builtin_skill_bundle.BAKED_SKILLS_ROOT}" in source
    # Late: after the heavy dependency layers, so a Skill edit rebuilds one layer.
    final = source[source.rindex("\nFROM "):]
    assert final.index("COPY --from=baked-skills") > final.index("COPY --from=skill-manifests")
    assert final.index("COPY --from=baked-skills") > final.index("npm install --no-fund")


def test_runner_and_bundle_agree_on_the_baked_layout() -> None:
    for name in ("BAKED_SKILLS_ROOT", "BAKED_INDEX_FILE", "BAKED_DIGEST_DIR", "BAKED_LINKS_DIR", "BAKED_INDEX_SCHEMA"):
        assert getattr(runner_baked, name) == getattr(builtin_skill_bundle, name), name
    assert sandbox_toolset.BAKED_SKILLS_ROOT == builtin_skill_bundle.BAKED_SKILLS_ROOT


def test_bundle_module_runs_without_the_repository(tmp_path: Path, fake_repo: Path) -> None:
    """The image build and the release verifier run it as a lone file."""
    lone = tmp_path / "lone" / "builtin_skill_bundle.py"
    lone.parent.mkdir()
    shutil.copy(BUNDLE_MODULE, lone)
    out = tmp_path / "out"
    baked = subprocess.run(
        [sys.executable, str(lone), "bake", "--repo-root", str(fake_repo), "--out", str(out)],
        capture_output=True, text=True,
    )
    assert baked.returncode == 0, baked.stderr
    verified = subprocess.run([sys.executable, str(lone), "verify", "--root", str(out)], capture_output=True, text=True)
    assert verified.returncode == 0, verified.stderr


def test_read_only_paths_are_declared_validated_and_linked(fake_repo: Path, tmp_path: Path) -> None:
    skills_root = fake_repo / builtin_skill_bundle.SKILLS_SUBDIR
    index = builtin_skill_bundle.bake(
        tmp_path / "out", skills_root=skills_root, agent_skills_root=fake_repo / ".agents/skills"
    )
    assert index["skills"]["deck"]["read_only_paths"] == ["templates/icons"]
    assert (tmp_path / "out" / ".links" / "deck").read_text() == "templates/icons\n"
    assert index["skills"]["alpha"]["read_only_paths"] == []
    # A script inside a read-only subtree would resolve its own path into the image.
    bad = _skill(tmp_path / "bad", "bad", {"lib/x.py": "", "lib/data.txt": ""}, {"sandbox_read_only_paths": ["lib"]})
    with pytest.raises(builtin_skill_bundle.BundleError, match="holds scripts"):
        builtin_skill_bundle.read_only_paths(bad)
    escape = _skill(tmp_path / "esc", "esc", {"a/b.txt": ""}, {"sandbox_read_only_paths": ["../a"]})
    with pytest.raises(builtin_skill_bundle.BundleError):
        builtin_skill_bundle.read_only_paths(escape)


def test_shipped_read_only_declarations_are_valid() -> None:
    for skill_dir in builtin_skill_loader._builtin_skill_dirs():
        builtin_skill_bundle.read_only_paths(skill_dir)  # raises on a bad declaration
    assert builtin_skill_bundle.read_only_paths(builtin_skill_loader._SKILLS_ROOT / "pptx") == ["templates/icons"]


# ── 2. the control plane: baked only on an exact advertised digest ───────


class _Client:
    def __init__(self, health: dict | Exception, *, refuse: bool = False, fail: Exception | None = None):
        self._health = health
        self.refuse = refuse
        self.fail = fail
        self.creates: list[dict] = []
        self.loads: list[dict] = []

    async def health(self):
        if isinstance(self._health, Exception):
            raise self._health
        return self._health

    def _answer(self, kwargs):
        if kwargs.get("baked_skill") is not None:
            if self.fail is not None:
                raise self.fail
            if self.refuse:
                raise SimpleNamespaceError(BAKED_SKILL_UNAVAILABLE)
        return SimpleNamespace(sandbox_id="sbx", **{"skill": None})

    async def create_from_builtin(self, **kwargs):
        self.creates.append(kwargs)
        return self._answer(kwargs)

    async def load_skill(self, **kwargs):
        self.loads.append(kwargs)
        return self._answer(kwargs)


class SimpleNamespaceError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code
        self.status_code = 409


REF = BakedSkillRef(name="pdf", digest=DIGEST)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "health,baked",
    [
        ({"baked_skills": {"pdf": DIGEST}}, True),
        ({"baked_skills": {"pdf": OTHER}}, False),  # image built from other Skill files
        ({"baked_skills": {}}, False),
        ({"status": "ok"}, False),  # an old runner advertises nothing
        (RuntimeError("health down"), False),
    ],
    ids=["match", "mismatch", "absent", "old-runner", "no-health"],
)
async def test_create_sends_baked_only_on_an_exact_digest_match(health, baked, monkeypatch):
    monkeypatch.setattr(sandbox_baked_skills, "_upload_files", lambda files, ref: {"SKILL.md": "x"})
    client = _Client(health)

    await create_builtin_sandbox(client, skill_name="pdf", files=None, baked=REF, env={})

    assert len(client.creates) == 1
    call = client.creates[0]
    if baked:
        assert call["baked_skill"] == {"name": "pdf", "digest": DIGEST} and call["files"] == {}
    else:
        assert "baked_skill" not in call and call["files"] == {"SKILL.md": "x"}


@pytest.mark.asyncio
async def test_a_refusing_runner_gets_the_files(monkeypatch):
    monkeypatch.setattr(sandbox_baked_skills, "_upload_files", lambda files, ref: {"SKILL.md": "x"})
    client = _Client({"baked_skills": {"pdf": DIGEST}}, refuse=True)

    await create_builtin_sandbox(client, skill_name="pdf", files=None, baked=REF)
    await load_builtin_skill(client, sandbox_id="s", skill_name="pdf", files=None, baked=REF)

    assert [("baked_skill" in c, c["files"]) for c in client.creates] == [(True, {}), (False, {"SKILL.md": "x"})]
    assert [("baked_skill" in c, c["files"]) for c in client.loads] == [(True, {}), (False, {"SKILL.md": "x"})]


@pytest.mark.asyncio
async def test_other_runner_errors_are_not_masked_by_an_upload():
    client = _Client({"baked_skills": {"pdf": DIGEST}}, fail=SimpleNamespaceError("capacity_full"))
    with pytest.raises(SimpleNamespaceError):
        await create_builtin_sandbox(client, skill_name="pdf", files=None, baked=REF)
    assert len(client.creates) == 1


@pytest.mark.asyncio
async def test_no_reference_uploads_exactly_the_given_files():
    client = _Client({"baked_skills": {"pdf": DIGEST}})
    await create_builtin_sandbox(client, skill_name="video-edit-runtime", files={"a": "1"}, baked=None)
    assert client.creates == [{"skill_name": "video-edit-runtime", "files": {"a": "1"}}]


def _pre_baking_upload(skill_dir: Path) -> dict[str, str]:
    """What the upload sent before baking: every UTF-8 file; binaries left out."""
    files = {}
    for path in skill_dir.rglob("*"):
        if path.is_file():
            try:
                files[str(path.relative_to(skill_dir))] = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
    return files


def test_upload_fallback_reads_the_packaged_files_as_before() -> None:
    files = sandbox_baked_skills._upload_files(None, BakedSkillRef(name="pdf", digest=DIGEST))
    pdf = builtin_skill_loader._SKILLS_ROOT / "pdf"
    # The packaged directory may hold binaries (CI imports the pdf scripts,
    # leaving __pycache__/*.pyc next to them): the expectation is built the
    # pre-baking way, never by decoding every file.
    assert files == _pre_baking_upload(pdf) and "SKILL.md" in files
    # Caller-provided files (video runtime's two-file subset) win.
    assert sandbox_baked_skills._upload_files({"a": "1"}, BakedSkillRef(name="pdf", digest=DIGEST)) == {"a": "1"}



def test_upload_fallback_leaves_binaries_out_while_the_baked_bundle_carries_them(tmp_path: Path) -> None:
    skill = tmp_path / "skill"
    (skill / "scripts" / "__pycache__").mkdir(parents=True)
    (skill / "assets").mkdir()
    (skill / "SKILL.md").write_text("# skill", encoding="utf-8")
    (skill / "scripts" / "run.py").write_text("print('ok')", encoding="utf-8")
    (skill / "scripts" / "__pycache__" / "run.cpython-312.pyc").write_bytes(b"\xcb\r\r\n" + bytes(12))
    (skill / "assets" / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n" + bytes(8))

    uploaded = sandbox_baked_skills.read_skill_upload_files(skill)
    assert uploaded == {"SKILL.md": "# skill", "scripts/run.py": "print('ok')"}
    assert uploaded == _pre_baking_upload(skill)
    # The baked copy is the real directory: binaries included, bytecode not.
    bundled = {rel for rel, _ in builtin_skill_bundle.skill_bundle_files(skill)}
    assert bundled == {"SKILL.md", "scripts/run.py", "assets/logo.png"}

# ── 3. only packaged built-ins get a reference ───────────────────────────


def test_only_packaged_builtin_directories_get_a_reference(tmp_path: Path) -> None:
    pdf = builtin_skill_loader._SKILLS_ROOT / "pdf"
    ref = sandbox_baked_skills.baked_skill_ref(pdf)
    assert ref == BakedSkillRef(name="pdf", digest=builtin_skill_bundle.skill_source_sha256(pdf))
    # A byte-identical copy elsewhere (a workspace or edited Skill) is not packaged.
    copy = tmp_path / "pdf"
    shutil.copytree(pdf, copy)
    assert builtin_skill_bundle.skill_source_sha256(copy) == ref.digest
    assert sandbox_baked_skills.baked_skill_ref(copy) is None
    assert sandbox_baked_skills.baked_skill_ref(None) is None
    assert sandbox_baked_skills.baked_skill_ref_for_name("no-such-skill") is None


@pytest.mark.asyncio
async def test_workspace_bundles_never_send_a_baked_reference(monkeypatch):
    from packages.core.services import sandbox_session_service as sss

    sent = []

    class Client:
        async def health(self):
            return {"baked_skills": {"pdf": DIGEST}}

        async def create_from_files(self, **kwargs):
            sent.append(("files", kwargs))
            return SimpleNamespace(
                sandbox_id="s1", workdir="/skill", env_injection="exec", env_blocked=[],
                skill=SimpleNamespace(scripts=[], entry_hint=None, requirements_txt=None),
            )

        async def create_from_builtin(self, **kwargs):
            sent.append(("builtin", kwargs))
            raise AssertionError("a workspace Skill reached the built-in path")

        async def close(self):
            pass

    monkeypatch.setattr(sss, "_new_client", lambda *_a, **_k: Client())

    async def no_bind(**_kwargs):
        return None

    monkeypatch.setattr(sss, "_bind_sandbox", no_bind)
    settings = SimpleNamespace(SANDBOX_SERVICE_URL="http://runner", SANDBOX_API_TOKEN="t")
    bundle = sss.SandboxBundle(
        skill_name="pdf", files={"SKILL.md": "edited"}, source="workspace", baked_skill=REF,
    )
    owner = sss.SandboxOwner(entity_id="e1", user_id="u1")
    session = await sss._create_session(
        owner=owner, bundle=bundle, binding=sss.SandboxBinding(), settings=settings, register=False,
    )
    assert getattr(session, "sandbox_id", None) == "s1", session
    assert [kind for kind, _ in sent] == ["files"]
    assert "baked_skill" not in sent[0][1]


def test_skill_service_references_only_platform_builtins() -> None:
    """Entity (workspace) Skills read their own files; only the platform
    built-in branch asks for a reference."""
    source = (ROOT / "packages/core/services/skill_service.py").read_text()
    entity_branch, builtin_branch = source.split("# Platform built-in skill: read from on-disk skill_dir", 1)
    entity_branch = entity_branch[entity_branch.rindex("if skill.entity_id:"):]
    assert "baked_skill_ref" not in entity_branch
    assert "baked_skill = baked_skill_ref(skill_dir_path)" in builtin_branch
    assert 'source="workspace" if skill.entity_id else "builtin"' in source


def test_reservation_payload_carries_the_reference() -> None:
    from packages.core.services import sandbox_session_service as sss

    bundle = sss.SandboxBundle(skill_name="pdf", files={}, baked_skill=REF, baked_file_paths=("SKILL.md", "a.py"))
    payload = sss._reservation_request_payload(bundle)
    assert payload["baked_skill"] == REF.payload() and payload["files"] == {}
    assert BakedSkillRef.from_payload(payload["baked_skill"]) == REF
    assert bundle.file_paths() == ["SKILL.md", "a.py"]
    assert "baked_skill" not in sss._reservation_request_payload(sss.SandboxBundle(skill_name="w", files={"a": "1"}))


# ── 4. the runner ─────────────────────────────────────────────────────────


def _index(**skills: tuple[str, list[str], dict]) -> runner_baked.BakedSkillIndex:
    return runner_baked.BakedSkillIndex.parse(json.dumps({
        "schema": 1,
        "skills": {
            name: {"digest": digest, "files": files, "texts": texts, "read_only_paths": []}
            for name, (digest, files, texts) in skills.items()
        },
    }))


def test_index_resolves_only_the_exact_digest() -> None:
    index = _index(pdf=(DIGEST, ["SKILL.md"], {"SKILL.md": "x"}))
    assert index.advertised() == {"pdf": DIGEST}
    assert index.resolve("pdf", DIGEST).name == "pdf"
    for name, digest in (("pdf", OTHER), ("docx", DIGEST)):
        with pytest.raises(BakedSkillUnavailable):
            index.resolve(name, digest)


def test_baked_description_matches_a_scan_of_the_files(tmp_path: Path) -> None:
    """The runner describes a baked Skill from the index exactly as it would scan it."""
    skill_dir = _skill(
        tmp_path, "demo",
        {"scripts/run.py": "", "run.py": "", "requirements.txt": "six\n", ".env.example": "A=1\n",
         "node_modules/x/index.js": "", ".hidden/y.py": "", "templates/a.svg": ""},
    )
    files = [rel for rel, _ in builtin_skill_bundle.skill_bundle_files(skill_dir)]
    texts = builtin_skill_bundle._index_texts(builtin_skill_bundle.skill_bundle_files(skill_dir))
    baked = runner_baked.BakedSkill(name="demo", digest=DIGEST, files=tuple(files), texts=texts)
    stub = tmp_path / "stub"
    stub.mkdir()
    baked.write_root_texts(str(stub))

    described = baked.manifest("demo", str(stub))
    scanned = SkillScanner().scan(str(skill_dir))
    fields = ("scripts", "requirements_txt", "package_json", "env_vars", "entry_hint", "needs_sandbox", "description")
    assert {f: getattr(described, f) for f in fields} == {f: getattr(scanned, f) for f in fields}
    # Dependency manifests keep their bytes, so the preinstalled-deps digest matches.
    assert docker_backend._host_manifest_digest(str(stub), "requirements.txt") == docker_backend._host_manifest_digest(
        str(skill_dir), "requirements.txt"
    )


def _materialize(root: Path, name: str, digest: str, target: Path, replace: bool) -> int:
    return subprocess.run(
        [sys.executable, "-c", runner_baked.MATERIALIZE_PROGRAM, str(root), name, digest, str(target),
         "1" if replace else "0"],
        capture_output=True, text=True,
    ).returncode


def test_materialize_copies_writable_files_and_links_read_only_subtrees(fake_repo: Path, tmp_path: Path) -> None:
    out = tmp_path / "out"
    index = builtin_skill_bundle.bake(
        out,
        skills_root=fake_repo / builtin_skill_bundle.SKILLS_SUBDIR,
        agent_skills_root=fake_repo / builtin_skill_bundle.AGENT_SKILLS_SUBDIR,
    )
    digest = index["skills"]["deck"]["digest"]
    target = tmp_path / "skill"
    target.mkdir()
    (target / "leftover.txt").write_text("previous skill")

    assert _materialize(out, "deck", digest, target, replace=True) == 0
    assert not (target / "leftover.txt").exists()
    layouts = target / "templates" / "layouts" / "i.json"
    assert layouts.is_file() and not layouts.is_symlink()
    layouts.write_text('{"edited": true}')  # Skills rewrite their own files in place
    icons = target / "templates" / "icons"
    assert icons.is_symlink() and icons.resolve() == (out / "deck" / "templates" / "icons").resolve()
    script = target / "scripts" / "make.py"
    assert not script.is_symlink() and script.stat().st_mode & 0o111
    visible = [
        os.path.relpath(os.path.join(directory, name), target)
        for directory, _dirs, names in os.walk(target, followlinks=True)
        for name in names
    ]
    assert sorted(visible) == sorted(index["skills"]["deck"]["files"])

    # Same Skill again: packaged files restored, session work kept.
    (target / "projects").mkdir()
    (target / "projects" / "deck.pptx").write_text("work")
    assert _materialize(out, "deck", digest, target, replace=False) == 0
    assert layouts.read_text() == "{}"
    assert (target / "projects" / "deck.pptx").read_text() == "work"
    assert icons.is_symlink()

    # A digest the image does not hold: refused before anything is touched.
    assert _materialize(out, "deck", OTHER, target, replace=True) == 65
    assert (target / "projects" / "deck.pptx").exists()
    assert _materialize(out, "missing", digest, target, replace=True) == 65


@pytest.fixture
def docker(monkeypatch):
    fake = FakeDocker()
    monkeypatch.setattr(docker_backend, "_run_docker", fake)
    return fake


@pytest.mark.asyncio
async def test_setup_materializes_instead_of_uploading(docker, tmp_path: Path) -> None:
    docker.baked_digests = {"pdf": DIGEST}
    baked = runner_baked.BakedSkill(name="pdf", digest=DIGEST, files=("SKILL.md", "scripts/a.py"), texts={})
    skill = baked.manifest("pdf", str(tmp_path))
    sandbox = docker_backend.DockerSandbox("sbx-b", ContainerConfig())

    await sandbox.setup(skill, {}, auto_install=False, baked=baked)

    assert sandbox.status is SandboxStatus.READY
    # Into the Skill's own /skills/<slot>, where Skills live side by side
    # (sandbox-user-conversation) -- never the shared /skill view.
    assert docker.materialized == [["/opt/manor/skills", "pdf", DIGEST, "/skills/pdf", "1"]]
    assert not any("tar" in call for call in docker.calls), "baked Skills are never uploaded"


@pytest.mark.asyncio
async def test_materialize_takes_any_target_and_refuses_an_unheld_digest(docker) -> None:
    """The one entry point other layouts (e.g. /skills/<name>) call."""
    docker.baked_digests = {"pdf": DIGEST}
    sandbox = docker_backend.DockerSandbox("sbx-b", ContainerConfig())
    sandbox.container_name = "skill-sbx-demo"

    await sandbox.materialize_builtin_skill("pdf", DIGEST, "/skills/pdf", replace=False)
    assert docker.materialized == [["/opt/manor/skills", "pdf", DIGEST, "/skills/pdf", "0"]]
    with pytest.raises(BakedSkillUnavailable):
        await sandbox.materialize_builtin_skill("pdf", OTHER, "/skill")
    for name, digest, target in (("../x", DIGEST, "/skill"), ("pdf", "zz", "/skill"), ("pdf", DIGEST, "rel")):
        with pytest.raises(Exception):
            await sandbox.materialize_builtin_skill(name, digest, target)
    assert len(docker.materialized) == 2


@pytest.mark.asyncio
async def test_load_of_an_unheld_digest_leaves_the_sandbox_ready(docker) -> None:
    runner = SkillRunner()
    sandbox, _lease = await register_sandbox(runner)
    baked = runner_baked.BakedSkill(name="pdf", digest=DIGEST, files=("SKILL.md",), texts={"SKILL.md": "x"})
    runner._baked_index = ("sha256:img", runner_baked.BakedSkillIndex({"pdf": baked}))
    docker.baked_digests = {"pdf": OTHER}  # the container's image differs from the index

    with pytest.raises(BakedSkillUnavailable):
        await runner.load_skill_into_sandbox("sbx-1", "pdf", {}, baked_skill=("pdf", DIGEST))
    assert sandbox.status is SandboxStatus.READY and "sbx-1" in runner._sandboxes
    assert sandbox.skill.name == "demo"

    # Not in the runner's index at all: refused before the container is touched.
    calls = len(docker.calls)
    with pytest.raises(BakedSkillUnavailable):
        await runner.load_skill_into_sandbox("sbx-1", "pdf", {}, baked_skill=("pdf", OTHER))
    assert len(docker.calls) == calls

    docker.baked_digests = {"pdf": DIGEST}
    response = await runner.load_skill_into_sandbox("sbx-1", "pdf", {}, baked_skill=("pdf", DIGEST))
    assert response.skill.name == "pdf" and sandbox.skill.name == "pdf"
    assert docker.materialized[-1][1:] == ["pdf", DIGEST, "/skills/pdf", "1"]


@pytest.mark.asyncio
async def test_health_advertises_the_images_baked_skills(monkeypatch) -> None:
    index = _index(pdf=(DIGEST, ["SKILL.md"], {}))
    reads = []

    def load(image, previous, **_kw):
        reads.append(image)
        return previous if previous is not None else ("sha256:one", index)

    monkeypatch.setattr(skill_runner_module, "_image_exists_locally", lambda *_a, **_k: True)
    monkeypatch.setattr(skill_runner_module, "_load_baked_skill_index", load)
    runner = SkillRunner()
    assert runner.runtime_status()["baked_skills"] == {}
    await runner.refresh_image_status()
    assert runner.runtime_status()["baked_skills"] == {"pdf": DIGEST}

    def broken(*_a, **_k):
        raise RuntimeError("docker run failed")

    monkeypatch.setattr(skill_runner_module, "_load_baked_skill_index", broken)
    runner._image_status_at -= 3600
    await runner.refresh_image_status()
    await runner._image_probe
    # A failed re-read keeps the last good index (and backs off).
    assert runner.runtime_status()["baked_skills"] == {"pdf": DIGEST}


def test_create_route_refuses_an_unheld_digest_with_a_stable_code(monkeypatch) -> None:
    main = load_service_main(monkeypatch, "_sandbox_service_main_baked_test")
    runner = SkillRunner()
    monkeypatch.setattr(main, "runner", runner)
    from api import routes

    monkeypatch.setattr(routes, "_runner", runner)
    client = TestClient(main.app, headers={"X-Manor-Sandbox-Token": API_TOKEN})
    response = client.post(
        "/api/v1/sandbox/create-from-builtin",
        json={"skill_name": "pdf", "baked_skill": {"name": "pdf", "digest": DIGEST}},
    )
    assert response.status_code == 409
    assert response.json()["code"] == BAKED_SKILL_UNAVAILABLE


# ── 5. release verification ───────────────────────────────────────────────


def test_toolset_verify_checks_the_baked_skills(fake_repo: Path, tmp_path: Path, monkeypatch) -> None:
    import io

    missing = sandbox_toolset.verify_baked_skills(tmp_path / "nothing", stream=io.StringIO())
    assert missing and "index" in missing[0]

    out = tmp_path / "out"
    builtin_skill_bundle.bake(
        out,
        skills_root=fake_repo / builtin_skill_bundle.SKILLS_SUBDIR,
        agent_skills_root=fake_repo / builtin_skill_bundle.AGENT_SKILLS_SUBDIR,
    )
    assert sandbox_toolset.verify_baked_skills(out, stream=io.StringIO()) == []
    (out / "alpha" / "scripts" / "run.py").write_text("tampered")
    assert any("alpha" in failure for failure in sandbox_toolset.verify_baked_skills(out, stream=io.StringIO()))

    monkeypatch.setattr(sandbox_toolset, "verify", lambda *_a, **_k: [])
    manifest = Path(sandbox_toolset.MANIFEST_PATH)
    assert sandbox_toolset.main(["--manifest", str(manifest), "verify", "--baked-skills-root", str(tmp_path / "x")]) == 1
    shutil.rmtree(out)
    builtin_skill_bundle.bake(
        out,
        skills_root=fake_repo / builtin_skill_bundle.SKILLS_SUBDIR,
        agent_skills_root=fake_repo / builtin_skill_bundle.AGENT_SKILLS_SUBDIR,
    )
    assert sandbox_toolset.main(["--manifest", str(manifest), "verify", "--baked-skills-root", str(out)]) == 0


def test_toolset_verifier_ships_with_the_bundle_module() -> None:
    source = DOCKERFILE.read_text()
    assert re.search(
        r"COPY [^\n]*sandbox_toolset\.py [^\n]*builtin_skill_bundle\.py /opt/manor/toolset/", source
    ), "the image's verifier cannot check the baked Skills without builtin_skill_bundle.py beside it"


@pytest.mark.asyncio
async def test_a_baked_skill_loads_beside_others_in_the_users_shared_sandbox(monkeypatch):
    """On a runner that keeps Skills side by side (one shared sandbox per
    user), a packaged built-in Skill loads from the image's copy -- the runner
    materializes it into the Skill's own /skills/<slot> -- instead of
    uploading its files."""

    from packages.core.services import sandbox_session_service as sss

    loads = []

    class Client:
        async def health(self):
            return {"baked_skills": {"pdf": DIGEST}}

        async def load_skill(self, **kwargs):
            loads.append(kwargs)
            return SimpleNamespace(
                sandbox_id="s-shared", skill_dir="/skills/pdf", env_blocked=[],
                skill=SimpleNamespace(scripts=["scripts/a.py"], entry_hint=None, requirements_txt=None),
            )

    async def committed(**kwargs):
        return kwargs["session"]

    monkeypatch.setattr(sss, "_commit_reuse", committed)
    existing = sss.SandboxSession(
        sandbox_id="s-shared", base_url="http://runner", created_at=1.0, last_acquired_at=1.0,
        loaded_skills=["docx"], env_injection="exec", current_skill="docx", skill_slots=True,
    )
    bundle = sss.SandboxBundle(skill_name="pdf", files={}, source="builtin", baked_skill=REF)
    settings = SimpleNamespace(SANDBOX_QUEUE_POLL_SECONDS=1)

    session = await sss._reuse_slots(
        client=Client(), owner=sss.SandboxOwner(entity_id="e1", user_id="u1"), existing=existing,
        info=SimpleNamespace(workdir="/skill"), bundle=bundle, binding=sss.SandboxBinding(),
        settings=settings, sleep=None, clock=lambda: 0.0, deadline=10.0, scope="user",
    )

    [load] = loads
    assert load["baked_skill"] == {"name": "pdf", "digest": DIGEST} and load["files"] == {}
    assert session.loaded_skills == ["docx", "pdf"]
