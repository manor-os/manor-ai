"""A Blueprint kept as a source tree merges in git like code.

What these protect: the tree is only a layout (every built-in Blueprint comes
back exactly, packed or not); two people editing different paragraphs of one
prompt merge without a conflict, where the single-file export conflicts; and a
folder packed by hand installs like an exported package.
"""
from __future__ import annotations

import gzip
import io
import json
import subprocess
import tarfile
from pathlib import Path

import pytest

from packages.core.blueprints.package import PackageError, asset_from_bytes, pack, pack_source, unpack
from packages.core.blueprints.source_tree import SourceTreeError, join, split

BUILTIN_BLUEPRINTS = sorted(Path("packages/core/blueprints/configs").rglob("*.json"))
PROMPT = "\n\n".join(f"Paragraph {n}: do the work for step {n} and report what changed." for n in range(1, 6))


def _payload() -> dict:
    return {
        "manifest": {"blueprint_version": "1.1", "title": "Studio"},
        "embedded": {"agents": [{"slug": "writer", "name": "Writer", "system_prompt": PROMPT}]},
        "recipe": {"workflows": [{"slug": "intake", "name": "Intake", "steps": []}]},
    }


@pytest.mark.parametrize("config", BUILTIN_BLUEPRINTS, ids=lambda path: path.stem)
def test_every_builtin_blueprint_comes_back_exactly_from_its_tree(config):
    payload = json.loads(config.read_text())
    files = split(payload)
    assert join(files) == (payload, [])
    assert unpack(pack_source(payload, []))[0] == payload
    # Long text leaves the JSON, so no component file is one unreadable line.
    assert max(len(line) for name, data in files.items() if name.endswith(".json")
               for line in data.decode().splitlines()) < 1000


def _merge(tmp_path: Path, base: str, ours: str, theirs: str) -> tuple[int, str]:
    paths = []
    for name, text in (("ours", ours), ("base", base), ("theirs", theirs)):
        path = tmp_path / name
        path.write_text(text)
        paths.append(str(path))
    result = subprocess.run(["git", "merge-file", "-p", *paths], capture_output=True, text=True)
    return result.returncode, result.stdout


def test_two_people_editing_different_paragraphs_of_one_prompt_merge(tmp_path):
    base = _payload()
    mine, simons = json.loads(json.dumps(base)), json.loads(json.dumps(base))
    mine["embedded"]["agents"][0]["system_prompt"] = PROMPT.replace("Paragraph 1: do", "Paragraph 1: carefully do")
    simons["embedded"]["agents"][0]["system_prompt"] = PROMPT.replace("Paragraph 5: do", "Paragraph 5: always do")

    def single_file(payload):
        return json.dumps(payload, sort_keys=True, ensure_ascii=False, indent=2) + "\n"

    conflicts, _ = _merge(tmp_path, single_file(base), single_file(mine), single_file(simons))
    assert conflicts, "the single-file export puts the whole prompt on one line"

    trees = [split(payload) for payload in (base, mine, simons)]
    merged = {}
    for name in trees[0]:
        conflicts, text = _merge(tmp_path, *(tree[name].decode() for tree in trees))
        assert conflicts == 0, name
        merged[name] = text.encode()
    prompt = join(merged)[0]["embedded"]["agents"][0]["system_prompt"]
    assert "Paragraph 1: carefully do" in prompt and "Paragraph 5: always do" in prompt


def _tar(entries: dict[str, bytes]) -> bytes:
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as archive:
        for name, data in entries.items():
            info = tarfile.TarInfo(name)
            if data is None:
                info.type = tarfile.DIRTYPE
                archive.addfile(info)
                continue
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return gzip.compress(raw.getvalue())


def test_a_folder_packed_by_hand_reads_like_an_export_and_is_checked_the_same():
    asset = asset_from_bytes(b"RIFF....WEBP", name="cover.webp", mime_type="image/webp")
    payload = {**_payload(), "manifest": {**_payload()["manifest"], "cover_image_url": asset.placeholder}}
    files = {**split(payload, assets=[{"sha256": asset.sha256, "path": asset.path, "mime_type": asset.mime_type,
                                       "name": asset.name}]), asset.path: asset.data}
    # `tar -czf blueprint.manor -C blueprint .` writes ./-prefixed names, folders and macOS litter.
    by_hand = _tar({"./": None, **{f"./{name}": data for name, data in files.items()}, "./._manor.json": b"x"})
    assert unpack(by_hand)[0] == payload
    in_folder = _tar({f"blueprint/{name}": data for name, data in files.items()})
    assert [item.sha256 for item in unpack(in_folder)[1]] == [asset.sha256]
    assert unpack(pack(payload, [asset]))[0] == payload  # the package/ layout still reads

    with pytest.raises(PackageError, match="does not match"):
        unpack(_tar({**files, asset.path: b"tampered"}))
    missing = {name: data for name, data in files.items() if not name.endswith("system_prompt.md")}
    with pytest.raises(PackageError, match="missing"):
        unpack(_tar(missing))
    with pytest.raises(SourceTreeError, match=r"\$ref"):
        split({**payload, "recipe": {"note": {"$ref": "elsewhere.md"}}})
