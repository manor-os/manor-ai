"""Evidence carried between Agent attempts, separate from execution snapshots."""
from __future__ import annotations

import json
from typing import Any

from packages.core.services.sensitive_data import sanitize_sensitive_payload


def agent_retry_context(
    result: dict[str, Any], *, run_id: str, step_id: str, error: str | None,
    receipts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Keep complete, redacted evidence; never nest copies of recovery history."""
    previous = result.get("agent_recovery")
    previous = previous if isinstance(previous, dict) else {}
    attempts = [
        item for item in previous.get("attempts", [])
        if isinstance(item, dict) and item.get("run_id") != run_id
    ]
    attempts.append({
        "run_id": run_id,
        "step_id": step_id,
        "run_error": error,
        "result": {key: value for key, value in result.items() if key not in {
            "agent_recovery", "inputs", "runtime_trace", "usage",
        }},
        # Operations are scoped to the run, not necessarily this Agent step.
        "operation_receipts": receipts or [],
        "operation_receipts_scope": "workflow_run",
    })
    return sanitize_sensitive_payload({"attempts": attempts})


def recovery_prompt_evidence(value: Any, *, max_chars: int = 32_000) -> Any:
    """Bound the prompt without replacing an entire result with a JSON prefix.

    Durable recovery evidence stays complete. Oversized fields are explicit
    omissions, never plausible but shortened IDs. Divide the budget between
    siblings so a large description cannot hide a later identity or blocker.
    """
    value = sanitize_sensitive_payload(value)

    def compact(item: Any, budget: int) -> Any:
        if len(json.dumps(item, ensure_ascii=False)) <= budget:
            return item
        omitted = {"truncated": True}
        if isinstance(item, dict) and item:
            overhead = len(json.dumps(dict.fromkeys(item), ensure_ascii=False))
            available = budget - overhead
            if available >= len(item) * 64:
                share = available // len(item)
                return {key: compact(child, share) for key, child in item.items()}
        elif isinstance(item, list) and item:
            # Keep the first attempt's effect evidence and the latest attempts.
            count = min(len(item), max(0, (budget - 64) // 128))
            if count:
                if count == len(item):
                    selected = item
                elif count > 1:
                    selected = [item[0], *item[-(count - 1):]]
                else:
                    selected = [item[-1]]
                share = (budget - 64) // count - 2
                result = [compact(child, share) for child in selected]
                if count < len(item):
                    result.append({"truncated": True, "omitted_items": len(item) - count})
                return result
        elif isinstance(item, str) and budget >= 128:
            # JSON escaping can expand a preview; measure the serialized form.
            preview = item[: max(0, (budget - 64) // 6)]
            return {"truncated": True, "preview": preview}
        return omitted

    return compact(value, max_chars)
