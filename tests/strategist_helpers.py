"""Success-only Strategist mocks acknowledge the exact supplied observation."""
from __future__ import annotations

import json


def proposal_with_execution_review(payload: dict, user_prompt: str) -> dict:
    """Keep execution validation real without hardcoding a snapshot revision."""
    assert "execution_review" not in payload, "Explicit execution-review fixtures must stay unchanged"
    section = user_prompt.rsplit("# Current execution evidence", 1)[1].split("\n", 1)[1]
    snapshot, _ = json.JSONDecoder().raw_decode(section.lstrip())
    assert snapshot["status"] == "available"
    assert isinstance(snapshot["revision"], str) and snapshot["revision"]
    return {
        **payload,
        "execution_review": {"revision": snapshot["revision"], "references": []},
    }
