"""Production sources in either a checkout or an exported source archive."""
from pathlib import Path
import subprocess


def production_python_files(root: Path) -> list[str]:
    manifest = root / "OSS_MANIFEST.txt"
    if manifest.is_file():
        listed = manifest.read_text(encoding="utf-8").splitlines()
    else:
        listed = subprocess.run(
            ["git", "ls-files", "-z", "--", "packages", "apps"],
            cwd=root, check=True, capture_output=True, text=True,
        ).stdout.split("\0")
    return sorted(
        path for path in listed
        if path.startswith(("packages/", "apps/")) and path.endswith(".py") and (root / path).is_file()
    )
