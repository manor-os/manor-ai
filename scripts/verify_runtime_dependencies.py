#!/usr/bin/env python3
"""Verify an installed API environment against its hashed uv runtime export.

This is a parity check, not a resolver or a replacement for pip's hash check.
Run it with the final image's interpreter after installing the exported closure.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import platform
import re
import sys
import tomllib
from collections.abc import Iterable, Mapping
from pathlib import Path

from packaging.markers import default_environment
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version


_HASH = re.compile(r"(?:^|\s)--hash=sha256:[0-9a-fA-F]{64}(?=\s|$)")


def runtime_pins(
    text: str, *, marker_environment: Mapping[str, str] | None = None,
) -> dict[str, Version]:
    """Parse uv's hashed requirements format without losing target markers."""
    environment = {**default_environment(), "extra": "", **(marker_environment or {})}
    pins: dict[str, Version] = {}
    pending: list[str] = []
    for line_number, physical_line in enumerate(text.splitlines(), 1):
        line = physical_line.strip()
        if not line or line.startswith("#"):
            continue
        continued = line.endswith("\\")
        pending.append(line[:-1].rstrip() if continued else line)
        if continued:
            continue
        logical_line = " ".join(pending)
        pending.clear()
        requirement_text, hash_count = _HASH.subn(" ", logical_line)
        if not hash_count:
            raise ValueError(f"Requirement ending at line {line_number} has no SHA256 hash")
        requirement = Requirement(requirement_text.strip())
        specifiers = list(requirement.specifier)
        if requirement.url or len(specifiers) != 1 or specifiers[0].operator != "==":
            raise ValueError(f"{requirement.name}: runtime requirements must have one exact == pin")
        version = Version(specifiers[0].version)
        if requirement.marker and not requirement.marker.evaluate(environment):
            continue
        name = canonicalize_name(requirement.name, validate=True)
        if name in pins and pins[name] != version:
            raise ValueError(f"{name}: conflicting target-active pins {pins[name]} and {version}")
        # Extras select dependency closure during export; they are not separate
        # installed distribution names. The full closure is checked below.
        pins[name] = version
    if pending:
        raise ValueError("Runtime requirements end with an unfinished continuation")
    if not pins:
        raise ValueError("Runtime requirements contain no target-active pinned dependencies")
    return pins


def verify_runtime_dependencies(
    requirements_text: str,
    installed_distributions: Iterable[tuple[str, str]],
    *,
    expected_project_version: str,
    expected_pip_version: str,
    marker_environment: Mapping[str, str] | None = None,
) -> dict[str, object]:
    expected = runtime_pins(requirements_text, marker_environment=marker_environment)
    if {"manor", "pip"} & expected.keys():
        raise ValueError("Runtime export must omit the first-party project and pip bootstrap")
    runtime_count = len(expected)
    expected.update(manor=Version(expected_project_version), pip=Version(expected_pip_version))
    installed: dict[str, Version] = {}
    for raw_name, raw_version in installed_distributions:
        name = canonicalize_name(raw_name, validate=True)
        version = Version(raw_version)
        if name in installed and installed[name] != version:
            raise ValueError(f"{name}: conflicting installed versions {installed[name]} and {version}")
        installed[name] = version

    missing = sorted(expected.keys() - installed.keys())
    unexpected = sorted(installed.keys() - expected.keys())
    mismatched = {
        name: {"expected": str(expected[name]), "installed": str(installed[name])}
        for name in sorted(expected.keys() & installed.keys())
        if expected[name] != installed[name]
    }
    if missing or unexpected or mismatched:
        raise ValueError(json.dumps({
            "missing": missing, "unexpected": unexpected, "version_mismatches": mismatched,
        }, sort_keys=True))
    return {
        "status": "passed",
        "runtime_dependency_count": runtime_count,
        "installed_unique_distribution_count": len(installed),
        "first_party": {"name": "manor", "version": str(expected["manor"])},
        "pip_version": str(expected["pip"]),
    }


def installed_distribution_versions() -> Iterable[tuple[str, str]]:
    for distribution in importlib.metadata.distributions():
        name = distribution.metadata.get("Name")
        if not name:
            raise ValueError("Installed distribution is missing its Name metadata")
        yield name, distribution.version


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--requirements", type=Path, required=True)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--expected-pip-version", required=True)
    args = parser.parse_args(argv)
    try:
        project_bytes = args.project.read_bytes()
        project = tomllib.loads(project_bytes.decode("utf-8"))["project"]
        if project.get("name") != "manor" or not isinstance(project.get("version"), str):
            raise ValueError("Expected Manor project metadata with a static version")
        requirements_bytes = args.requirements.read_bytes()
        result = verify_runtime_dependencies(
            requirements_bytes.decode("utf-8"),
            installed_distribution_versions(),
            expected_project_version=project["version"],
            expected_pip_version=args.expected_pip_version,
        )
        result.update({
            "requirements_sha256": hashlib.sha256(requirements_bytes).hexdigest(),
            "project_sha256": hashlib.sha256(project_bytes).hexdigest(),
            "python_version": platform.python_version(),
            "platform": sys.platform,
        })
    except (OSError, ValueError, KeyError) as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, sort_keys=True))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
