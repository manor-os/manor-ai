#!/usr/bin/env python3
"""Template mode: build a native editable PPTX from deck.json in one step.

    python3 scripts/build_deck.py projects/<name>/deck.json

Validates deck.json, fits every text slot, writes
``projects/<name>/exports/<name>.pptx``, runs the final quality gate, and
prints one JSON summary. On failure it lists every problem at once — fix
deck.json and run the same command again.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from deck_engine import BuildError, build_deck
from deck_engine.schema import layout_catalog
from pptx_quality_gate import run_gate


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("deck", type=Path, help="path to <project>/deck.json")
    args = parser.parse_args()

    deck_path = args.deck.expanduser().resolve()
    project = deck_path.parent
    try:
        output, plans = build_deck(deck_path)
    except BuildError as exc:
        print(json.dumps({
            "status": "fix_deck_json",
            "errors": exc.errors,
            "layouts": layout_catalog(),
            "next": "Edit deck.json to fix every error above, then rerun this command.",
        }, ensure_ascii=False, indent=2))
        return 1

    report_path = project / "qa" / "pptx-quality.json"
    gate = run_gate(
        output,
        mode="auto",
        project=project,
        render_dir=project / "qa" / "final-render",
    )
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(gate, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    passed = gate.get("status") == "pass"
    summary = {
        "status": "ready" if passed else "gate_failed",
        "pptx": str(output),
        "slides": len(plans),
        "quality_score": gate.get("quality_score"),
        "errors": gate.get("errors", []),
        "warnings": gate.get("warnings", []),
        "next": (
            f"Call sandbox action='save_result' with file_path='{output}' and artifact_role='final'."
            if passed
            else (
                "Fix content errors (placeholder/draft words, internal production language, "
                "URLs outside [Sources]) in deck.json and rerun. Layout or overflow errors "
                "are template-engine defects: report them instead of retrying."
            )
        ),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
