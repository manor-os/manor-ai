"""Tests for the intent->tool-path memory service (tool discovery v2, spec §A3).

See docs/superpowers/specs/2026-07-25-tool-discovery-v2-design.md (§A3)
and docs/superpowers/plans/2026-07-25-tool-discovery-v2-memory.md.

Fixture conventions (client, _register_owner) mirror
tests/test_manor_mcp_admin.py / tests/test_tool_discovery_v2.py. This file
ships in OSS, so no cloud-only imports or platform-admin references.
"""
from __future__ import annotations

import pytest
from httpx import AsyncClient

from packages.core.services import tool_path_memory as tpm


async def _register_owner(client: AsyncClient, username: str) -> tuple[dict[str, str], str, str]:
    resp = await client.post(
        "/api/v1/auth/register",
        json={
            "username": username,
            "email": f"{username}@example.com",
            "password": "securepass123",
            "entity_name": f"{username} Co",
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    headers = {"Authorization": f"Bearer {data['access_token']}"}
    me = await client.get("/api/v1/auth/me", headers=headers)
    assert me.status_code == 200, me.text
    return headers, data["user_id"], me.json()["entity_id"]


def test_intent_signature_normalizes_and_bounds():
    """intent_signature is order-insensitive and case-insensitive over its
    tokenized terms, and bounds to <=12 terms.

    Deviation from the plan's literal test: runtime_search_terms's CJK
    regex ([\\u4e00-\\u9fff]+) captures each *contiguous* run of Chinese
    characters as a single token — it does not word-segment. The plan's
    original two example strings ("...关于新功能上线！" with no internal
    spaces vs "新功能 上线" with a space between them) tokenize to
    DIFFERENT term sets (one big blob vs two separate terms), so they
    were never going to produce the same signature regardless of any
    "order-insensitive" property — that's a tokenization-boundary
    mismatch, not an order issue. Confirmed by running the literal plan
    test first (it failed pre-fix with mismatched sets, not just order).
    Using two inputs with the SAME space-delimited token set in a
    different order isolates the actual property under test. This
    matches the design's explicit no-embeddings-in-v2 stance (substring/
    token-overlap only) — semantic equivalence across differently
    punctuated free text is intentionally out of scope for the v2
    tokenizer.
    """
    sig_a = tpm.intent_signature("帮我 发一条 x post 关于 新功能 上线")
    sig_b = tpm.intent_signature("发一条 x post 新功能 上线 帮我 关于")
    assert sig_a == sig_b  # order-insensitive over the same token set
    assert len(sig_a.split(" ")) <= 12


def test_intent_overlap_scoring():
    a = tpm.intent_signature("发一条 x post")
    strong = tpm.overlap_score(a, tpm.intent_signature("再发一条 x post 宣传"))
    weak = tpm.overlap_score(a, tpm.intent_signature("查看系统健康"))
    assert strong > weak
    assert weak == 0 or strong >= 2 * weak


@pytest.mark.asyncio
async def test_record_lookup_roundtrip(client: AsyncClient):
    _, user_id, entity_id = await _register_owner(client, "tpm_rt")
    import packages.core.database as dbmod

    async with dbmod.async_session() as db:
        await tpm.record_success(
            db, entity_id=entity_id, user_id=user_id,
            user_message="发一条 x post", tool_name="mcp__twitter_x__post_tweet",
        )
        await tpm.record_success(
            db, entity_id=entity_id, user_id=user_id,
            user_message="发一条 x post", tool_name="mcp__chrome__browser_action",
        )
        await db.commit()

    paths = await tpm.lookup_paths(
        entity_id=entity_id, user_id=user_id, user_message="再发一条 x post",
    )
    names = [p.tool_name for p in paths]
    assert "mcp__twitter_x__post_tweet" in names
    assert "mcp__chrome__browser_action" in names  # both alternatives kept


@pytest.mark.asyncio
async def test_failure_suppression_and_revival(client: AsyncClient):
    _, user_id, entity_id = await _register_owner(client, "tpm_sup")
    import packages.core.database as dbmod

    async with dbmod.async_session() as db:
        await tpm.record_success(
            db, entity_id=entity_id, user_id=user_id,
            user_message="发 x post", tool_name="mcp__twitter_x__post_tweet",
        )
        await tpm.record_failure(
            db, entity_id=entity_id, user_id=user_id,
            user_message="发 x post", tool_name="mcp__twitter_x__post_tweet",
        )
        await tpm.record_failure(
            db, entity_id=entity_id, user_id=user_id,
            user_message="发 x post", tool_name="mcp__twitter_x__post_tweet",
        )
        await db.commit()

    paths = await tpm.lookup_paths(
        entity_id=entity_id, user_id=user_id, user_message="发 x post",
    )
    assert all(p.tool_name != "mcp__twitter_x__post_tweet" for p in paths)  # suppressed

    async with dbmod.async_session() as db:
        await tpm.record_success(  # fresh success revives + resets failures
            db, entity_id=entity_id, user_id=user_id,
            user_message="发 x post", tool_name="mcp__twitter_x__post_tweet",
        )
        await db.commit()
    paths = await tpm.lookup_paths(
        entity_id=entity_id, user_id=user_id, user_message="发 x post",
    )
    assert any(p.tool_name == "mcp__twitter_x__post_tweet" for p in paths)


@pytest.mark.asyncio
async def test_paraphrased_failures_still_suppress(client: AsyncClient):
    """Review #1: hints are found via fuzzy overlap against rows with a
    DIFFERENT signature (that's the whole point of the fuzzy match), but
    failure recording used to look up by the CURRENT turn's EXACT
    intent_signature only. Since real repeat messages are almost never
    verbatim (users paraphrase — 're-post that' vs 'post that again'),
    the exact-signature miss + 'never create rows from failures alone'
    guard meant a hinted path's failures were silently dropped forever,
    and it could never actually get suppressed in practice. Seed with
    '发一条 x post', then hint+fail TWICE via a paraphrase ('再发一条 x post
    吧') that shares no exact signature with the seed row but clearly
    overlaps it (overlap_score >= MATCH_THRESHOLD) — suppression must
    still kick in."""
    _, user_id, entity_id = await _register_owner(client, "tpm_paraphrase")
    import packages.core.database as dbmod

    async with dbmod.async_session() as db:
        await tpm.record_success(
            db, entity_id=entity_id, user_id=user_id,
            user_message="发一条 x post", tool_name="mcp__twitter_x__post_tweet",
        )
        await db.commit()

    # Confirm the two messages really do have different exact signatures
    # (this is the whole premise of the bug) but a real overlap.
    seed_sig = tpm.intent_signature("发一条 x post")
    paraphrase_sig = tpm.intent_signature("再发一条 x post 吧")
    assert seed_sig != paraphrase_sig
    assert tpm.overlap_score(seed_sig, paraphrase_sig) >= tpm.MATCH_THRESHOLD

    async with dbmod.async_session() as db:
        await tpm.record_failure(
            db, entity_id=entity_id, user_id=user_id,
            user_message="再发一条 x post 吧", tool_name="mcp__twitter_x__post_tweet",
        )
        await tpm.record_failure(
            db, entity_id=entity_id, user_id=user_id,
            user_message="再发一条 x post 吧", tool_name="mcp__twitter_x__post_tweet",
        )
        await db.commit()

    paths = await tpm.lookup_paths(
        entity_id=entity_id, user_id=user_id, user_message="发一条 x post",
    )
    assert all(p.tool_name != "mcp__twitter_x__post_tweet" for p in paths), (
        "paraphrased failures were not recorded against the seeded row -> "
        "never suppressed"
    )


def test_fold_path_boosts_takes_max_rank_and_clamps_negative():
    """Minor #9 (folded in): boosts = {p.provider: p.rank() for p in paths}
    is last-write-wins on a dict comprehension, not max-per-provider, and
    doesn't clamp a negative rank (a shaky/mixed-history path) to 0 —
    letting it actively penalize a provider's ranking through this
    channel instead of just not helping."""
    from datetime import datetime, timezone

    from packages.core.services.tool_path_memory import IntentPath, fold_path_boosts

    now = datetime.now(timezone.utc).isoformat()
    high = IntentPath(
        provider="twitter_x", tool_name="mcp__twitter_x__post_tweet",
        success_count=5, failure_count=0,
        last_success_at=now, last_failure_at=None,
        intent_signature="x post",
    )
    low = IntentPath(
        provider="twitter_x", tool_name="mcp__twitter_x__retweet",
        success_count=1, failure_count=0,
        last_success_at=now, last_failure_at=None,
        intent_signature="x post",
    )
    negative = IntentPath(
        provider="facebook", tool_name="mcp__facebook__create_post",
        success_count=0, failure_count=1,
        last_success_at=None, last_failure_at=now,
        intent_signature="x post",
    )

    # high listed first, low second: a naive {p.provider: p.rank() ...}
    # dict comprehension would end up with twitter_x -> low.rank() (last
    # write wins) instead of the correct max (high.rank()).
    boosts = fold_path_boosts([high, low, negative])
    # approx: rank()'s decay factor depends on live datetime.now(), so two
    # separate .rank() calls a few microseconds apart aren't bit-identical.
    assert boosts["twitter_x"] == pytest.approx(high.rank())
    assert boosts["twitter_x"] > low.rank() * 2  # unambiguously the high path, not low
    assert "facebook" not in boosts  # negative rank clamped out entirely


def test_tool_path_memory_outcome_excludes_approval_gates_and_hitl():
    """Review #2: chat_service.py's hooks used to derive recording success
    from status == 'success' alone, but runtime_tool_status_for_chat
    collapses waiting_human/rejected/blocked/cancelled/canceled into its
    UI-facing 'error' bucket right alongside real execution failures —
    and a raw __hitl__ interrupt payload parses as a clean 'success'.
    Spec §A3 explicitly excludes approval denials/cancellations/
    availability blocks from failure recording ('those say nothing about
    whether the path fits the task'), and a HITL interrupt is not a
    terminal outcome at all yet. runtime_tool_path_memory_outcome must
    tell these apart from runtime_tool_status_for_chat's collapsed
    vocabulary."""
    import json

    from packages.core.ai.runtime.streams import runtime_tool_path_memory_outcome

    # Approval-gate statuses: skip entirely (neither success nor failure).
    for status in ("waiting_human", "rejected", "blocked", "cancelled", "canceled"):
        result = json.dumps({"status": status})
        assert runtime_tool_path_memory_outcome(result) is None, status

    # A raw HITL interrupt payload: skip (not a terminal outcome yet).
    hitl_result = json.dumps({"__hitl__": True, "approvalId": "appr_1"})
    assert runtime_tool_path_memory_outcome(hitl_result) is None

    # True execution outcomes: these DO count.
    assert runtime_tool_path_memory_outcome(
        json.dumps({"status": "success"})
    ) == "success"
    assert runtime_tool_path_memory_outcome(
        json.dumps({"status": "error"})
    ) == "failure"
    assert runtime_tool_path_memory_outcome(
        json.dumps({"error": "boom"})
    ) == "failure"
    assert runtime_tool_path_memory_outcome("Tool error (x): boom") == "failure"
    assert runtime_tool_path_memory_outcome('{"count": 3}') == "success"


def test_tool_path_memory_eligible_mutation_prefixes_only():
    from packages.core.ai.agentic_loop import _tool_path_memory_eligible

    assert _tool_path_memory_eligible("mcp__twitter_x__post_tweet") is True
    assert _tool_path_memory_eligible("mcp__manor_mcp_calendar__create_booking") is True
    # read-shaped mcp tool -> not eligible
    assert _tool_path_memory_eligible("mcp__twitter_x__get_profile") is False
    assert _tool_path_memory_eligible("mcp__manor_mcp_calendar__list_bookings") is False
    # non-mcp tool -> never eligible regardless of prefix
    assert _tool_path_memory_eligible("create_task") is False


@pytest.mark.asyncio
async def test_maybe_record_only_terminal_mutations(monkeypatch):
    """Task 3 contract, per the plan: successes for any eligible mutation
    tool are recorded; read-shaped tools and non-mcp tools are never
    recorded, regardless of success; failures are recorded ONLY for tools
    that were hinted this turn (a failure of an un-hinted tool says
    nothing about the hint)."""
    from packages.core.ai import agentic_loop as al

    success_calls: list[dict] = []
    failure_calls: list[dict] = []

    async def fake_success(db, **kw):
        success_calls.append(kw)

    async def fake_failure(db, **kw):
        failure_calls.append(kw)

    monkeypatch.setattr(
        "packages.core.services.tool_path_memory.record_success", fake_success,
    )
    monkeypatch.setattr(
        "packages.core.services.tool_path_memory.record_failure", fake_failure,
    )

    common = dict(
        entity_id="ent_1", user_id="user_1", user_message="post a tweet",
    )

    # mutation-shaped mcp tool + success -> recorded
    await al._maybe_record_tool_path(
        tool_name="mcp__twitter_x__post_tweet", success=True,
        hinted_tool_names=None, **common,
    )
    assert len(success_calls) == 1
    assert success_calls[0]["tool_name"] == "mcp__twitter_x__post_tweet"

    # read-shaped tool -> not recorded even on success
    await al._maybe_record_tool_path(
        tool_name="mcp__twitter_x__get_profile", success=True,
        hinted_tool_names=None, **common,
    )
    assert len(success_calls) == 1  # unchanged

    # non-mcp tool -> not recorded even on success
    await al._maybe_record_tool_path(
        tool_name="create_task", success=True,
        hinted_tool_names=None, **common,
    )
    assert len(success_calls) == 1  # unchanged

    # mutation tool that failed, but was NOT hinted this turn -> not recorded
    await al._maybe_record_tool_path(
        tool_name="mcp__twitter_x__post_tweet", success=False,
        hinted_tool_names=None, **common,
    )
    assert failure_calls == []

    # mutation tool that failed AND was hinted this turn -> recorded
    await al._maybe_record_tool_path(
        tool_name="mcp__twitter_x__post_tweet", success=False,
        hinted_tool_names={"mcp__twitter_x__post_tweet"}, **common,
    )
    assert len(failure_calls) == 1
    assert failure_calls[0]["tool_name"] == "mcp__twitter_x__post_tweet"


@pytest.mark.asyncio
async def test_maybe_record_skips_when_context_incomplete(monkeypatch):
    """Missing entity_id/user_id/user_message must never raise — this runs
    fire-and-forget from a hot path and must degrade silently."""
    from packages.core.ai import agentic_loop as al

    calls = []

    async def fake(db, **kw):
        calls.append(kw)

    monkeypatch.setattr(
        "packages.core.services.tool_path_memory.record_success", fake,
    )

    await al._maybe_record_tool_path(
        tool_name="mcp__twitter_x__post_tweet", success=True,
        entity_id=None, user_id="user_1", user_message="post a tweet",
        hinted_tool_names=None,
    )
    await al._maybe_record_tool_path(
        tool_name="mcp__twitter_x__post_tweet", success=True,
        entity_id="ent_1", user_id=None, user_message="post a tweet",
        hinted_tool_names=None,
    )
    await al._maybe_record_tool_path(
        tool_name="mcp__twitter_x__post_tweet", success=True,
        entity_id="ent_1", user_id="user_1", user_message="",
        hinted_tool_names=None,
    )
    assert calls == []


@pytest.mark.asyncio
async def test_run_chat_message_wires_real_tool_exec_site_to_recording(
    db_session, monkeypatch,
) -> None:
    """The plan named packages/core/ai/agentic_loop.py as the call site,
    but agentic_loop()'s generic engine has no entity_id/user_id/
    active_user_message of its own (it's shared by non-chat callers) —
    those only exist as closure state inside chat_service.py's
    on_tool_end/_on_tool_end callbacks, which is where the real
    chat.tool_exec success signal (runtime_tool_status_for_chat, aliased
    there as tool_status_for_chat) is computed. This test drives the REAL
    run_chat_message -> real _on_tool_end wiring (only
    runtime_execute_chat_agent_loop is faked, and the fake simply invokes
    the on_tool_end callback it was handed — mirroring the pattern in
    tests/test_provider_runtime_approvals.py's
    test_non_stream_nested_provider_approval_is_persisted_as_hitl), to
    confirm the deviation from the plan's file assumption actually wires
    correctly end to end, not just in theory."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, patch

    from packages.core.models.base import generate_ulid
    from packages.core.models.task import Conversation
    from packages.core.services.chat_service import run_chat_message

    entity_id = generate_ulid()
    user_id = generate_ulid()
    conversation_id = generate_ulid()
    db_session.add(
        Conversation(
            id=conversation_id, entity_id=entity_id, user_id=user_id,
            title="Path memory wiring",
        )
    )
    await db_session.flush()

    recorded: list[dict] = []

    async def fake_record(**kwargs):
        recorded.append(kwargs)

    monkeypatch.setattr(
        "packages.core.ai.agentic_loop._maybe_record_tool_path", fake_record,
    )

    async def fake_context(*_args, **_kwargs):
        return (
            "system",
            [{"type": "function", "function": {"name": "mcp__twitter_x__post_tweet"}}],
            [],
            SimpleNamespace(
                workspace_id=None, task_id=None, runtime_envelope=None,
                tool_profile=None, allowed_tool_names=None,
                user=None, entity=None, hinted_tool_names=set(),
            ),
        )

    async def fake_loop(**kwargs):
        kwargs["on_tool_end"](
            "mcp__twitter_x__post_tweet", '{"status": "success"}', 12, {},
        )
        from packages.core.ai.agentic_loop import AgenticResult
        return AgenticResult(
            content="Posted.", messages=[], usage={}, rounds=1,
            tool_calls_made=["mcp__twitter_x__post_tweet"],
        )

    with (
        patch(
            "packages.core.services.chat_service.resolve_runtime_chat_context",
            new=fake_context,
        ),
        patch(
            "packages.core.services.chat_service.runtime_execute_chat_agent_loop",
            new=fake_loop,
        ),
        patch(
            "packages.core.services.model_resolver.resolve_model_for_user",
            new=AsyncMock(return_value="openai/gpt-5.5"),
        ),
        patch(
            "packages.core.services.chat_service.record_chat_llm_usage",
            new=AsyncMock(),
        ),
        patch(
            "packages.core.services.chat_service.resolve_author_subscription_id",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "packages.core.services.chat_service.record_chat_runtime_learning",
            new=AsyncMock(return_value=[]),
        ),
        patch(
            "packages.core.services.chat_service.schedule_learning_candidate_applies",
            new=AsyncMock(),
        ),
        patch(
            "packages.core.services.chat_service.runtime_persist_chat_runtime_events",
            new=AsyncMock(),
        ),
    ):
        await run_chat_message(
            "post a tweet",
            conversation_id,
            entity_id=entity_id,
            user_id=user_id,
            db=db_session,
        )
        # asyncio.create_task schedules the recording call; let it run.
        import asyncio
        await asyncio.sleep(0)

    assert len(recorded) == 1
    assert recorded[0]["tool_name"] == "mcp__twitter_x__post_tweet"
    assert recorded[0]["success"] is True
    assert recorded[0]["entity_id"] == entity_id
    assert recorded[0]["user_id"] == user_id
    assert recorded[0]["user_message"] == "post a tweet"


@pytest.mark.asyncio
async def test_approval_interrupt_and_gate_statuses_never_record(
    db_session, monkeypatch,
) -> None:
    """Review #2: a __hitl__ interrupt result must not record anything (it
    is not a terminal outcome yet — recording it as success would be
    premature and would double-count once the human actually resolves
    it), and approval-gate statuses (waiting_human/rejected/blocked/
    cancelled/canceled) must not record either (spec §A3 explicitly
    excludes approval denials/cancellations/availability blocks — 'those
    say nothing about whether the path fits the task'). Drives the same
    real run_chat_message -> real on_tool_end wiring as the sibling test
    above, once per case."""
    import json
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, patch

    from packages.core.models.base import generate_ulid
    from packages.core.models.task import Conversation
    from packages.core.services.chat_service import run_chat_message

    entity_id = generate_ulid()
    user_id = generate_ulid()

    recorded: list[dict] = []

    async def fake_record(**kwargs):
        recorded.append(kwargs)

    monkeypatch.setattr(
        "packages.core.ai.agentic_loop._maybe_record_tool_path", fake_record,
    )

    async def fake_context(*_args, **_kwargs):
        return (
            "system",
            [{"type": "function", "function": {"name": "mcp__twitter_x__post_tweet"}}],
            [],
            SimpleNamespace(
                workspace_id=None, task_id=None, runtime_envelope=None,
                tool_profile=None, allowed_tool_names=None,
                user=None, entity=None, hinted_tool_names=set(),
            ),
        )

    results_to_try = [
        json.dumps({"__hitl__": True, "approvalId": "appr_1"}),
        json.dumps({"status": "waiting_human"}),
        json.dumps({"status": "rejected"}),
        json.dumps({"status": "blocked"}),
        json.dumps({"status": "cancelled"}),
        json.dumps({"status": "canceled"}),
    ]

    for tool_result in results_to_try:

        async def fake_loop(**kwargs):
            kwargs["on_tool_end"](
                "mcp__twitter_x__post_tweet", tool_result, 12, {},
            )
            from packages.core.ai.agentic_loop import AgenticResult
            return AgenticResult(
                content="Waiting.", messages=[], usage={}, rounds=1,
                tool_calls_made=["mcp__twitter_x__post_tweet"],
            )

        conversation_id = generate_ulid()
        db_session.add(
            Conversation(
                id=conversation_id, entity_id=entity_id, user_id=user_id,
                title="Approval-gate wiring",
            )
        )
        await db_session.flush()

        with (
            patch(
                "packages.core.services.chat_service.resolve_runtime_chat_context",
                new=fake_context,
            ),
            patch(
                "packages.core.services.chat_service.runtime_execute_chat_agent_loop",
                new=fake_loop,
            ),
            patch(
                "packages.core.services.model_resolver.resolve_model_for_user",
                new=AsyncMock(return_value="openai/gpt-5.5"),
            ),
            patch(
                "packages.core.services.chat_service.record_chat_llm_usage",
                new=AsyncMock(),
            ),
            patch(
                "packages.core.services.chat_service.resolve_author_subscription_id",
                new=AsyncMock(return_value=None),
            ),
            patch(
                "packages.core.services.chat_service.record_chat_runtime_learning",
                new=AsyncMock(return_value=[]),
            ),
            patch(
                "packages.core.services.chat_service.schedule_learning_candidate_applies",
                new=AsyncMock(),
            ),
            patch(
                "packages.core.services.chat_service.runtime_persist_chat_runtime_events",
                new=AsyncMock(),
            ),
        ):
            await run_chat_message(
                "post a tweet",
                conversation_id,
                entity_id=entity_id,
                user_id=user_id,
                db=db_session,
            )
            import asyncio
            await asyncio.sleep(0)

    assert recorded == [], (
        f"expected zero recording calls across all approval/HITL cases, got {recorded}"
    )


async def _set_flag(db, key: str, *, enabled: bool) -> None:
    """Canonical test-side flag setter — same helper duplicated in
    tests/test_tool_discovery_v2.py; see that file's docstring for why
    (no service-level set_flag(); this direct FeatureFlag row upsert +
    cache bump is the pattern every test in this repo actually uses)."""
    from sqlalchemy import select

    from packages.core.models.feature_flag import FeatureFlag
    from packages.core.services import feature_flags as feature_flags_service

    flag = (await db.execute(
        select(FeatureFlag).where(FeatureFlag.key == key)
    )).scalar_one_or_none()
    if flag is None:
        db.add(FeatureFlag(key=key, description="test", default_enabled=enabled))
    else:
        flag.default_enabled = enabled
    await db.commit()
    feature_flags_service._bump_cache()


def _mock_prompt_pipeline(monkeypatch, captured: dict):
    """Mock the two heavy dependencies of resolve_runtime_chat_context
    (workspace runtime resolution + prompt assembly) so the test exercises
    the REAL function end to end for everything else — in particular the
    new A3 lookup+hint block, which needs a real DB session (tool_path_memory
    and is_enabled/resolve_usable_mcp_providers all do real SQL) — while
    keeping the heavier prompt/tool-schema machinery out of scope. Captures
    the kwargs passed to the (mocked) prompt-assembly call so the test can
    assert directly on what reached it, mirroring
    tests/test_runtime_chat_context_byok.py's established pattern."""
    from types import SimpleNamespace

    from packages.core.ai.runtime.prompt_adapter import ChatContext
    from packages.core.services import runtime_chat_context as module

    async def fake_resolve_workspace_runtime(*_args, **kwargs):
        return SimpleNamespace(
            workspace_id=kwargs.get("workspace_id"),
            tool_profile=None,
            is_master=False,
            task_id=None,
            thread_ref_kind=None,
            thread_ref_id=None,
            bound_tool_names=[],
            mcp_allowed_names=set(),
            extra_context=None,
        )

    async def fake_assemble_prompt(_db, *, request, **kwargs):
        captured["legacy_extra_context"] = kwargs.get("legacy_extra_context")
        captured["initial_extra_context"] = kwargs.get("initial_extra_context")
        ctx = ChatContext(
            db=_db,
            entity_id=request.entity_id,
            user_id=request.user_id,
            workspace_id=request.workspace_id,
            conversation_id=request.conversation_id,
        )
        return SimpleNamespace(context=ctx, tool_schemas=[], prompt="system prompt")

    async def fake_auto_skill_forced_tool_calls(*_args, **_kwargs):
        return []

    async def fake_resolve_model(*_args, **_kwargs):
        return "openai/gpt-5.5"

    async def fake_resolve_metadata(*_args, **_kwargs):
        return None

    monkeypatch.setattr(
        "packages.core.services.workspace_runtime.resolve_workspace_runtime",
        fake_resolve_workspace_runtime,
    )
    monkeypatch.setattr(module, "runtime_assemble_prompt_for_turn", fake_assemble_prompt)
    monkeypatch.setattr(module, "runtime_auto_skill_forced_tool_calls", fake_auto_skill_forced_tool_calls)
    monkeypatch.setattr(
        "packages.core.services.model_resolver.resolve_model_for_user",
        fake_resolve_model,
    )
    monkeypatch.setattr(
        "packages.core.services.model_resolver.resolve_llm_metadata_for_user",
        fake_resolve_metadata,
    )


@pytest.mark.asyncio
async def test_resolve_runtime_chat_context_surfaces_path_hint_when_flag_on(
    client: AsyncClient, monkeypatch,
) -> None:
    """Uses a first-party manor_mcp_calendar tool rather than the plan's
    example (mcp__twitter_x__post_tweet): a freshly-registered test entity
    has zero connected integrations, so ANY externally-connected provider
    (twitter_x included) correctly fails the A1 usability filter this
    lookup applies before display (spec §A3: 'Retrieved paths pass through
    the A1 usability filter before display'). Confirmed directly — with
    the plan's literal tool name, resolve_usable_mcp_providers legitimately
    returns frozenset() for this entity, so the hint is correctly empty;
    that's the intended contract, not a bug, and it's already covered by
    Part 1's usability-filter tests. A first-party tool isolates the
    behavior actually under test here (the cache-first lookup + hint
    surfacing) from that already-tested filter."""
    _, user_id, entity_id = await _register_owner(client, "tpm_ctx_on")
    import packages.core.database as dbmod
    from packages.core.services import runtime_chat_context as module

    async with dbmod.async_session() as db:
        await tpm.record_success(
            db, entity_id=entity_id, user_id=user_id,
            user_message="发 x post", tool_name="mcp__manor_mcp_calendar__update_working_hours",
        )
        await db.commit()

    async with dbmod.async_session() as db:
        await _set_flag(db, "tool_discovery_v2", enabled=True)

    captured: dict = {}
    _mock_prompt_pipeline(monkeypatch, captured)

    async with dbmod.async_session() as db:
        _prompt, _tools, _history, ctx = await module.resolve_runtime_chat_context(
            db, "再发一条 x post",
            entity_id=entity_id, user_id=user_id, conversation_id=None,
        )

    combined = (
        (captured.get("legacy_extra_context") or "")
        + (captured.get("initial_extra_context") or "")
    )
    assert "mcp__manor_mcp_calendar__update_working_hours" in combined
    assert "mcp__manor_mcp_calendar__update_working_hours" in ctx.hinted_tool_names


@pytest.mark.asyncio
async def test_resolve_runtime_chat_context_no_hint_when_flag_off(
    client: AsyncClient, monkeypatch,
) -> None:
    _, user_id, entity_id = await _register_owner(client, "tpm_ctx_off")
    import packages.core.database as dbmod
    from packages.core.services import runtime_chat_context as module

    async with dbmod.async_session() as db:
        await tpm.record_success(
            db, entity_id=entity_id, user_id=user_id,
            user_message="发 x post", tool_name="mcp__manor_mcp_calendar__update_working_hours",
        )
        await db.commit()
    # tool_discovery_v2 flag intentionally left unset (off by default).

    captured: dict = {}
    _mock_prompt_pipeline(monkeypatch, captured)

    async with dbmod.async_session() as db:
        _prompt, _tools, _history, ctx = await module.resolve_runtime_chat_context(
            db, "再发一条 x post",
            entity_id=entity_id, user_id=user_id, conversation_id=None,
        )

    combined = (
        (captured.get("legacy_extra_context") or "")
        + (captured.get("initial_extra_context") or "")
    )
    assert "mcp__manor_mcp_calendar__update_working_hours" not in combined
    assert ctx.hinted_tool_names == set()


@pytest.mark.asyncio
async def test_resolve_runtime_chat_context_no_hint_for_non_matching_message(
    client: AsyncClient, monkeypatch,
) -> None:
    _, user_id, entity_id = await _register_owner(client, "tpm_ctx_miss")
    import packages.core.database as dbmod
    from packages.core.services import runtime_chat_context as module

    async with dbmod.async_session() as db:
        await tpm.record_success(
            db, entity_id=entity_id, user_id=user_id,
            user_message="发 x post", tool_name="mcp__manor_mcp_calendar__update_working_hours",
        )
        await db.commit()
    async with dbmod.async_session() as db:
        await _set_flag(db, "tool_discovery_v2", enabled=True)

    captured: dict = {}
    _mock_prompt_pipeline(monkeypatch, captured)

    async with dbmod.async_session() as db:
        _prompt, _tools, _history, ctx = await module.resolve_runtime_chat_context(
            db, "查天气",  # unrelated message — no overlap with the seeded path
            entity_id=entity_id, user_id=user_id, conversation_id=None,
        )

    combined = (
        (captured.get("legacy_extra_context") or "")
        + (captured.get("initial_extra_context") or "")
    )
    assert "mcp__manor_mcp_calendar__update_working_hours" not in combined
    assert ctx.hinted_tool_names == set()


def _boost_synthetic_pool():
    return [
        ("mcp__facebook__create_post", {
            "function": {
                "name": "mcp__facebook__create_post",
                "description": "Create a Facebook post with extra promo text.",
            },
        }),
        ("mcp__twitter_x__post_tweet", {
            "function": {"name": "mcp__twitter_x__post_tweet", "description": "Post a tweet."},
        }),
    ]


def test_intent_path_boost_breaks_a_small_score_gap():
    """Task 5 contract (plan §Task 5, spec §A3 'ranking boost'): facebook
    naturally outscores twitter by a small margin on keyword match alone
    (query 'post extra' matches 'post' in both tool names, +'extra' only
    in facebook's description) — intent_path_boosts should be able to
    overcome that small gap in the boosted provider's favor.

    Deviations from the plan's literal test framing, both found by running
    the un-boosted baseline and inspecting scores directly rather than
    assuming:
    1. ('two providers tie') An EXACT tie isn't decided by pool/insertion
       order in this ranking — ties break on (score, provider_name) with
       reverse=True, so 'twitter_x' > 'facebook' alphabetically always
       wins an exact tie regardless of pool order. A genuine small
       nonzero gap (not a coincidental tie) is what actually demonstrates
       the boost nudging the ranking, and is the more realistic scenario
       for what the boost is for in production anyway.
    2. The per-tool final ranked score is `provider_score + tool_score`,
       and `provider_score` already includes `best_tool_score` once — so
       with one tool per provider, a raw tool-score gap between two
       single-tool providers is effectively counted TWICE in the final
       ranking, while a boost (added only inside provider_score) counts
       once. A facebook/twitter gap of 3 (via two matched name terms vs
       one) doubles to 6, comfortably beating a +5 boost — confirmed
       directly before picking a smaller, boost-beatable gap. Using a
       1-point gap (one desc-only match, not a name match) here instead,
       which doubles to ~2 and is easily overcome by +5."""
    from packages.core.ai.runtime.tool_search import runtime_search_tool_candidates

    baseline, _ = runtime_search_tool_candidates(
        tool_schemas=_boost_synthetic_pool(), query="post extra", max_results=2,
    )
    # Confirm the (small, non-boosted) gap actually favors facebook first.
    assert baseline[0]["name"].startswith("mcp__facebook__")

    boosted, _ = runtime_search_tool_candidates(
        tool_schemas=_boost_synthetic_pool(), query="post extra", max_results=2,
        intent_path_boosts={"twitter_x": 5.0},
    )
    assert boosted[0]["name"].startswith("mcp__twitter_x__")


def test_intent_path_boost_never_beats_a_strong_alias_match():
    """A strong keyword+alias match (query names the OTHER provider
    explicitly) must still win even against the max +9 boost — the boost
    is a nudge among near-ties, never a hard override."""
    from packages.core.ai.runtime.tool_search import runtime_search_tool_candidates

    results, _ = runtime_search_tool_candidates(
        tool_schemas=_boost_synthetic_pool(), query="facebook post", max_results=2,
        intent_path_boosts={"twitter_x": 5.0},
    )
    assert results[0]["name"].startswith("mcp__facebook__")


def test_intent_path_boost_is_capped_at_nine():
    """A boost value far above the cap must not be applied uncapped —
    otherwise a stale/over-confident memory could eventually out-rank a
    strong alias match too."""
    from packages.core.ai.runtime.tool_search import runtime_search_tool_candidates

    results, _ = runtime_search_tool_candidates(
        tool_schemas=_boost_synthetic_pool(), query="facebook post", max_results=2,
        intent_path_boosts={"twitter_x": 1000.0},
    )
    assert results[0]["name"].startswith("mcp__facebook__")


@pytest.mark.asyncio
async def test_recording_task_holds_a_strong_reference_until_done(monkeypatch):
    """Review #3: a bare asyncio.create_task() with no strong reference
    held elsewhere can be garbage-collected mid-DB-write — nothing in the
    calling scope keeps the Task object alive once the closure returns.
    _schedule_tool_path_recording must add the task to a module-level set
    (mirroring the existing _DETACHED_CHAT_TURNS pattern) and discard it
    only once it completes."""
    import asyncio

    from packages.core.services import chat_service

    release = asyncio.Event()
    entered = asyncio.Event()

    async def fake_record(**kwargs):
        entered.set()
        await release.wait()

    monkeypatch.setattr(
        "packages.core.ai.agentic_loop._maybe_record_tool_path", fake_record,
    )

    assert chat_service._RECORDING_TASKS == set()
    chat_service._schedule_tool_path_recording(
        tool_name="mcp__twitter_x__post_tweet",
        result='{"status": "success"}',
        entity_id="ent_1", user_id="user_1", message="post a tweet",
        ctx=None,
    )
    await entered.wait()
    # The task is mid-flight (awaiting `release`) — it must be held here.
    assert len(chat_service._RECORDING_TASKS) == 1
    task = next(iter(chat_service._RECORDING_TASKS))
    assert not task.done()

    release.set()
    await task
    assert chat_service._RECORDING_TASKS == set()  # discarded once done
