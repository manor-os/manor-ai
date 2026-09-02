import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
MS_EXCEL_PATH = ROOT / "packages/core/ai/mcp/ms_excel.py"


def ms_excel_handler_names() -> set[str]:
    source = MS_EXCEL_PATH.read_text(encoding="utf-8")
    match = re.search(r"_HANDLERS\s*=\s*\{(?P<body>.*?)\n\}", source, re.S)
    assert match is not None
    return set(re.findall(r'"([^"]+)":\s*_[a-z_]+', match.group("body")))


def test_ms_excel_skill_config_exposes_every_mcp_tool():
    config = json.loads(
        (ROOT / "packages/core/ai/skills/mcp_ms_excel/config.json").read_text(
            encoding="utf-8",
        ),
    )

    configured_tools = set(config["tools"])
    catalog_tools = {
        f"mcp__ms_excel__{name}"
        for name in ms_excel_handler_names()
    }

    assert configured_tools == catalog_tools
