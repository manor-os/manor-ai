"""Discovery ranks the English catalog locally; there is no model call.

The utility-model matcher it replaced took 4.4-10s per search on staging and
timed out into an unranked 25-43k character dump with zero matches.
"""
from __future__ import annotations

import json
from statistics import median
import time

import pytest

from packages.core.ai.runtime import capability_matcher, capability_ranker
from packages.core.ai.runtime.capability_ranker import (
    query_is_english,
    rank_runtime_capabilities,
    ranking_terms,
)
from packages.core.ai.runtime.tool_search import runtime_execute_search_tools_handler

CATALOG = [
    {"kind": "tool", "name": "web_search", "description": "Search the public web for current information and news."},
    {"kind": "tool", "name": "generate_image", "description": "Generate an image from a text prompt."},
    {"kind": "tool", "name": "generate_video", "description": "Generate a short video clip from a prompt or script."},
    {"kind": "tool", "name": "compose_video_timeline", "description": "Edit a video: cut, trim, add captions and music on a timeline."},
    {"kind": "tool", "name": "read_file", "description": "Read a file from the workspace knowledge."},
    {"kind": "skill", "skill_id": "s1", "name": "Weekly Report", "description": "Write a weekly business report document."},
    {"kind": "provider", "name": "gmail", "description": "Gmail: read, draft and send email messages."},
    {"kind": "provider", "name": "google_calendar", "description": "Google Calendar: list, create and update calendar events."},
    {"catalog_id": "manor.create_scheduled_job", "kind": "operation", "name": "create_scheduled_job",
     "description": "Create a recurring scheduled job that runs automatically every day or week."},
]


def _top(query: str) -> str:
    return rank_runtime_capabilities(query=query, candidates=CATALOG).matches[0]["id"]


@pytest.mark.parametrize("query,expected", [
    ("send email to a customer", "provider:gmail"),
    ("calendar events tomorrow", "provider:google_calendar"),
    ("make a short promo video", "tool:generate_video"),
    ("trim video and add captions", "tool:compose_video_timeline"),
    ("run this automatically every day", "manor.create_scheduled_job"),
    ("latest news search", "tool:web_search"),
    ("weekly reports", "skill:s1"),
])
def test_english_keywords_rank_the_right_capability_first(query, expected):
    assert _top(query) == expected


def test_identifiers_split_into_words_and_plurals_meet_singulars():
    assert ranking_terms("composeVideoTimeline") == ranking_terms("compose video timeline")
    assert len(ranking_terms("composeVideoTimeline")) == 3
    assert ranking_terms("send_messages") == ranking_terms("send message")
    assert ranking_terms("emails boxes categories status") == ["email", "box", "category", "status"]


def test_ing_forms_meet_the_verbs_catalog_text_uses():
    # Long requests say "logging in, filling the profile"; descriptions say "log in, fill".
    assert ranking_terms("logging filling uploading creating scheduling making embedding running") == \
        ranking_terms("log fill upload create schedule make embed run")
    # Words that only end in -ing or -s stay whole.
    assert ranking_terms("string thing news canvas") == ["string", "thing", "news", "canvas"]
    assert ranking_terms("news") != ranking_terms("new")


def test_a_capital_letter_standing_alone_is_a_name():
    catalog = [
        {"kind": "provider", "name": "twitter_x", "description": "X API v2: post tweets, read the timeline."},
        {"kind": "provider", "name": "linkedin", "description": "Publish a post to your professional network."},
    ]
    query = "Publish an X post"
    assert rank_runtime_capabilities(query=query, candidates=catalog).matches[0]["id"] == "provider:twitter_x"
    assert "x" in ranking_terms("an X post") and ranking_terms("3D model") == ["3d", "model"]
    # Lower-case letters in prose are fragments ("e.g."), and "I" and "a" are words.
    assert ranking_terms("e.g. I want a chart") == ["chart"]


def test_function_words_rare_in_the_catalog_do_not_outrank_content_words():
    catalog = [*CATALOG, {"kind": "tool", "name": "list_my_files", "description": "Lists files you own."}]
    for query, expected in (("what is on my calendar tomorrow", "provider:google_calendar"),
                            ("show me my emails", "provider:gmail")):
        assert rank_runtime_capabilities(query=query, candidates=catalog).matches[0]["id"] == expected


def test_ranking_is_a_shortlist_not_a_verdict():
    selection = rank_runtime_capabilities(query="send email", candidates=CATALOG)
    assert selection.status == "ranked" and selection.examined == len(CATALOG)
    first = selection.matches[0]["assessment"]
    assert first["source"] == "lexical_rank" and first["fit"] == "unassessed" and first["rank"] == 0
    assert "email" in first["reason"]
    assert rank_runtime_capabilities(query="xylophone", candidates=CATALOG).status == "unmatched"


def test_only_english_queries_meet_the_english_catalog():
    assert query_is_english("send email") and query_is_english("post to X, 2 times a day")
    assert not query_is_english("发送邮件给客户") and not query_is_english("  ")
    # One English brand inside another language would rank on the brand alone.
    assert not query_is_english("每天搜索 Instagram 最新帖子") and not query_is_english("añadir música")


def test_cold_ranking_scales_linearly_with_catalog_size():
    catalogs = {
        size: [{"kind": "tool", "name": f"tool_{i}",
                "description": f"Does task {i} with records, files and messages."}
               for i in range(size)] + CATALOG
        for size in (2_000, 4_000)
    }
    samples = {size: [] for size in catalogs}
    # Compare work on the same CPU. A fixed subsecond limit fails on slower
    # runners even when the algorithm is linear; a doubling catches quadratic
    # growth without measuring scheduling or unrelated threads.
    for _ in range(3):
        for size, catalog in catalogs.items():
            capability_ranker._document.cache_clear()
            capability_ranker._ranking_terms.cache_clear()
            started = time.thread_time()
            selection = rank_runtime_capabilities(query="send email messages", candidates=catalog)
            samples[size].append(time.thread_time() - started)
            assert selection.matches[0]["id"] == "provider:gmail"
    small, large = (median(samples[size]) for size in catalogs)
    assert large < 3 * small, f"Doubling the catalog increased CPU time from {small:.3f}s to {large:.3f}s"
    assert large < 5, f"Cold ranking used {large:.3f}s of CPU time"


def _schema(name, description):
    return name, {"type": "function", "function": {
        "name": name, "description": description, "parameters": {"type": "object", "properties": {}},
    }}


@pytest.mark.asyncio
async def test_search_tools_never_calls_a_model_and_returns_ranked_matches(monkeypatch):
    async def no_model(*args, **kwargs):
        raise AssertionError("discovery must not call a model")

    monkeypatch.setattr(capability_matcher, "runtime_execute_text_completion", no_model)
    tools = [_schema("create_scheduled_job", "Create recurring Workspace work that runs every day."),
             _schema("generate_image", "Generate an image from a text prompt.")]
    result = json.loads(await runtime_execute_search_tools_handler(
        arguments={"query": "run every day automatically", "kinds": ["tool"]},
        tool_schemas=tools, available_tool_names=[name for name, _ in tools],
    ))
    assert result["discovery"]["assessment_status"] == "ranked"
    assert [match["name"] for match in result["matches"]][:1] == ["create_scheduled_job"]
    assert "candidates" not in result["discovery"]


@pytest.mark.asyncio
async def test_search_tools_asks_for_english_keywords(monkeypatch):
    tools = [_schema("generate_image", "Generate an image from a text prompt.")]
    result = json.loads(await runtime_execute_search_tools_handler(
        arguments={"query": "生成一张图片"}, tool_schemas=tools, available_tool_names=["generate_image"],
    ))
    assert result["error"] == "query_not_english" and "English keywords" in result["message"]


def test_parameters_display_names_and_aliases_are_searchable():
    catalog = [
        {"kind": "tool", "name": "create_scheduled_job", "description": "Create a scheduled automation.",
         "parameters": ["cron_expr", "every_seconds", "timezone"]},
        {"kind": "tool", "name": "create_task", "description": "Create a new task."},
        {"kind": "tool", "name": "list_tasks", "description": "List tasks."},
        {"kind": "skill", "skill_id": "s9", "name": "stickman_video_creator", "display_name": "Stickman Video",
         "description": "End-to-end generator for simple stickman explainer videos."},
    ]
    assert rank_runtime_capabilities(query="cron timezone", candidates=catalog).matches[0]["id"] == "tool:create_scheduled_job"
    assert rank_runtime_capabilities(query="remind me weekly", candidates=catalog).matches[0]["id"] == "tool:create_scheduled_job"
    assert rank_runtime_capabilities(query="todo follow up", candidates=catalog).matches[0]["id"] == "tool:create_task"
    assert rank_runtime_capabilities(query="stick figure video", candidates=catalog).matches[0]["id"] == "skill:s9"


def test_composite_operations_rank_by_their_action_name():
    catalog = [{"catalog_id": "manor_action:list_scheduled_jobs", "kind": "operation", "name": "manor",
                "description": "Run a Manor operation."},
               {"catalog_id": "manor_action:create_task", "kind": "operation", "name": "manor",
                "description": "Run a Manor operation."}]
    assert rank_runtime_capabilities(query="list scheduled jobs", candidates=catalog).matches[0]["id"] == "manor_action:list_scheduled_jobs"


def test_warm_ranking_reuses_tokenized_documents():
    catalog = [{"kind": "tool", "name": f"tool_{i}", "description": f"Does task {i} with records, files and messages."}
               for i in range(2_000)] + CATALOG
    rank_runtime_capabilities(query="send email", candidates=catalog)
    cached = capability_ranker._document.cache_info()
    for query, expected in (("send email messages", "provider:gmail"),
                            ("calendar events", "provider:google_calendar"),
                            ("make a video", "tool:generate_video")):
        assert rank_runtime_capabilities(query=query, candidates=catalog).matches[0]["id"] == expected
    reused = capability_ranker._document.cache_info()
    assert reused.misses == cached.misses
    assert reused.hits - cached.hits == 3 * len(catalog)


def test_a_required_word_must_name_the_capability():
    catalog = [*CATALOG,
               {"kind": "provider", "name": "chrome", "description": "Control the user's browser: click and fill forms."},
               {"kind": "tool", "name": "browse_web", "description": "Open a page in a headless browser, e.g. Chrome."}]
    ranked = [m["id"] for m in rank_runtime_capabilities(query="+chrome fill form", candidates=catalog).matches]
    # browse_web only mentions Chrome in its description.
    assert ranked == ["provider:chrome"]
    assert [m["id"] for m in rank_runtime_capabilities(query="+X post", candidates=[
        *CATALOG, {"kind": "provider", "name": "twitter_x", "description": "Post tweets."},
    ]).matches] == ["provider:twitter_x"]
    assert rank_runtime_capabilities(query="+nonexistent send email", candidates=catalog).status == "unmatched"


def test_each_query_of_a_multi_step_search_reaches_the_shortlist():
    queries = ["send email to a customer", "trim video and add captions", "latest news search"]
    selection = rank_runtime_capabilities(query="", queries=queries, candidates=CATALOG, limit=3)
    assert [(m["id"], m["assessment"]["query"]) for m in selection.matches] == [
        ("provider:gmail", queries[0]), ("tool:compose_video_timeline", queries[1]), ("tool:web_search", queries[2]),
    ]
    single = rank_runtime_capabilities(query=queries[0], candidates=CATALOG)
    assert "query" not in single.matches[0]["assessment"]


@pytest.mark.asyncio
async def test_search_tools_takes_one_query_per_step():
    tools = [_schema("generate_image", "Generate an image from a text prompt."),
             _schema("send_email", "Send an email message."),
             _schema("web_search", "Search the web for news.")]
    names = [name for name, _ in tools]

    async def search(arguments):
        return json.loads(await runtime_execute_search_tools_handler(
            arguments=arguments, tool_schemas=tools, available_tool_names=names,
        ))

    result = await search({"queries": ["generate image", "send email", "news search"]})
    assert [(m["name"], m["assessment"]["query"]) for m in result["matches"]] == [
        ("generate_image", "generate image"), ("send_email", "send email"), ("web_search", "news search"),
    ]
    assert (await search({"queries": ["generate image", "发邮件"]}))["error"] == "query_not_english"
    # Models often add a query summarizing the request; it is ranked as one more step.
    both = await search({"queries": ["generate image", "send email"], "query": "news search"})
    assert [(m["name"], m["assessment"]["query"]) for m in both["matches"]] == [
        ("generate_image", "generate image"), ("send_email", "send email"), ("web_search", "news search"),
    ]
    assert "error" not in await search({"queries": ["send email"], "query": "send email"})
