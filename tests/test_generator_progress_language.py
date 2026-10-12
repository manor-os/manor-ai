"""Exercise actual generator events without a database or model provider."""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from packages.core.i18n import get_locale, set_locale
from packages.core.services import agent_generator, skill_generator, workflow_generator


# Literal visible copy catches missing translations and English fallback labels.
LABELS = {
    "en": (
        "Designing the agent", "Matching capabilities", "Preparing preview", "Saving the agent",
        "Drafting the skill", "Reviewing and refining", "Packaging and saving",
        "Designing the workflow", "Refining the workflow", "Validating nodes",
    ),
    "zh": (
        "正在设计智能体", "正在匹配能力", "正在准备预览", "正在保存智能体",
        "正在起草技能", "正在审查与完善", "正在打包并保存",
        "正在设计工作流", "正在优化工作流", "正在验证节点",
    ),
    "es": (
        "Diseñando el agente", "Buscando capacidades adecuadas", "Preparando la vista previa", "Guardando el agente",
        "Redactando la habilidad", "Revisando y mejorando", "Empaquetando y guardando",
        "Diseñando el flujo de trabajo", "Mejorando el flujo de trabajo", "Validando los nodos",
    ),
    "fr": (
        "Conception de l'agent", "Recherche de fonctionnalités adaptées", "Préparation de l'aperçu",
        "Enregistrement de l'agent", "Rédaction de la compétence", "Révision et amélioration",
        "Préparation et enregistrement", "Conception du workflow", "Amélioration du workflow", "Validation des nœuds",
    ),
    "ja": (
        "エージェントを設計中", "機能を選定中", "プレビューを準備中", "エージェントを保存中",
        "スキルを作成中", "レビューと改善中", "パッケージ化して保存中",
        "ワークフローを設計中", "ワークフローを改善中", "ノードを検証中",
    ),
    "de": (
        "Agent wird entworfen", "Passende Fähigkeiten werden gesucht", "Vorschau wird vorbereitet",
        "Agent wird gespeichert", "Skill wird erstellt", "Überprüfung und Verbesserung",
        "Paket wird erstellt und gespeichert", "Workflow wird entworfen", "Workflow wird verbessert",
        "Knoten werden geprüft",
    ),
    "vi": (
        "Đang thiết kế tác nhân", "Đang chọn khả năng phù hợp", "Đang chuẩn bị bản xem trước", "Đang lưu tác nhân",
        "Đang soạn kỹ năng", "Đang kiểm tra và cải thiện", "Đang đóng gói và lưu",
        "Đang thiết kế quy trình", "Đang cải thiện quy trình", "Đang kiểm tra các nút",
    ),
}
KINDS = ("agent", "draft", "skill", "workflow", "workflow_edit")
BORROWED_DB = object()


def locale_kwargs(kind):
    return {"db": BORROWED_DB} if kind in {"agent", "draft", "skill"} else {}


@pytest.fixture
def generators(monkeypatch):
    previous_locale = get_locale()
    state = SimpleNamespace(locale=None)
    state.resolve = AsyncMock(side_effect=lambda user_id, **kwargs: state.locale if user_id else None)
    for module in (agent_generator, skill_generator, workflow_generator):
        monkeypatch.setattr(module, "resolve_response_locale", state.resolve)

    spec = {
        "name": "Unchanged name", "description": "Unchanged description",
        "category": "support", "tags": ["support"], "system_prompt": "Reply in German.",
    }
    state.agent_completion = AsyncMock(return_value=SimpleNamespace(content=json.dumps(spec)))
    monkeypatch.setattr(agent_generator, "runtime_execute_text_completion", state.agent_completion)
    plan = SimpleNamespace(selected_catalog_ids=[], status=SimpleNamespace(value="ready"), setup_required=[])
    catalog = SimpleNamespace(resolve=lambda ids: plan)
    monkeypatch.setattr(
        "packages.core.services.agent_capability_catalog.AgentCapabilityCatalogFactory.create",
        AsyncMock(return_value=catalog),
    )
    monkeypatch.setattr(agent_generator, "_match_agent_capabilities", AsyncMock(return_value=plan))
    state.agent = SimpleNamespace(id="agent", name=spec["name"])
    state.create_agent = AsyncMock(return_value=state.agent)
    monkeypatch.setattr("packages.core.services.agent_service.create_agent", state.create_agent)

    state.skill_completion = AsyncMock(return_value=SimpleNamespace(content=json.dumps(spec)))
    monkeypatch.setattr(skill_generator, "runtime_execute_skill_generation_completion", state.skill_completion)
    monkeypatch.setattr(
        skill_generator, "runtime_execute_skill_review_completion", AsyncMock(return_value=SimpleNamespace(content="PASS")),
    )
    state.skill = SimpleNamespace(id="skill", name=spec["name"])
    state.create_skill = AsyncMock(return_value=state.skill)
    monkeypatch.setattr("packages.core.services.skill_service.create_skill", state.create_skill)

    state.workflow = {
        "name": "Unchanged workflow",
        "steps": [{"id": "start", "type": "trigger", "name": "Start", "config": {}, "next": []}],
        "variables": {"output_language": "de"},
    }
    state.workflow_completion = AsyncMock(return_value=SimpleNamespace(content=json.dumps(state.workflow)))
    monkeypatch.setattr(workflow_generator, "runtime_execute_text_completion", state.workflow_completion)
    yield state
    set_locale(previous_locale)


def stream(kind, *, user_id="actor", owner_user_id="owner"):
    if kind == "agent":
        return agent_generator.generate_agent_streaming("请创建客服助手", "entity", BORROWED_DB, user_id=user_id or "")
    if kind == "draft":
        return agent_generator.generate_agent_draft_streaming(
            "请创建客服助手", "entity", db=BORROWED_DB, user_id=user_id or "",
        )
    if kind == "skill":
        return skill_generator.generate_skill_streaming(
            "请创建客服技能", "entity", BORROWED_DB, user_id=user_id, owner_user_id=owner_user_id,
        )
    return workflow_generator.generate_workflow_streaming(
        "请创建客服工作流", [{"id": "start", "type": "trigger"}] if kind == "workflow_edit" else None,
        "entity", user_id=user_id,
    )


def expected_steps(kind, locale, *, has_actor=True):
    labels = LABELS[locale]
    if kind in {"agent", "draft"}:
        return [labels[0], *([labels[1]] if has_actor else []), labels[2], *([labels[3]] if kind == "agent" else [])]
    if kind == "skill":
        return list(labels[4:7])
    return [labels[8 if kind == "workflow_edit" else 7], labels[9]]


@pytest.mark.asyncio
@pytest.mark.parametrize("locale", LABELS)
@pytest.mark.parametrize("kind", KINDS)
async def test_actual_generator_progress_uses_saved_actor_language(generators, locale, kind):
    generators.locale = locale
    request_locale = "en" if locale == "zh" else "zh"
    set_locale(request_locale)

    events = [event async for event in stream(kind)]

    assert [label for event, label in events if event == "step"] == expected_steps(kind, locale)
    generators.resolve.assert_awaited_once_with("actor", **locale_kwargs(kind))
    assert get_locale() == request_locale
    # Translations affect narration only; explicit output instructions and data survive.
    if kind == "agent":
        assert events[-1] == ("agent", generators.agent)
        assert generators.create_agent.await_args.kwargs["system_prompt"] == "Reply in German."
    elif kind == "draft":
        assert events[-1][0] == "draft"
        assert events[-1][1]["system_prompt"] == "Reply in German."
        generators.create_agent.assert_not_awaited()
    elif kind == "skill":
        assert events[-1] == ("skill", generators.skill)
        assert generators.create_skill.await_args.kwargs["system_prompt"] == "Reply in German."
        assert generators.skill_completion.await_args.kwargs["user_id"] == "actor"
    else:
        assert events[-1] == ("workflow", generators.workflow)
        assert generators.workflow_completion.await_args.kwargs["user_id"] == "actor"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("request_locale, expected_locale", [("fr", "fr"), ("unsupported", "en")])
async def test_missing_actor_falls_back_to_request_language(generators, kind, request_locale, expected_locale):
    set_locale(request_locale)
    events = [event async for event in stream(kind, user_id=None, owner_user_id=None)]
    assert [label for event, label in events if event == "step"] == expected_steps(kind, expected_locale, has_actor=False)
    generators.resolve.assert_awaited_once_with(None, **locale_kwargs(kind))


@pytest.mark.asyncio
async def test_skill_progress_uses_owner_when_triggering_actor_is_missing(generators):
    generators.locale = "ja"
    set_locale("en")
    events = [event async for event in stream("skill", user_id=None)]
    assert [label for event, label in events if event == "step"] == expected_steps("skill", "ja")
    generators.resolve.assert_awaited_once_with("owner", db=BORROWED_DB)
    assert generators.skill_completion.await_args.kwargs["user_id"] == "owner"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", KINDS)
async def test_progress_language_is_stable_within_generation_and_reloads_next_call(generators, kind):
    generators.locale = "zh"
    first_stream = stream(kind)
    first = await anext(first_stream)
    generators.locale = "fr"
    set_locale("de")
    first_events = [first, *[event async for event in first_stream]]
    assert [label for event, label in first_events if event == "step"] == expected_steps(kind, "zh")
    second_events = [event async for event in stream(kind)]
    assert [label for event, label in second_events if event == "step"] == expected_steps(kind, "fr")
    assert generators.resolve.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", KINDS)
async def test_unavailable_account_locale_uses_request_language(generators, kind):
    set_locale("vi")
    events = [event async for event in stream(kind)]
    assert [label for event, label in events if event == "step"] == expected_steps(kind, "vi")
    generators.resolve.assert_awaited_once_with("actor", **locale_kwargs(kind))
