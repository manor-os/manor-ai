"""Smoke test for governance.

Covers:
  1. policy_from_dict round-trips
  2. decide() — hard deny / HITL pause / auto_approve precedence
  3. risk ceiling
  4. per-kind budget cap
  5. update_policy + list_revisions writes the audit chain
  6. check_step_policy returns DEFAULT for unknown workspaces

Run with: uv run python -m packages.core.governance._smoke
"""
from __future__ import annotations

import asyncio
import sys

from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from packages.core.governance import (
    DEFAULT_POLICY,
    PolicyError,
    WorkspacePolicy,
    check_step_policy,
    get_policy,
    list_revisions,
    policy_from_dict,
    policy_to_dict,
    update_policy,
)
from packages.core.governance.policy import decide
from packages.core.models.governance import (
    GovernancePolicy,
    GovernanceRevision,
)


def _check(cond: bool, msg: str) -> None:
    print(f"  {'✓' if cond else '✗'} {msg}")
    if not cond:
        sys.exit(1)


# ── Policy-only cases (synchronous) ───────────────────────────────────

def case_round_trip() -> None:
    print("[case] policy_from_dict round-trips")
    p = WorkspacePolicy(
        never_allow_actions=["billing.*"],
        hitl_required_actions=["x.delete_account"],
        auto_approve_actions=["x.like"],
        max_risk_level="medium",
        budget_caps_per_kind={"action": 100, "llm": 500},
    )
    raw = policy_to_dict(p)
    p2 = policy_from_dict(raw)
    _check(p2 == p, "round-trip equal")


def case_invalid_max_risk() -> None:
    print("\n[case] invalid max_risk_level rejected")
    try:
        policy_from_dict({"max_risk_level": "extreme"})
        _check(False, "should have raised")
    except PolicyError as exc:
        _check("low|medium|high" in str(exc), "error names valid options")


def case_decide_hard_deny() -> None:
    print("\n[case] decide() hard-denies on never_allow")
    p = WorkspacePolicy(never_allow_actions=["billing.*"])
    d = decide(p, kind="action", action_key="billing.refund", risk_level="low")
    _check(d.allowed is False, "denied")
    _check(d.matched_rule == "billing.*", "rule pattern recorded")


def case_decide_hitl_pause() -> None:
    print("\n[case] decide() pauses for HITL")
    p = WorkspacePolicy(hitl_required_actions=["x.delete_*"])
    d = decide(p, kind="action", action_key="x.delete_post", risk_level="low")
    _check(d.allowed is False, "not allowed")
    _check(d.pause_for_hitl is True, "pause_for_hitl set")
    _check(d.matched_rule == "x.delete_*", "matched glob rule")


def case_auto_approve_beats_hitl() -> None:
    print("\n[case] auto_approve overrides hitl_required")
    p = WorkspacePolicy(
        hitl_required_actions=["x.*"],
        auto_approve_actions=["x.like"],
    )
    d = decide(p, kind="action", action_key="x.like", risk_level="low")
    _check(d.allowed is True, "auto_approve wins for x.like")
    d2 = decide(p, kind="action", action_key="x.post", risk_level="low")
    _check(d2.pause_for_hitl is True, "x.post still HITL")


def case_risk_ceiling() -> None:
    print("\n[case] max_risk_level ceiling")
    p = WorkspacePolicy(max_risk_level="medium")
    d = decide(p, kind="action", action_key="x.post", risk_level="high")
    _check(d.allowed is False, "high risk denied under medium ceiling")
    _check(d.matched_rule == "max_risk_level", "rule named")


def case_per_kind_cap() -> None:
    print("\n[case] per-kind budget cap")
    p = WorkspacePolicy(budget_caps_per_kind={"llm": 50})
    d = decide(
        p, kind="llm", action_key=None, risk_level="low",
        spent_credits_per_kind={"llm": 50},
    )
    _check(d.allowed is False, "denied at exactly the cap")
    _check("budget_caps_per_kind.llm" in (d.matched_rule or ""), "rule named")


# ── DB cases ──────────────────────────────────────────────────────────

async def db_cases() -> None:
    print("\n[case] update_policy writes audit chain")
    # Swap JSONB → JSON for sqlite.
    from sqlalchemy import JSON
    for tbl in (GovernancePolicy.__table__, GovernanceRevision.__table__):
        for col in tbl.columns:
            if isinstance(col.type, JSONB):
                col.type = JSON()

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(GovernancePolicy.__table__.create)
        await conn.run_sync(GovernanceRevision.__table__.create)
    SessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with SessionLocal() as db:
        # Two updates → two revisions.
        await update_policy(
            db, entity_id="ent_x", workspace_id="ws_x",
            policy=WorkspacePolicy(never_allow_actions=["billing.*"]),
            changed_by="user_a", change_summary="initial setup",
        )
        await update_policy(
            db, entity_id="ent_x", workspace_id="ws_x",
            policy=WorkspacePolicy(
                never_allow_actions=["billing.*"],
                hitl_required_actions=["x.delete_*"],
            ),
            changed_by="user_a", change_summary="add HITL for delete",
        )
        await db.commit()

    async with SessionLocal() as db:
        cur = await get_policy(db, "ws_x")
        _check(cur.never_allow_actions == ["billing.*"], "current policy persisted")
        _check("x.delete_*" in cur.hitl_required_actions, "second revision applied")
        revs = await list_revisions(db, "ws_x")
        _check(len(revs) == 2, f"two revisions logged (got {len(revs)})")
        _check(revs[0].revision == 2, "newest first")
        _check(revs[1].change_summary == "initial setup", "summary preserved")

    print("\n[case] check_step_policy returns DEFAULT for unknown workspace")
    async with SessionLocal() as db:
        d = await check_step_policy(
            db, workspace_id="ws_unknown",
            kind="action", action_key="anything", risk_level="low",
        )
        _check(d.allowed is True, "default policy = allow")

    print("\n[case] check_step_policy honours stored rule")
    async with SessionLocal() as db:
        d = await check_step_policy(
            db, workspace_id="ws_x",
            kind="action", action_key="billing.refund", risk_level="low",
        )
        _check(d.allowed is False, "denied per stored never_allow")


def main() -> None:
    case_round_trip()
    case_invalid_max_risk()
    case_decide_hard_deny()
    case_decide_hitl_pause()
    case_auto_approve_beats_hitl()
    case_risk_ceiling()
    case_per_kind_cap()
    asyncio.run(db_cases())
    print("\nSMOKE OK")


if __name__ == "__main__":
    main()
