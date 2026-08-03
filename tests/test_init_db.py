import subprocess
import sys
from pathlib import Path


def test_init_db_imports_after_runtime_tool_registry_refactor() -> None:
    root = Path(__file__).parents[1]
    script_path = root / "scripts/init_db.py"
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            f"import runpy; runpy.run_path({str(script_path)!r})",
        ],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
