import json
from types import SimpleNamespace

import pytest

from packages.core.dispatcher.output_coercion import coerce_step_output_for_schema
from packages.core.dispatcher.validation import SchemaError, validate_step_output


def _step(schema: dict):
    return SimpleNamespace(plan_id="plan_1", step_key="step_1", expected_output_schema=schema)


def _draft_pack_schema() -> dict:
    return {
        "type": "object",
        "required": ["files", "summary", "draft_count"],
        "properties": {
            "files": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["name", "path"],
                    "properties": {
                        "name": {"type": "string"},
                        "path": {"type": "string"},
                    },
                },
            },
            "summary": {"type": "string"},
            "draft_count": {"type": "integer"},
        },
    }


def test_coerce_output_schema_from_wrapped_json_text() -> None:
    schema = _draft_pack_schema()
    raw = {
        "value": """
        Here is the result:
        ```json
        {
          "files": [{"name": "draft-pack.md", "path": "workspace/draft-pack.md"}],
          "summary": "Prepared three social drafts.",
          "draft_count": 3
        }
        ```
        """
    }

    coerced = coerce_step_output_for_schema(schema, raw)

    validate_step_output(_step(schema), coerced)
    assert coerced["draft_count"] == 3
    assert coerced["files"][0]["path"] == "workspace/draft-pack.md"


def test_coerce_draft_pack_from_text_artifact_path_and_headings() -> None:
    schema = _draft_pack_schema()
    raw = {
        "text": """
        Saved final pack to `workspace/social/draft-pack.md`.

        Draft 1: X launch post
        Draft 2: XHS note
        Draft 3: Follow-up reply
        """
    }

    coerced = coerce_step_output_for_schema(schema, raw)

    validate_step_output(_step(schema), coerced)
    assert coerced["files"] == [{"name": "draft-pack.md", "path": "workspace/social/draft-pack.md"}]
    assert coerced["draft_count"] == 3
    assert coerced["summary"].startswith("Saved final pack")


def test_coerce_note_titles_from_text() -> None:
    schema = {
        "type": "object",
        "required": ["note_titles"],
        "properties": {
            "note_titles": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 3,
                "maxItems": 3,
            }
        },
    }
    raw = {
        "text": """
        Title: 低成本把 XHS 账号跑起来
        Title: 竞品拆解后的选品清单
        Title: 一人公司每周内容 SOP
        """
    }

    coerced = coerce_step_output_for_schema(schema, raw)

    validate_step_output(_step(schema), coerced)
    assert coerced["note_titles"] == [
        "低成本把 XHS 账号跑起来",
        "竞品拆解后的选品清单",
        "一人公司每周内容 SOP",
    ]


def test_coerce_product_angle_from_labeled_brief() -> None:
    schema = {
        "type": "object",
        "required": ["primary_angle_name", "value_proposition", "messaging_pillars"],
        "properties": {
            "primary_angle_name": {"type": "string"},
            "value_proposition": {"type": "string"},
            "messaging_pillars": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 3,
                "maxItems": 3,
            },
        },
    }
    raw = {
        "text": """
        Primary angle: Solo founder social operating system
        Value proposition: Turn scattered channel work into repeatable weekly growth loops.

        Messaging pillars:
        - Competitor signals become concrete content ideas
        - Drafts require human approval before publishing
        - Each run teaches the workspace what worked
        """
    }

    coerced = coerce_step_output_for_schema(schema, raw)

    validate_step_output(_step(schema), coerced)
    assert coerced["primary_angle_name"] == "Solo founder social operating system"
    assert len(coerced["messaging_pillars"]) == 3


def test_coerce_research_report_markdown_and_topics_from_plain_report() -> None:
    schema = {
        "type": "object",
        "required": ["research_report_markdown", "recommended_topics"],
        "properties": {
            "research_report_markdown": {"type": "string"},
            "recommended_topics": {"type": "array", "items": {"type": "string"}},
        },
    }
    raw = {
        "text": """
        # Day-Zero Competitive Research Report

        ## XHS Trends
        Solo founders are turning competitor teardown notes into weekly content systems.

        ## X Trends
        Short operational lessons and transparent build-in-public threads are winning.

        ## Recommended Topics for Next 7 Days
        - XHS: 一人公司如何用竞品评论区找到选题
        - X: The 20-minute competitor sweep that creates a week's posts
        - XHS: 从收藏夹到内容日历的自动化流程
        """,
    }

    coerced = coerce_step_output_for_schema(schema, raw)

    validate_step_output(_step(schema), coerced)
    assert coerced["research_report_markdown"].startswith("# Day-Zero")
    assert coerced["recommended_topics"] == [
        "XHS: 一人公司如何用竞品评论区找到选题",
        "X: The 20-minute competitor sweep that creates a week's posts",
        "XHS: 从收藏夹到内容日历的自动化流程",
    ]


def test_coerce_single_required_trend_report_from_plain_markdown() -> None:
    schema = {
        "type": "object",
        "required": ["trend_report"],
        "properties": {
            "trend_report": {"type": "string"},
        },
    }
    raw = {
        "text": """
        # Trend Research: Startup Founder Content

        ## Themes
        - Transparent revenue lessons with a specific operating takeaway.
        - AI workflow teardown posts that show the exact before/after.
        """,
    }

    coerced = coerce_step_output_for_schema(schema, raw)

    validate_step_output(_step(schema), coerced)
    assert coerced["trend_report"].lstrip().startswith("# Trend Research")


def test_coerce_publish_url_from_plain_confirmation_without_fabricating_text() -> None:
    schema = {
        "type": "object",
        "required": ["post_url", "post_text"],
        "properties": {
            "post_url": {"type": "string"},
            "post_text": {"type": "string"},
        },
    }
    raw = {
        "text": "Published here: https://www.linkedin.com/feed/update/urn:li:activity:12345/",
        "post_text": "Every founder hits a wall.",
    }

    coerced = coerce_step_output_for_schema(schema, raw)

    validate_step_output(_step(schema), coerced)
    assert coerced["post_url"] == "https://www.linkedin.com/feed/update/urn:li:activity:12345/"
    assert coerced["post_text"] == "Every founder hits a wall."


def test_coerce_publish_result_still_requires_real_url() -> None:
    schema = {
        "type": "object",
        "required": ["post_url", "post_text"],
        "properties": {
            "post_url": {"type": "string"},
            "post_text": {"type": "string"},
        },
    }
    raw = {
        "text": "Published successfully.",
        "post_text": "Every founder hits a wall.",
    }

    coerced = coerce_step_output_for_schema(schema, raw)

    with pytest.raises(SchemaError):
        validate_step_output(_step(schema), coerced)


def test_coerce_twitter_publish_payload_to_required_schema() -> None:
    schema = {
        "type": "object",
        "required": ["tweet_id", "published_at", "status"],
        "additionalProperties": False,
        "properties": {
            "tweet_id": {"type": "string"},
            "published_at": {"type": "string"},
            "status": {"type": "string"},
            "tweet_url": {"type": "string"},
            "post_text": {"type": "string"},
        },
    }
    raw = {
        "data": {
            "id": "1999000000000000000",
            "text": "Manor runtime shipped the weekly planning loop.",
            "edit_history_tweet_ids": ["1999000000000000000"],
        },
    }

    coerced = coerce_step_output_for_schema(schema, raw)

    validate_step_output(_step(schema), coerced)
    assert coerced["tweet_id"] == "1999000000000000000"
    assert coerced["status"] == "published"
    assert coerced["tweet_url"] == "https://x.com/i/web/status/1999000000000000000"
    assert coerced["post_text"] == "Manor runtime shipped the weekly planning loop."


def test_coerce_twitter_publish_payload_uses_platform_created_at_when_available() -> None:
    schema = {
        "type": "object",
        "required": ["tweet_id", "published_at", "status"],
        "properties": {
            "tweet_id": {"type": "string"},
            "published_at": {"type": "string"},
            "status": {"type": "string", "enum": ["published", "failed"]},
        },
    }
    raw = {
        "data": {
            "id": "1999000000000000001",
            "text": "Published with metadata.",
            "created_at": "2026-05-21T12:00:00.000Z",
            "edit_history_tweet_ids": ["1999000000000000001"],
        },
    }

    coerced = coerce_step_output_for_schema(schema, raw)

    validate_step_output(_step(schema), coerced)
    assert coerced["published_at"] == "2026-05-21T12:00:00.000Z"
    assert coerced["status"] == "published"


def test_coerce_topics_from_existing_markdown_field() -> None:
    schema = {
        "type": "object",
        "required": ["research_report_markdown", "recommended_topics"],
        "properties": {
            "research_report_markdown": {"type": "string"},
            "recommended_topics": {"type": "array", "items": {"type": "string"}},
        },
    }
    raw = {
        "research_report_markdown": """
        # Day-Zero Competitive Research Report

        ## Signals
        Competitors are getting traction with practical founder workflows.

        ## Recommended Topics
        - XHS: 用 20 分钟竞品扫描做一周选题
        - X: A solo founder's weekly content operating loop
        """,
    }

    coerced = coerce_step_output_for_schema(schema, raw)

    validate_step_output(_step(schema), coerced)
    assert coerced["recommended_topics"] == [
        "XHS: 用 20 分钟竞品扫描做一周选题",
        "X: A solo founder's weekly content operating loop",
    ]


def test_coerce_named_draft_counts_from_plain_text() -> None:
    schema = {
        "type": "object",
        "required": ["drafts_document_markdown", "draft_count", "short_form_draft_count", "x_draft_count"],
        "properties": {
            "drafts_document_markdown": {"type": "string"},
            "draft_count": {"type": "integer"},
            "short_form_draft_count": {"type": "integer"},
            "x_draft_count": {"type": "integer"},
        },
    }
    raw = {
        "text": """
        # First Draft Pack

        draft_count: 7
        short_form_draft_count: 4
        x_draft_count: 3

        Draft 1: Short-form competitor insight
        Draft 2: Founder workflow
        """,
    }

    coerced = coerce_step_output_for_schema(schema, raw)

    validate_step_output(_step(schema), coerced)
    assert coerced["draft_count"] == 7
    assert coerced["short_form_draft_count"] == 4
    assert coerced["x_draft_count"] == 3


def test_coerce_files_ignores_non_file_string_arrays() -> None:
    schema = {
        "type": "object",
        "required": ["short_form_note_count", "x_post_count", "flagged_items"],
        "properties": {
            "files": {"type": "array", "items": {"type": "object"}},
            "fs_path": {"type": "string"},
            "file_url": {"type": "string"},
            "short_form_note_count": {"type": "integer"},
            "x_post_count": {"type": "integer"},
            "flagged_items": {"type": "array", "items": {"type": "string"}},
        },
    }
    raw = {
        "fs_path": "Workspaces/demo/documents/short-form-x-draft-pack.md",
        "file_url": "01KRS9F8TV6WPNN0E5K12Z5B0Z",
        "short_form_note_count": 3,
        "x_post_count": 3,
        "flagged_items": [
            "Income claim framing (Short-form Note 1 & X Post 1): '副业收入翻了2倍' / "
            "'Side income: 2x' needs evidence before publishing.",
            "Screenshot promise: attach assets before publishing.",
        ],
    }

    coerced = coerce_step_output_for_schema(schema, raw)

    validate_step_output(_step(schema), coerced)
    assert [file["path"] for file in coerced["files"]] == [
        "Workspaces/demo/documents/short-form-x-draft-pack.md",
    ]
    assert coerced["files"][0]["url"] == "01KRS9F8TV6WPNN0E5K12Z5B0Z"
    assert coerced["flagged_items"] == raw["flagged_items"]


def test_coerce_files_as_string_paths_when_schema_requests_strings() -> None:
    schema = {
        "type": "object",
        "required": ["files", "changes_summary"],
        "properties": {
            "files": {
                "type": "array",
                "items": {"type": "string"},
            },
            "changes_summary": {"type": "string"},
        },
    }
    raw = {
        "files": [
            {"name": "pricing.md", "path": "运营与增长/documents/pricing.md"},
            {"name": "faq.md", "path": "运营与增长/documents/faq.md"},
        ],
        "changes_summary": "Created pricing and FAQ docs.",
    }

    coerced = coerce_step_output_for_schema(schema, raw)

    validate_step_output(_step(schema), coerced)
    assert coerced["files"] == [
        "运营与增长/documents/pricing.md",
        "运营与增长/documents/faq.md",
    ]


def test_coerce_required_fs_path_from_path_alias() -> None:
    schema = {
        "type": "object",
        "required": ["fs_path"],
        "properties": {"fs_path": {"type": "string"}},
    }
    raw = {"path": "Workspaces/demo/artifacts/report.md"}

    coerced = coerce_step_output_for_schema(schema, raw)

    validate_step_output(_step(schema), coerced)
    assert coerced["fs_path"] == "Workspaces/demo/artifacts/report.md"


def test_coerce_required_document_url_accepts_path_reference() -> None:
    schema = {
        "type": "object",
        "required": ["summary", "document_url"],
        "properties": {
            "summary": {"type": "string"},
            "document_url": {"type": "string"},
        },
    }
    raw = {
        "summary": "Research memo saved.",
        "path": "Workspaces/Noon Coffee/documents/research.md",
    }

    coerced = coerce_step_output_for_schema(schema, raw)

    validate_step_output(_step(schema), coerced)
    assert coerced["document_url"] == "Workspaces/Noon Coffee/documents/research.md"


def test_coerce_required_typed_url_from_generic_url_alias() -> None:
    schema = {
        "type": "object",
        "required": ["image_url"],
        "properties": {"image_url": {"type": "string"}},
    }
    raw = {"url": "/api/v1/fs/entity/images/generated.png"}

    coerced = coerce_step_output_for_schema(schema, raw)

    validate_step_output(_step(schema), coerced)
    assert coerced["image_url"] == "/api/v1/fs/entity/images/generated.png"


def test_coerce_required_fs_path_does_not_use_url_alias() -> None:
    schema = {
        "type": "object",
        "required": ["fs_path"],
        "properties": {"fs_path": {"type": "string"}},
    }
    # The /public/ fs URL is not an entity-relative workspace path, so it must
    # NOT be treated as fs_path. (The canonical /api/v1/fs/<entity>/<rel> form IS
    # derived — covered by test_coerce_files_item_derives_fs_path_from_api_fs_url.)
    raw = {"url": "/api/v1/fs/public/shared/generated.png"}

    coerced = coerce_step_output_for_schema(schema, raw)

    with pytest.raises(SchemaError):
        validate_step_output(_step(schema), coerced)


def test_coerce_files_items_project_path_alias_to_required_fs_path() -> None:
    schema = {
        "type": "object",
        "required": ["files"],
        "properties": {
            "files": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["name", "fs_path"],
                    "properties": {
                        "name": {"type": "string"},
                        "fs_path": {"type": "string"},
                    },
                },
            },
        },
    }
    raw = {
        "files": [
            {"name": "RULES.md", "path": "Workspaces/demo/documents/RULES.md"},
            {"name": "LEARNINGS.md", "path": "Workspaces/demo/documents/LEARNINGS.md"},
        ],
    }

    coerced = coerce_step_output_for_schema(schema, raw)

    validate_step_output(_step(schema), coerced)
    assert [file["fs_path"] for file in coerced["files"]] == [
        "Workspaces/demo/documents/RULES.md",
        "Workspaces/demo/documents/LEARNINGS.md",
    ]
    assert coerced["files"][0]["name"] == "RULES.md"


def test_coerce_files_items_project_url_alias_to_required_file_url() -> None:
    schema = {
        "type": "object",
        "required": ["files"],
        "properties": {
            "files": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["name", "file_url"],
                    "properties": {
                        "name": {"type": "string"},
                        "file_url": {"type": "string"},
                    },
                },
            },
        },
    }
    raw = {
        "files": [
            {"name": "generated.png", "url": "https://cdn.example.com/generated.png"},
        ],
    }

    coerced = coerce_step_output_for_schema(schema, raw)

    validate_step_output(_step(schema), coerced)
    assert coerced["files"][0]["file_url"] == "https://cdn.example.com/generated.png"


def test_coerce_files_items_required_fs_path_does_not_use_url_alias() -> None:
    schema = {
        "type": "object",
        "required": ["files"],
        "properties": {
            "files": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["name", "fs_path"],
                    "properties": {
                        "name": {"type": "string"},
                        "fs_path": {"type": "string"},
                    },
                },
            },
        },
    }
    raw = {
        "files": [
            {"name": "generated.png", "url": "https://cdn.example.com/generated.png"},
        ],
    }

    coerced = coerce_step_output_for_schema(schema, raw)

    with pytest.raises(SchemaError):
        validate_step_output(_step(schema), coerced)


def test_coercion_does_not_fabricate_missing_required_fields() -> None:
    schema = {
        "type": "object",
        "required": ["primary_angle_name", "value_proposition", "messaging_pillars"],
        "properties": {
            "primary_angle_name": {"type": "string"},
            "value_proposition": {"type": "string"},
            "messaging_pillars": {"type": "array", "items": {"type": "string"}},
        },
    }

    coerced = coerce_step_output_for_schema(schema, {"text": "Done."})

    with pytest.raises(SchemaError):
        validate_step_output(_step(schema), coerced)


def _script_batch_schema(count: int = 2) -> dict:
    return {
        "type": "object",
        "required": ["scripts", "batch_summary"],
        "properties": {
            "scripts": {
                "type": "array",
                "minItems": count,
                "maxItems": count,
                "items": {
                    "type": "object",
                    "required": ["script_id", "title", "format", "target_metric", "hypothesis"],
                    "properties": {
                        "script_id": {"type": "string"},
                        "title": {"type": "string"},
                        "format": {"type": "string"},
                        "content_pillar": {"type": "string"},
                        "target_metric": {"type": "string"},
                        "hypothesis": {"type": "string"},
                    },
                },
            },
            "batch_summary": {
                "type": "object",
                "required": ["total_scripts", "format_distribution", "recommended_publish_order"],
                "properties": {
                    "total_scripts": {"type": "number"},
                    "format_distribution": {"type": "object"},
                    "content_pillar_distribution": {"type": "object"},
                    "primary_target_metrics": {"type": "array"},
                    "top_hypotheses": {"type": "array"},
                    "recommended_publish_order": {"type": "array"},
                },
            },
        },
    }


def _script(script_id: str, *, fmt: str = "Mistake->Fix") -> dict:
    return {
        "script_id": script_id,
        "title": f"Title {script_id}",
        "format": fmt,
        "content_pillar": "AI Productivity",
        "target_metric": "saves",
        "hypothesis": f"Hypothesis {script_id}",
    }


def test_coerce_schema_matching_json_candidate_instead_of_first_embedded_object() -> None:
    schema = _script_batch_schema()
    scripts = [_script("MF-01"), _script("MF-02")]
    raw = {"text": (f"Input echo: {json.dumps(scripts[0])}\n\nFinal output:\n{json.dumps({'scripts': scripts})}")}

    coerced = coerce_step_output_for_schema(schema, raw)

    validate_step_output(_step(schema), coerced)
    assert [script["script_id"] for script in coerced["scripts"]] == ["MF-01", "MF-02"]
    assert coerced["batch_summary"]["total_scripts"] == 2
    assert coerced["batch_summary"]["recommended_publish_order"] == ["MF-01", "MF-02"]


def test_coerce_bare_script_array_to_required_scripts_wrapper() -> None:
    schema = _script_batch_schema()
    scripts = [_script("MF-01"), _script("MF-02", fmt="Checklist")]

    coerced = coerce_step_output_for_schema(schema, {"text": json.dumps(scripts)})

    validate_step_output(_step(schema), coerced)
    assert coerced["scripts"] == scripts
    assert coerced["batch_summary"]["format_distribution"] == {
        "Mistake->Fix": 1,
        "Checklist": 1,
    }


def test_single_script_is_not_promoted_to_complete_batch() -> None:
    schema = _script_batch_schema(count=30)
    coerced = coerce_step_output_for_schema(schema, {"text": json.dumps(_script("MF-01"))})

    with pytest.raises(SchemaError):
        validate_step_output(_step(schema), coerced)


def test_coerce_files_item_derives_fs_path_from_api_fs_url() -> None:
    # A files[] entry whose only path is the canonical /api/v1/fs/<entity>/<rel>
    # URL (no fs_path) must derive fs_path from it — that URL encodes the path.
    schema = {
        "type": "object",
        "required": ["files"],
        "properties": {
            "files": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["name", "fs_path"],
                    "properties": {"name": {"type": "string"}, "fs_path": {"type": "string"}},
                },
            },
        },
    }
    raw = {
        "files": [
            {
                "name": "title.png",
                "path": "/api/v1/fs/01KQAW2DS63V54M17Y2B534AKB/Workspaces/demo/images/title.png",
                "url": "/api/v1/fs/01KQAW2DS63V54M17Y2B534AKB/Workspaces/demo/images/title.png",
            },
        ],
    }
    coerced = coerce_step_output_for_schema(schema, raw)
    validate_step_output(_step(schema), coerced)
    assert coerced["files"][0]["fs_path"] == "Workspaces/demo/images/title.png"


def test_coerce_generate_video_mixed_file_refs_validate() -> None:
    # Reproduces the prod generate_video failure: the same image appears both as
    # an /api/v1/fs URL ref (no fs_path) and as an fs_path ref.
    schema = {
        "type": "object",
        "required": ["files"],
        "properties": {
            "files": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["name", "fs_path"],
                    "properties": {"name": {"type": "string"}, "fs_path": {"type": "string"}},
                },
            },
        },
    }
    raw = {
        "files": [
            {"name": "vid.mp4", "path": "Workspaces/demo/videos/vid.mp4", "fs_path": "Workspaces/demo/videos/vid.mp4"},
            {
                "name": "title.png",
                "path": "/api/v1/fs/ENT/Workspaces/demo/images/title.png",
                "url": "/api/v1/fs/ENT/Workspaces/demo/images/title.png",
            },
            {
                "name": "title.png",
                "path": "Workspaces/demo/images/title.png",
                "fs_path": "Workspaces/demo/images/title.png",
            },
        ],
    }
    coerced = coerce_step_output_for_schema(schema, raw)
    validate_step_output(_step(schema), coerced)
    assert all(f.get("fs_path") for f in coerced["files"])
