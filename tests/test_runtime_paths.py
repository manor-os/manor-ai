"""Owner-only runtime files must resist shared-temp symlink substitution."""
from __future__ import annotations

import os
import stat

import pytest

from packages.core.services.runtime_paths import atomic_write_text, private_runtime_dir


def test_private_runtime_directory_is_owner_only(tmp_path, monkeypatch):
    monkeypatch.setenv("MANOR_RUNTIME_DIR", str(tmp_path))
    path = private_runtime_dir("cache", "provider")
    assert path.is_dir()
    assert path.stat().st_uid == os.getuid()
    assert stat.S_IMODE(path.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_private_runtime_directory_rejects_symlink(tmp_path, monkeypatch):
    monkeypatch.setenv("MANOR_RUNTIME_DIR", str(tmp_path))
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / f"manor-runtime-{os.getuid()}"
    root.symlink_to(outside, target_is_directory=True)
    with pytest.raises(RuntimeError, match="trusted owner directory"):
        private_runtime_dir("cache")


def test_atomic_write_replaces_symlink_instead_of_following_it(tmp_path):
    victim = tmp_path / "victim.txt"
    victim.write_text("must stay unchanged")
    target = tmp_path / "cache.json"
    target.symlink_to(victim)
    atomic_write_text(target, '{"safe":true}')
    assert not target.is_symlink()
    assert target.read_text() == '{"safe":true}'
    assert victim.read_text() == "must stay unchanged"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_runtime_path_components_cannot_traverse(tmp_path, monkeypatch):
    monkeypatch.setenv("MANOR_RUNTIME_DIR", str(tmp_path))
    for value in ("", ".", "..", "a/b", "a\\b"):
        with pytest.raises(ValueError):
            private_runtime_dir(value)
