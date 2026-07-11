#!/usr/bin/env python3
"""No-dependency smoke test for the Zhihu Chrome browser-control sequence.

The fixture mirrors the user-reported flow:

open Zhihu -> read_page -> fill search -> Enter -> read_page results ->
click result posts -> read_page each opened post.

It validates the page payload that reaches the model and separately verifies
that a bad chain is diagnosed as an action-sequencing problem, not as missing
page text.
"""
from __future__ import annotations

import json
from typing import Any


SEARCH_READ_PAGE = {
    "read_page_contract": "mcp_chrome_read_page_v1",
    "tabId": 1118233892,
    "url": "https://www.zhihu.com/search?type=content&q=AI%20%E8%A7%86%E9%A2%91%E5%89%AA%E8%BE%91%E5%B7%A5%E5%85%B7",
    "page_kind": "search_results",
    "visible_text": "\n".join(
        [
            "AI 视频剪辑工具",
            "测试了30多款AI剪辑工具后，我留下了这10个（2026版）",
            "这篇回答按自动剪辑、字幕、数字人、图文成片四类比较工具。",
            "AI视频剪辑软件有哪些好用？",
            "回答里讨论剪映、Runway、可灵、CapCut、Descript 的适用场景。",
            "相关搜索",
            "ai剪视频工具推荐",
        ]
    ),
    "result_candidates": [
        {
            "rank": 1,
            "ref": "e65",
            "candidate_kind": "content_result",
            "title": "测试了30多款AI剪辑工具后，我留下了这10个（2026版）",
            "href": "https://www.zhihu.com/question/123/answer/456",
            "context": "这篇回答按自动剪辑、字幕、数字人、图文成片四类比较工具。",
        },
        {
            "rank": 2,
            "ref": "e72",
            "candidate_kind": "content_result",
            "title": "AI视频剪辑软件有哪些好用？",
            "href": "https://www.zhihu.com/question/789/answer/101112",
            "context": "回答里讨论剪映、Runway、可灵、CapCut、Descript 的适用场景。",
        },
    ],
    "search_refinement_candidates": [
        {
            "rank": 1,
            "ref": "e88",
            "candidate_kind": "search_refinement",
            "title": "ai剪视频工具推荐",
            "href": "https://www.zhihu.com/search?type=content&q=ai%E5%89%AA%E8%A7%86%E9%A2%91%E5%B7%A5%E5%85%B7%E6%8E%A8%E8%8D%90&utm_content=search_relatedsearch&search_source=RelatedSearch",
        }
    ],
    "actionable_refs": [
        {"ref": "e31", "role": "tab", "label": "话题"},
        {"ref": "e37", "role": "button", "label": "button"},
    ],
}

ARTICLE_READ_PAGE = {
    "read_page_contract": "mcp_chrome_read_page_v1",
    "tabId": 1118233999,
    "url": "https://www.zhihu.com/question/123/answer/456",
    "page_kind": "content_page",
    "visible_text": "工具可以分为自动剪辑、字幕转写、图文成片、数字人口播和素材生成五类。中文短视频优先考虑剪映、即梦、可灵和海螺 AI。",
    "result_candidates": [],
    "search_refinement_candidates": [],
}


def _result_refs(read_page: dict[str, Any]) -> set[str]:
    return {
        str(candidate.get("ref") or "")
        for candidate in read_page.get("result_candidates", [])
        if candidate.get("ref")
    }


def _refinement_refs(read_page: dict[str, Any]) -> set[str]:
    return {
        str(candidate.get("ref") or "")
        for candidate in read_page.get("search_refinement_candidates", [])
        if candidate.get("ref")
    }


def _navigation_or_filter_refs(read_page: dict[str, Any]) -> set[str]:
    out: set[str] = set()
    for candidate in read_page.get("actionable_refs", []):
        ref = str(candidate.get("ref") or "")
        role = str(candidate.get("role") or "").lower()
        label = str(candidate.get("label") or "")
        if ref and (role == "tab" or label in {"综合", "用户", "话题", "论文", "圈子", "AI 搜索"}):
            out.add(ref)
    return out


def assert_read_page_payload_is_unambiguous(read_page: dict[str, Any]) -> None:
    if read_page.get("read_page_contract") != "mcp_chrome_read_page_v1":
        raise AssertionError("read_page_contract is missing")
    text = str(read_page.get("visible_text") or "")
    for phrase in (
        "测试了30多款AI剪辑工具后",
        "AI视频剪辑软件有哪些好用",
        "自动剪辑",
    ):
        if phrase not in text:
            raise AssertionError(f"visible_text missing page evidence: {phrase}")
    if _result_refs(read_page) != {"e65", "e72"}:
        raise AssertionError(f"unexpected result candidates: {read_page.get('result_candidates')}")
    if _refinement_refs(read_page) != {"e88"}:
        raise AssertionError("related search candidate e88 should be isolated")
    for candidate in read_page.get("result_candidates", []):
        href = str(candidate.get("href") or "")
        if "RelatedSearch" in href or "search_relatedsearch" in href:
            raise AssertionError(f"result candidate is actually a related search: {candidate}")


def validate_sequence(events: list[dict[str, Any]]) -> list[str]:
    violations: list[str] = []
    latest_read_page: dict[str, Any] | None = None
    last_tool = ""
    last_read_page_fingerprint: tuple[Any, ...] | None = None
    open_count = 0
    clicked_result_since_last_open = False

    for event in events:
        tool = str(event.get("tool") or "")
        args = event.get("args") or {}
        result = event.get("result") or {}

        if tool == "open":
            open_count += 1
            url = str(args.get("url") or "")
            if open_count > 1:
                violations.append("opened_extra_url_after_entry_page")
            if "zhihu.com/search" in url:
                violations.append("opened_synthesized_search_url")
            if clicked_result_since_last_open and ("zhuanlan.zhihu.com/p/" in url or "zhihu.com/question/" in url):
                violations.append("opened_result_url_instead_of_using_click_target_tab")
            if args.get("active") is not False:
                violations.append("open_should_use_active_false")

        if tool == "activate_tab":
            violations.append("activated_tab_without_user_request")

        if tool == "claim_tab":
            violations.append("called_claim_tab_in_skill_flow")

        if tool == "screenshot":
            violations.append("screenshot_without_visual_request")

        if tool == "press_key" and str(args.get("key") or "") == "Return":
            violations.append("used_return_key_instead_of_enter")

        if tool == "read_page":
            fingerprint = (result.get("tabId"), result.get("url"), result.get("page_kind"))
            if last_tool == "read_page" and fingerprint == last_read_page_fingerprint:
                violations.append("repeated_read_page_without_state_change")
            latest_read_page = result
            last_read_page_fingerprint = fingerprint

        if tool in {"read_page", "click_element", "fill_or_select", "press_key", "scroll"} and "tabId" not in args:
            violations.append(f"{tool}_missing_explicit_tabId")

        if tool == "click_element":
            ref = str(args.get("ref") or "")
            if latest_read_page is None:
                violations.append("click_without_read_page")
            else:
                if ref in _refinement_refs(latest_read_page):
                    violations.append("clicked_search_refinement_instead_of_result")
                if ref in _navigation_or_filter_refs(latest_read_page):
                    violations.append("clicked_navigation_or_filter_tab_instead_of_result")
                if latest_read_page.get("page_kind") == "search_results" and ref not in _result_refs(latest_read_page):
                    violations.append("clicked_non_result_on_search_page")
                if latest_read_page.get("page_kind") == "search_results" and ref in _result_refs(latest_read_page):
                    clicked_result_since_last_open = True

        if tool == "read_page" and event.get("expected_tabId") is not None and args.get("tabId") != event["expected_tabId"]:
            violations.append("read_page_did_not_claim_target_tab")

        last_tool = tool

    return sorted(set(violations))


def main() -> int:
    assert_read_page_payload_is_unambiguous(SEARCH_READ_PAGE)

    healthy = [
        {"tool": "open", "args": {"url": "https://www.zhihu.com/", "active": False}, "result": {"tabId": 1118233892}},
        {"tool": "read_page", "args": {"tabId": 1118233892}, "result": SEARCH_READ_PAGE},
        {"tool": "click_element", "args": {"tabId": 1118233892, "ref": "e65"}, "result": {"acted_tab_id": 1118233892, "target_tab_id": 1118233999, "opened_new_tab": True}},
        {"tool": "read_page", "args": {"tabId": 1118233999}, "expected_tabId": 1118233999, "result": ARTICLE_READ_PAGE},
        {"tool": "read_page", "args": {"tabId": 1118233892}, "result": SEARCH_READ_PAGE},
        {"tool": "click_element", "args": {"tabId": 1118233892, "ref": "e72"}, "result": {"acted_tab_id": 1118233892, "target_tab_id": 1118234001, "opened_new_tab": True}},
    ]
    healthy_violations = validate_sequence(healthy)
    if healthy_violations:
        raise AssertionError(f"healthy sequence should pass: {healthy_violations}")

    bad = [
        {"tool": "open", "args": {"url": "https://www.zhihu.com/", "active": True}, "result": {"tabId": 1118233892}},
        {"tool": "read_page", "args": {"tabId": 1118233892}, "result": SEARCH_READ_PAGE},
        {"tool": "read_page", "args": {"tabId": 1118233892, "maxChars": 30000}, "result": SEARCH_READ_PAGE},
        {"tool": "screenshot", "args": {"tabId": 1118233892}, "result": {"tabId": 1118233892}},
        {"tool": "activate_tab", "args": {"tabId": 1118233892}, "result": {"tabId": 1118233892}},
        {"tool": "claim_tab", "args": {"tabId": 1118233892}, "result": {"ok": False, "error": "Unknown browser tool: browser_claim_tab"}},
        {"tool": "press_key", "args": {"tabId": 1118233892, "key": "Return"}, "result": {"ok": False}},
        {"tool": "click_element", "args": {"ref": "e88"}, "result": {"acted_tab_id": 1118233892, "target_tab_id": 1118233892}},
        {"tool": "click_element", "args": {"tabId": 1118233892, "ref": "e31"}, "result": {"acted_tab_id": 1118233892, "target_tab_id": 1118233892}},
        {"tool": "click_element", "args": {"tabId": 1118233892, "ref": "e65"}, "result": {"acted_tab_id": 1118233892, "target_tab_id": 1118233999, "opened_new_tab": True}},
        {"tool": "open", "args": {"url": "https://zhuanlan.zhihu.com/p/2006508170676285735"}, "result": {"tabId": 1118234002}},
        {"tool": "read_page", "args": {"tabId": 1118233892}, "expected_tabId": 1118233999, "result": SEARCH_READ_PAGE},
    ]
    bad_violations = validate_sequence(bad)
    expected = {
        "activated_tab_without_user_request",
        "called_claim_tab_in_skill_flow",
        "clicked_navigation_or_filter_tab_instead_of_result",
        "repeated_read_page_without_state_change",
        "click_element_missing_explicit_tabId",
        "clicked_search_refinement_instead_of_result",
        "clicked_non_result_on_search_page",
        "open_should_use_active_false",
        "opened_extra_url_after_entry_page",
        "opened_result_url_instead_of_using_click_target_tab",
        "screenshot_without_visual_request",
        "read_page_did_not_claim_target_tab",
        "used_return_key_instead_of_enter",
    }
    if not expected.issubset(set(bad_violations)):
        raise AssertionError(f"bad sequence did not expose expected problems: {bad_violations}")

    print(
        json.dumps(
            {
                "status": "ok",
                "read_page_payload": "unambiguous",
                "healthy_sequence": "passes",
                "diagnosed_bad_sequence": bad_violations,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
