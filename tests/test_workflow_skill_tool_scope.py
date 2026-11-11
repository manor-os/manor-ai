"""A Skill run from a Workflow node keeps the declared tools it has under a Task or Chat turn.

A Skill's tool surface is its declaration within the caller's scope. A node with
an explicit ``tools`` list is that scope, for itself and every Skill it runs;
when the list was kept by hand beside the Skills, they silently lost tools the
same Skills had in a Task (2026-10-14 Video Studio eval: no music library in the
delegated Video Edit loop or the canvas step). ``validate_workflow_steps``
reports such a node, and no shipped Blueprint may contain one.
"""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from packages.core.services.builtin_skill_loader import packaged_builtin_skill_declaration
from packages.core.services.workflow_service import validate_workflow_steps

CONFIGS = Path(__file__).resolve().parents[1] / "packages/core/blueprints/configs"


def _payloads():
    for path in sorted(CONFIGS.rglob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        for payload in [data] if "manifest" in data else data.values():
            yield path.name, payload


def _resolver(payload):
    embedded = {
        skill["slug"]: SimpleNamespace(
            id=None, slug=skill["slug"], name=skill["slug"], entity_id="embedded",
            tools=list(skill.get("tools") or ()), config=dict(skill.get("config") or {}),
        )
        for skill in (payload.get("embedded") or {}).get("skills") or ()
    }
    return lambda slug: embedded.get(slug) or packaged_builtin_skill_declaration(slug)


def _withheld(steps, resolve_skill=None):
    result = validate_workflow_steps(steps, resolve_skill=resolve_skill)
    return [w["message"] for w in result["warnings"] if w["code"] == "skill_tools_withheld"]


def test_no_shipped_blueprint_node_withholds_tools_from_a_skill_it_runs():
    checked_workflows = 0
    for name, payload in _payloads():
        resolve = _resolver(payload)
        for workflow in (payload.get("recipe") or {}).get("workflows") or ():
            steps = workflow.get("steps") or []
            checked_workflows += 1
            assert _withheld(steps, resolve) == [], (name, workflow.get("slug"))
    # Public Blueprints may inherit an Agent's tool scope. Validate every shipped
    # Workflow without requiring a private Blueprint's explicit Skill-node shape.
    assert checked_workflows, "no shipped Blueprint Workflow was checked"


def _flow(node_config):
    return [
        {"id": "start", "type": "trigger", "next": ["work"], "config": {}},
        {"id": "work", "type": "agent", "next": [], "config": node_config},
    ]


VIDEO_EDIT_CALL = 'Build it: invoke_skill(skill_id="video-edit", input="cut the clips")'
EDIT_TOOLS = ["invoke_skill", "video_edit", "generate_file", "patch_file", "wait_media_jobs", "probe_media",
              "read_file", "search_documents", "list_files", "normalize_audio_loudness",
              "build_narration_timeline", "transcribe_video", "analyze_audio", "render_frame_samples",
              "measure_media_audio_alignment"]


@pytest.mark.parametrize("config,withheld", [
    ({"tools": EDIT_TOOLS, "input": VIDEO_EDIT_CALL}, ("import_free_music", "search_free_music")),
    ({"tools": [t for t in EDIT_TOOLS if t != "invoke_skill"], "skill": "video-edit"},
     ("import_free_music", "search_free_music")),
    ({"tools": [*EDIT_TOOLS, "search_free_music", "import_free_music"], "input": VIDEO_EDIT_CALL}, ()),
    # No explicit list: the node resolves from its Agent, exactly as a Task or Chat turn would.
    ({"input": VIDEO_EDIT_CALL}, ()),
    # A Skill the node cannot invoke is not one it runs.
    ({"tools": ["read_file"], "input": VIDEO_EDIT_CALL}, ()),
], ids=["prompt-invocation", "skill-node", "covered", "agent-scope", "no-invoke-skill"])
def test_a_node_that_withholds_declared_tools_from_its_skill_is_reported(config, withheld):
    messages = _withheld(_flow(config))
    if not withheld:
        assert messages == []
        return
    assert len(messages) == 1 and "'video-edit'" in messages[0]
    assert all(name in messages[0] for name in withheld)
    assert validate_workflow_steps(_flow(config))["valid"]


def _instructed_withheld(steps):
    return [w["message"] for w in validate_workflow_steps(steps)["warnings"] if w["code"] == "instructed_tools_withheld"]


@pytest.mark.parametrize("prompt,tools,withheld", [
    # Production: a node had to confirm silence and platform acceptance with tools it was not given.
    ("Confirm the final cut is silent: use analyze_audio, then verify the post with `mcp__chrome__read_page`.",
     ["read_file"], ("analyze_audio", "mcp__chrome__read_page")),
    ("Confirm the final cut is silent: use analyze_audio.", ["read_file", "analyze_audio"], ()),
    # Naming what a later step does, or forbidding a tool, is not an instruction to use it.
    ("Plan how each shot is generated (generate_video or a still). Do not call list_files.", ["read_file"], ()),
], ids=["withheld", "covered", "described-or-forbidden"])
def test_a_node_told_to_use_tools_it_lacks_is_reported(prompt, tools, withheld):
    messages = _instructed_withheld(_flow({"tools": tools, "prompt": prompt}))
    if not withheld:
        assert messages == []
        return
    assert len(messages) == 1 and all(name in messages[0] for name in withheld)


def test_no_shipped_blueprint_node_is_told_to_use_a_tool_it_lacks():
    for name, payload in _payloads():
        for workflow in (payload.get("recipe") or {}).get("workflows") or ():
            assert _instructed_withheld(workflow.get("steps") or []) == [], (name, workflow.get("slug"))
