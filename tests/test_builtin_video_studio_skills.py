"""Guards for the public video skills.

The playbook skill ships in every edition, so these guards derive the video
type ids from its bundled ``references/<type_id>.md`` files. Only the Cloud
Video Studio catalogue contract (one playbook per selectable type, matching
labels and defaults) needs the catalogue itself.
"""
import json
import re
from pathlib import Path

from packages.core.ai.runtime.tool_registry import runtime_tool_schema
from packages.core.ai.runtime.skills import (
    runtime_prepare_prompt_skill_tool_surface,
    runtime_skill_tools_withheld_from_surface,
)
from packages.core.ai.runtime.tool_visibility import resolve_runtime_tool_surface
from packages.core.services.builtin_skill_loader import packaged_builtin_skill_declaration
from packages.core.services.builtin_skill_loader import _builtin_skill_dirs, _read_builtin_skill_extra_files

SKILLS_ROOT = Path(__file__).resolve().parents[1] / "packages/core/ai/skills"
PLAYBOOKS_ROOT = SKILLS_ROOT / "video-type-playbooks"


def _config(slug):
    return json.loads((SKILLS_ROOT / slug / "config.json").read_text(encoding="utf-8"))


def _playbooks():
    """``{type_id: reference path}`` for every bundled playbook."""
    refs = _read_builtin_skill_extra_files(PLAYBOOKS_ROOT)
    return {Path(ref).stem: ref for ref in sorted(refs)}


def test_playbook_references_are_flat_markdown_named_by_type_id():
    refs = _read_builtin_skill_extra_files(PLAYBOOKS_ROOT)
    assert refs
    for ref in refs:
        assert re.fullmatch(r"references/[a-z0-9_]+\.md", ref), ref


def test_playbook_skill_is_instructions_only_guidance():
    cfg = _config("video-type-playbooks")
    assert cfg["type"] == "runtime_guidance"
    assert cfg["execution_mode"] == "instructions_only"
    assert cfg["bundle_roots"] == ["references"]
    assert cfg["category"] == "video"
    assert "video-type-playbooks" in {p.name for p in _builtin_skill_dirs(skills_root=SKILLS_ROOT)}


# Guards beyond the catalogue contract: the defaults a playbook advertises must
# match the catalogue, and the playbooks may only name tools Manor actually
# registers (resolved from the live tool schemas, not a hand-kept list).
FORBIDDEN_TERMS = ("Kokoro", "OpenRouter", "Whisper", "Pexels", "Mixkit", "Pixabay", "npx ", "remove-background")
_CALL = re.compile(r"\b([a-z][a-z0-9]*(?:_[a-z0-9]+)+)\(")
# Only call-shaped tokens and backticked identifiers are checked; bare, un-backticked
# snake_case words in prose (e.g. timestamps_available=false) are intentionally not.
_BACKTICKED = re.compile(r"`([a-z][a-z0-9]*(?:_[a-z0-9]+)+)`")


def _schema_param_names(schema):
    """Every property name in a JSON schema: top level, array items and nested objects."""
    names = set()
    if not isinstance(schema, dict):
        return names
    for name, sub in (schema.get("properties") or {}).items():
        names.add(name)
        names |= _schema_param_names(sub)
    names |= _schema_param_names(schema.get("items"))
    for key in ("anyOf", "oneOf", "allOf"):
        for sub in schema.get(key) or []:
            names |= _schema_param_names(sub)
    return names


def _skill_texts():
    yield "SKILL.md", (PLAYBOOKS_ROOT / "SKILL.md").read_text(encoding="utf-8")
    for type_id, ref in _playbooks().items():
        yield type_id, (PLAYBOOKS_ROOT / ref).read_text(encoding="utf-8")


def _named_tools(text):
    """Registered tools a text names, either call-shaped or backticked."""
    tokens = set(_CALL.findall(text)) | set(_BACKTICKED.findall(text))
    return {tok for tok in tokens if runtime_tool_schema(tok) is not None}


def _assert_names_only_registered_tools(name, text):
    """Call-shaped tokens must be registered tools; backticked snake_case must be a tool or its parameter."""
    type_ids = set(_playbooks())
    called = set(_CALL.findall(text))
    missing = sorted(tool for tool in called if runtime_tool_schema(tool) is None)
    assert not missing, (name, missing)
    backticked = set(_BACKTICKED.findall(text))
    tools = _named_tools(text)
    params = set()
    for tool in tools:
        params |= _schema_param_names(runtime_tool_schema(tool)["function"]["parameters"])
    unknown = sorted(backticked - tools - params - type_ids)
    assert not unknown, (name, "backticked identifier is neither a registered tool nor its parameter", unknown)
    for term in FORBIDDEN_TERMS:
        assert term not in text, (name, term)


def test_playbooks_name_only_registered_manor_tools():
    for name, text in _skill_texts():
        _assert_names_only_registered_tools(name, text)

MOTION_DESIGN_REFERENCES = ("determinism", "gsap-timelines", "threejs", "captions-and-audio", "transitions", "safe-zones")


def _motion_design_texts():
    root = SKILLS_ROOT / "motion-design"
    yield "SKILL.md", (root / "SKILL.md").read_text(encoding="utf-8")
    for ref in MOTION_DESIGN_REFERENCES:
        yield ref, (root / "references" / f"{ref}.md").read_text(encoding="utf-8")


def test_motion_design_skill_is_packaged_and_linked_from_video_edit():
    cfg = _config("motion-design")
    assert cfg["type"] == "runtime_guidance" and cfg["execution_mode"] == "instructions_only"
    assert cfg["bundle_roots"] == ["references"]
    assert cfg["category"] == "video"
    assert "motion-design" in {p.name for p in _builtin_skill_dirs(skills_root=SKILLS_ROOT)}
    refs = _read_builtin_skill_extra_files(SKILLS_ROOT / "motion-design")
    assert set(refs) == {f"references/{n}.md" for n in MOTION_DESIGN_REFERENCES}
    assert "motion-design" in (SKILLS_ROOT / "video-edit" / "SKILL.md").read_text(encoding="utf-8")
    # The routing line is only executable if Video Edit's nested loop can resolve and invoke skills.
    assert {"list_skills", "invoke_skill"} <= set(_config("video-edit")["tools"])


def test_motion_design_names_only_registered_tools_and_local_assets():
    for name, text in _motion_design_texts():
        _assert_names_only_registered_tools(name, text)
        assert "http://" not in text and "https://" not in text, (name, "remote URL")


def test_motion_design_names_only_tools_video_edit_can_call():
    # Video Edit applies this guidance in its own nested loop, so every tool the
    # guidance names must be on Video Edit's declared tool surface.
    declared = set(_config("video-edit")["tools"])
    for name, text in _motion_design_texts():
        undeclared = sorted(_named_tools(text) - declared)
        assert not undeclared, (name, "named tool is not declared by video-edit", undeclared)


def test_video_production_requires_normalized_narration_segments():
    assert {"normalize_audio_loudness", "build_narration_timeline"} <= set(_config("video-production")["tools"])


def test_video_production_uses_tools_reachable_from_its_bound_agent():
    # The Agent's public generation gateway shadows low-level image/video tools.
    # A nested production Skill must still have every declared dependency.
    skill = packaged_builtin_skill_declaration("video-production")
    registered = tuple(skill.tools)
    parent = resolve_runtime_tool_surface(
        registered, bound_tool_names=set(registered), is_master=False,
        mcp_allowed_names=set(),
    )
    child = runtime_prepare_prompt_skill_tool_surface(
        skill, allowed_tool_names=parent.visible_tool_names,
        get_registered_tool_names=lambda: registered,
    )
    assert runtime_skill_tools_withheld_from_surface(
        skill, child, registered_tool_names=registered,
    ) == ()
    assert "generate_file" in child.skill_tool_names


_INVOKE_SKILL_CALL = re.compile(r"invoke_skill\(([^)]*)\)")


def test_reference_loading_examples_use_real_invoke_skill_arguments():
    # read_file cannot open another skill's bundle; these call-shaped examples are
    # the only route to a reference, so their arguments must exist on the schema
    # and every concrete path must be a bundled file of the named skill.
    params = set(runtime_tool_schema("invoke_skill")["function"]["parameters"]["properties"])
    bundles = {
        slug: _read_builtin_skill_extra_files(SKILLS_ROOT / slug)
        for slug in ("motion-design", "video-type-playbooks")
    }
    examples = 0
    for slug in ("motion-design", "video-edit", "video-type-playbooks", "video-production"):
        text = (SKILLS_ROOT / slug / "SKILL.md").read_text(encoding="utf-8")
        for args in _INVOKE_SKILL_CALL.findall(text):
            examples += 1
            names = set(re.findall(r"\b([a-z_]+)=", args))
            assert names and names <= params, (slug, args)
            target = re.search(r'skill_id="([a-z-]+)"', args)
            reference = re.search(r'bundle_file="([^"]+)"', args)
            assert target and target.group(1) in bundles and reference, (slug, args)
            if "<" not in reference.group(1):
                assert reference.group(1) in bundles[target.group(1)], (slug, args)
    assert examples >= 4


