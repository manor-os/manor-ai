from __future__ import annotations

import os
from pathlib import Path
import re
import stat
import subprocess


ROOT = Path(__file__).resolve().parents[1]
GENERATOR = ROOT / "scripts" / "generate_nango_runtime_env.sh"
EXAMPLE = ROOT / "deploy" / "k8s" / "env" / "nango-runtime.env.example"

REQUIRED_KEYS = (
    "NANGO_DB_PASSWORD",
    "NANGO_ENCRYPTION_KEY",
    "NANGO_SECRET_KEY",
    "NANGO_PUBLIC_KEY",
    "NANGO_CONNECT_HMAC_KEY",
    "NANGO_ADMIN_INVITE_TOKEN",
)

UUID_V4_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)


def _values(path: Path) -> dict[str, str]:
    return dict(
        line.split("=", 1)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
    )


def test_nango_runtime_example_has_only_cluster_owned_keys() -> None:
    assert tuple(_values(EXAMPLE)) == REQUIRED_KEYS


def test_generator_writes_private_fresh_bundle_without_printing_values(tmp_path: Path) -> None:
    output = tmp_path / "test-nango.env"

    result = subprocess.run(
        [str(GENERATOR), "test", str(output)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    values = _values(output)
    assert tuple(values) == REQUIRED_KEYS
    assert len(set(values.values())) == len(REQUIRED_KEYS)
    assert len(values["NANGO_DB_PASSWORD"]) == 64
    assert UUID_V4_PATTERN.fullmatch(values["NANGO_SECRET_KEY"])
    assert UUID_V4_PATTERN.fullmatch(values["NANGO_PUBLIC_KEY"])
    assert re.fullmatch(r"[0-9a-f]{64}", values["NANGO_CONNECT_HMAC_KEY"])
    assert all(value not in result.stdout + result.stderr for value in values.values())
    assert all(f"{key}_SHA256=" in result.stdout for key in REQUIRED_KEYS)

    second = subprocess.run(
        [str(GENERATOR), "test", str(output)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert second.returncode == 2
    assert "already exists" in second.stderr


def test_generator_rejects_counterpart_value_collisions(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_state = tmp_path / "openssl-state"
    fake_openssl = fake_bin / "openssl"
    fake_openssl.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
if [[ "$1" == "dgst" ]]; then
  exec /usr/bin/openssl "$@"
fi
count=0
[[ ! -f "$FAKE_OPENSSL_STATE" ]] || count="$(cat "$FAKE_OPENSSL_STATE")"
count=$((count + 1))
printf '%s' "$count" >"$FAKE_OPENSSL_STATE"
case "$count" in
  1) printf '%064d\\n' 1 ;;
  2) printf 'AgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgI=\\n' ;;
  3) printf '%064d\\n' 3 ;;
  4) printf '%064d\\n' 4 ;;
  5) printf '%064d\\n' 5 ;;
  *) exit 3 ;;
esac
""",
        encoding="utf-8",
    )
    fake_openssl.chmod(0o755)
    counterpart = tmp_path / "test.env"
    counterpart.write_text(
        "\n".join(
            (
                f"NANGO_DB_PASSWORD={'0' * 64}",
                f"NANGO_ENCRYPTION_KEY={'B' * 43}=",
                "NANGO_SECRET_KEY=00000000-0000-4000-8000-000000000003",
                "NANGO_PUBLIC_KEY=00000000-0000-4000-8000-000000000004",
                f"NANGO_CONNECT_HMAC_KEY={'0' * 64}",
                f"NANGO_ADMIN_INVITE_TOKEN={'5'.rjust(64, '0')}",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    counterpart.chmod(0o600)
    output = tmp_path / "prod.env"

    result = subprocess.run(
        [str(GENERATOR), "prod", str(output), "--compare", str(counterpart)],
        cwd=ROOT,
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "FAKE_OPENSSL_STATE": str(fake_state),
        },
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2
    assert not output.exists()
    assert "must use a distinct value" in result.stderr
    assert all(value not in result.stderr for value in _values(counterpart).values())


def test_generator_source_rejects_known_legacy_defaults() -> None:
    source = GENERATOR.read_text(encoding="utf-8")

    for unsafe in (
        "nango_secret",
        "manor-dev-admin",
        "RzV0YjJ4d3F1cFdfQXFqSHdSeUF6UWp4bUtxN1Y4WkE=",
    ):
        assert unsafe in source
