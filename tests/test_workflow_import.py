"""Tests for the ComfyUI / n8n / Dify workflow importers.

Covers: format auto-detection, node-type mapping, edge/connection
normalisation, graceful degradation of unmapped nodes, and the
WorkflowDefinition-shaped output.
"""
from __future__ import annotations

import json

import pytest

from packages.core.ai.workflow_import import (
    UnknownWorkflowFormat,
    detect_format,
    import_workflow,
)

# --------------------------------------------------------------------------- #
# Fixtures (minimal but structurally faithful exports)
# --------------------------------------------------------------------------- #

DIFY_DSL = """
app:
  name: Support Triage
  mode: workflow
kind: app
version: 0.1.5
workflow:
  graph:
    nodes:
      - id: "start1"
        data: {type: start, title: Start}
      - id: "kr1"
        data: {type: knowledge-retrieval, title: Lookup}
      - id: "llm1"
        data: {type: llm, title: Draft reply}
      - id: "if1"
        data: {type: if-else, title: "Confident?"}
      - id: "end1"
        data: {type: end, title: Done}
      - id: "weird1"
        data: {type: some-future-node, title: Mystery}
    edges:
      - {source: "start1", target: "kr1"}
      - {source: "kr1", target: "llm1"}
      - {source: "llm1", target: "if1"}
      - {source: "if1", target: "end1", sourceHandle: "true"}
      - {source: "if1", target: "llm1", sourceHandle: "false"}
"""

N8N_JSON = {
    "name": "Lead sync",
    "nodes": [
        {"name": "Webhook", "type": "n8n-nodes-base.webhook", "parameters": {}},
        {"name": "If", "type": "n8n-nodes-base.if", "parameters": {}},
        {"name": "Slack", "type": "n8n-nodes-base.slack", "parameters": {},
         "credentials": {"slackApi": {"id": "1", "name": "cred"}}},
        {"name": "HTTP Request", "type": "n8n-nodes-base.httpRequest", "parameters": {"url": "x"}},
        {"name": "Mystery", "type": "n8n-nodes-base.someUnknownThing", "parameters": {}},
    ],
    "connections": {
        "Webhook": {"main": [[{"node": "If", "type": "main", "index": 0}]]},
        "If": {"main": [
            [{"node": "Slack", "type": "main", "index": 0}],
            [{"node": "HTTP Request", "type": "main", "index": 0}],
        ]},
    },
}

COMFY_UI = {
    "nodes": [
        {"id": 1, "type": "CheckpointLoaderSimple"},
        {"id": 2, "type": "KSampler"},
        {"id": 3, "type": "VAEDecode"},
    ],
    "links": [
        [1, 1, 0, 2, 0, "MODEL"],
        [2, 2, 0, 3, 0, "LATENT"],
    ],
}

COMFY_API = {
    "3": {"class_type": "KSampler", "inputs": {"model": ["1", 0], "latent": ["2", 0], "seed": 42}},
    "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "sd.safetensors"}},
    "2": {"class_type": "EmptyLatentImage", "inputs": {"width": 512, "height": 512}},
}


# --------------------------------------------------------------------------- #
# Detection
# --------------------------------------------------------------------------- #

def test_detect_dify():
    assert detect_format(DIFY_DSL) == "dify"


def test_detect_n8n():
    assert detect_format(json.dumps(N8N_JSON)) == "n8n"


def test_detect_comfy_ui_and_api():
    assert detect_format(json.dumps(COMFY_UI)) == "comfyui"
    assert detect_format(json.dumps(COMFY_API)) == "comfyui"


def test_detect_unknown():
    assert detect_format('{"hello": "world"}') is None


# --------------------------------------------------------------------------- #
# Dify
# --------------------------------------------------------------------------- #

def test_dify_maps_node_types():
    result = import_workflow(DIFY_DSL)
    by_id = {n.id: n for n in result.graph.nodes}
    assert by_id["start1"].type == "trigger"
    assert by_id["kr1"].type == "rag"
    assert by_id["llm1"].type == "llm"
    assert by_id["if1"].type == "condition"
    assert by_id["end1"].type == "end"
    assert result.graph.name == "Support Triage"


def test_dify_branch_edges_split_true_false():
    result = import_workflow(DIFY_DSL)
    if1 = next(n for n in result.graph.nodes if n.id == "if1")
    assert if1.true_next == ["end1"]
    assert if1.false_next == ["llm1"]


def test_dify_unknown_node_becomes_unsupported():
    result = import_workflow(DIFY_DSL)
    weird = next(n for n in result.graph.nodes if n.id == "weird1")
    assert weird.type == "unsupported"
    assert weird.unmapped
    assert weird.meta["original_type"] == "some-future-node"
    assert result.report.unmapped_count == 1
    assert 0.0 < result.report.coverage < 1.0


# --------------------------------------------------------------------------- #
# n8n
# --------------------------------------------------------------------------- #

def test_n8n_maps_types_and_connectors():
    result = import_workflow(N8N_JSON)
    by_id = {n.id: n for n in result.graph.nodes}
    assert by_id["Webhook"].type == "trigger"
    assert by_id["If"].type == "condition"
    assert by_id["Slack"].type == "connector"
    assert by_id["HTTP Request"].type == "http"
    assert by_id["Mystery"].type == "unsupported"


def test_n8n_broad_coverage_mapping():
    """Common core nodes, the LangChain AI family, and third-party / base
    integration packages all resolve instead of degrading to 'unsupported'.
    Only a genuinely unknown *core* node stays unsupported."""
    raw = {
        "nodes": [
            {"name": "HTML", "type": "n8n-nodes-base.html", "parameters": {}},
            {"name": "Bright", "type": "n8n-nodes-brightdata.brightData", "parameters": {}},
            {"name": "Agent", "type": "@n8n/n8n-nodes-langchain.agent", "parameters": {}},
            {"name": "Model", "type": "@n8n/n8n-nodes-langchain.lmChatGoogleGemini", "parameters": {}},
            {"name": "Vec", "type": "@n8n/n8n-nodes-langchain.vectorStorePinecone", "parameters": {}},
            {"name": "Tool", "type": "@n8n/n8n-nodes-langchain.toolHttpRequest", "parameters": {}},
            {"name": "PG", "type": "n8n-nodes-base.postgres", "parameters": {}},
            {"name": "CRM", "type": "n8n-nodes-base.agileCrm", "parameters": {}},
            {"name": "Start", "type": "n8n-nodes-base.start", "parameters": {}},
            {"name": "Unknown", "type": "n8n-nodes-base.someBrandNewCoreNode", "parameters": {}},
        ],
        "connections": {},
    }
    by_id = {n.id: n.type for n in import_workflow(json.dumps(raw)).graph.nodes}
    assert by_id["HTML"] == "extract"
    assert by_id["Bright"] == "connector"      # third-party package
    assert by_id["Agent"] == "agent"
    assert by_id["Model"] == "llm"             # langchain prefix
    assert by_id["Vec"] == "rag"
    assert by_id["Tool"] == "tool"
    assert by_id["PG"] == "connector"
    assert by_id["CRM"] == "connector"         # base integration
    assert by_id["Start"] == "trigger"
    assert by_id["Unknown"] == "unsupported"   # genuine unknown core node


def test_n8n_folds_agent_subnodes():
    """n8n wires an agent's model/memory/tools in via ai_* connections. manor's
    agent is inline-config, so those sub-nodes fold into the agent and drop off
    the canvas rather than becoming disconnected islands."""
    raw = {
        "nodes": [
            {"name": "Agent", "type": "@n8n/n8n-nodes-langchain.agent",
             "parameters": {"promptType": "define", "text": "Label it",
                            "options": {"systemMessage": "You label emails."}}},
            {"name": "Model", "type": "@n8n/n8n-nodes-langchain.lmChatOpenAi",
             "parameters": {"model": {"value": "gpt-4o"}}},
            {"name": "Mem", "type": "@n8n/n8n-nodes-langchain.memoryBufferWindow", "parameters": {}},
            {"name": "ReadLabels", "type": "n8n-nodes-base.gmailTool", "parameters": {}},
            {"name": "AddLabel", "type": "n8n-nodes-base.gmailTool", "parameters": {}},
        ],
        "connections": {
            "Model": {"ai_languageModel": [[{"node": "Agent", "type": "ai_languageModel", "index": 0}]]},
            "Mem": {"ai_memory": [[{"node": "Agent", "type": "ai_memory", "index": 0}]]},
            "ReadLabels": {"ai_tool": [[{"node": "Agent", "type": "ai_tool", "index": 0}]]},
            "AddLabel": {"ai_tool": [[{"node": "Agent", "type": "ai_tool", "index": 0}]]},
        },
    }
    g = import_workflow(json.dumps(raw)).graph
    assert [n.name for n in g.nodes] == ["Start", "Agent"]
    assert g.nodes[0].next == ["Agent"]
    cfg = next(n for n in g.nodes if n.id == "Agent").config
    assert cfg["model"] == "gpt-4o"
    assert cfg["system_prompt"] == "You label emails."
    assert cfg["prompt"] == "Label it"
    assert cfg["tools"] == ["ReadLabels", "AddLabel"]
    assert cfg["memory"] is True


def test_n8n_connections_become_next():
    result = import_workflow(N8N_JSON)
    by_id = {n.id: n for n in result.graph.nodes}
    assert by_id["Webhook"].next == ["If"]
    # If has two output branches -> both targets flattened into next
    assert set(by_id["If"].next) == {"Slack", "HTTP Request"}


def test_n8n_preserves_credentials_in_config():
    result = import_workflow(N8N_JSON)
    slack = next(n for n in result.graph.nodes if n.id == "Slack")
    assert "credentials" in slack.config["n8n"]


# --------------------------------------------------------------------------- #
# ComfyUI
# --------------------------------------------------------------------------- #

def test_comfy_ui_format_collapses_to_media():
    """ComfyUI becomes an explicit trigger plus its generative media intent."""
    result = import_workflow(COMFY_UI)
    assert result.report.source_tool == "comfyui"
    nodes = result.graph.nodes
    assert [node.type for node in nodes] == ["trigger", "image"]
    assert nodes[0].next == [nodes[1].id]
    assert all(n.type != "unsupported" for n in nodes)


def test_comfy_api_collapses_with_aspect():
    """API graph (KSampler + 512×512 latent, no Save) → one image node carrying
    the recovered aspect ratio."""
    nodes = import_workflow(COMFY_API).graph.nodes
    assert [node.type for node in nodes] == ["trigger", "image"]
    assert nodes[1].config.get("aspect_ratio") == "1:1"


# --------------------------------------------------------------------------- #
# Output shape + errors
# --------------------------------------------------------------------------- #

def test_definition_output_shape():
    result = import_workflow(DIFY_DSL)
    d = result.definition
    assert d["name"] == "Support Triage"
    assert isinstance(d["steps"], list)
    step = d["steps"][0]
    assert {"id", "type", "name", "config", "next"} <= set(step.keys())
    assert d["source_tool"] == "dify"


def test_unknown_format_raises():
    with pytest.raises(UnknownWorkflowFormat):
        import_workflow('{"totally": "unrelated"}')


# --------------------------------------------------------------------------- #
# Dify config translation (data.dify -> native config)
# --------------------------------------------------------------------------- #

DIFY_RICH = """
app: {name: Rich, mode: workflow}
kind: app
version: 0.1.5
workflow:
  graph:
    edges: []
    nodes:
      - id: "llm1"
        data:
          type: llm
          title: Draft
          model: {provider: openai, name: gpt-4o, completion_params: {temperature: 0.3}}
          prompt_template:
            - {role: system, text: "You are a triage bot."}
            - {role: user, text: "Classify: {{#start1.query#}}"}
      - id: "http1"
        data:
          type: http-request
          title: Notify
          method: post
          url: "https://api.example.com/{{#start1.id#}}"
          headers: {Authorization: "Bearer {{#start1.token#}}"}
          body: {type: json, data: "{\\"x\\": 1}"}
      - id: "kr1"
        data:
          type: knowledge-retrieval
          title: KB
          query_variable_selector: ["start1", "query"]
          dataset_ids: ["ds_1"]
      - id: "code1"
        data:
          type: code
          title: Transform
          code_language: python3
          code: "def main(): return 1"
"""


def test_dify_translates_llm_config():
    result = import_workflow(DIFY_RICH)
    llm = next(n for n in result.graph.nodes if n.id == "llm1")
    assert llm.type == "llm"
    assert llm.config["model"] == "gpt-4o"
    assert llm.config["temperature"] == 0.3
    assert llm.config["system_prompt"] == "You are a triage bot."
    # Dify {{#start1.query#}} ref normalised to manor {{query}}
    assert llm.config["prompt"] == "Classify: {{query}}"
    # raw payload still preserved for traceability
    assert "dify" in llm.config


def test_dify_translates_http_config():
    result = import_workflow(DIFY_RICH)
    http = next(n for n in result.graph.nodes if n.id == "http1")
    assert http.type == "http"
    assert http.config["method"] == "POST"
    assert http.config["url"] == "https://api.example.com/{{id}}"
    assert http.config["headers"]["Authorization"] == "Bearer {{token}}"
    assert http.config["body"] == '{"x": 1}'


def test_dify_translates_rag_and_code():
    result = import_workflow(DIFY_RICH)
    rag = next(n for n in result.graph.nodes if n.id == "kr1")
    assert rag.type == "rag"
    assert rag.config["query"] == "{{query}}"
    assert rag.config["dataset_ids"] == ["ds_1"]
    code = next(n for n in result.graph.nodes if n.id == "code1")
    assert code.config["language"] == "python3"
    assert "def main" in code.config["code"]


DIFY_IFELSE_NEW = """
app: {name: Branchy, mode: workflow}
kind: app
version: 0.1.5
workflow:
  graph:
    edges: []
    nodes:
      - id: "if1"
        data:
          type: if-else
          title: Score gate
          cases:
            - case_id: "true"
              logical_operator: and
              conditions:
                - variable_selector: ["llm1", "score"]
                  comparison_operator: ">"
                  value: "0.7"
      - id: "if2"
        data:
          type: if-else
          title: Status gate
          conditions:
            - variable_selector: ["llm1", "status"]
              comparison_operator: "is"
              value: "approved"
      - id: "if3"
        data:
          type: if-else
          title: Unsupported op
          conditions:
            - variable_selector: ["llm1", "text"]
              comparison_operator: "contains"
              value: "urgent"
"""


def test_dify_ifelse_numeric_expression():
    result = import_workflow(DIFY_IFELSE_NEW)
    if1 = next(n for n in result.graph.nodes if n.id == "if1")
    assert if1.type == "condition"
    assert if1.config["expression"] == "score > 0.7"


def test_dify_ifelse_string_expression_quotes_value():
    result = import_workflow(DIFY_IFELSE_NEW)
    if2 = next(n for n in result.graph.nodes if n.id == "if2")
    assert if2.config["expression"] == 'status == "approved"'


def test_dify_ifelse_unsupported_operator_left_untranslated():
    result = import_workflow(DIFY_IFELSE_NEW)
    if3 = next(n for n in result.graph.nodes if n.id == "if3")
    # 'contains' has no native equivalent -> no expression, raw preserved
    assert "expression" not in if3.config
    assert "dify" in if3.config


def test_dify_translated_condition_evaluates_in_runner():
    """End-to-end: imported expression actually routes in the runner."""
    import asyncio
    from packages.core.ai.workflow_runner import WorkflowRunner
    from packages.core.models.workflow import WorkflowRun

    result = import_workflow(DIFY_IFELSE_NEW)
    if1 = next(n for n in result.graph.nodes if n.id == "if1")
    step = if1.to_step()
    step["true_next"] = ["a"]
    step["false_next"] = ["b"]

    runner = WorkflowRunner()
    run = WorkflowRun(id="r", workflow_id="w", entity_id="e",
                      variables={"score": 0.9}, step_results={})
    res = asyncio.run(runner._execute_step(step, run, None))
    assert res["condition_result"] is True
    assert res["next_override"] == ["a"]


# --------------------------------------------------------------------------- #
# n8n config translation (parameters -> native config)
# --------------------------------------------------------------------------- #

N8N_RICH = {
    "name": "Rich n8n",
    "nodes": [
        {"name": "HTTP", "type": "n8n-nodes-base.httpRequest", "parameters": {
            "method": "post",
            "url": "=https://api.example.com/{{ $json.id }}",
            "headerParameters": {"parameters": [{"name": "X-Key", "value": "abc"}]},
            "jsonBody": '{"a": 1}',
        }},
        {"name": "If", "type": "n8n-nodes-base.if", "parameters": {
            "conditions": {"conditions": [
                {"leftValue": "={{ $json.score }}", "rightValue": 0.7,
                 "operator": {"type": "number", "operation": "gt"}}
            ]},
        }},
        {"name": "IfV1", "type": "n8n-nodes-base.if", "parameters": {
            "conditions": {"string": [
                {"value1": "={{ $json.status }}", "operation": "equal", "value2": "approved"}
            ]},
        }},
        {"name": "Code", "type": "n8n-nodes-base.code", "parameters": {
            "jsCode": "return items;",
        }},
        {"name": "Set", "type": "n8n-nodes-base.set", "parameters": {
            "assignments": {"assignments": [{"name": "flag", "value": "=done", "type": "string"}]},
        }},
    ],
    "connections": {
        "If": {"main": [
            [{"node": "Code", "type": "main", "index": 0}],
            [{"node": "Set", "type": "main", "index": 0}],
        ]},
    },
}


def test_n8n_translates_http():
    result = import_workflow(N8N_RICH)
    http = next(n for n in result.graph.nodes if n.id == "HTTP")
    assert http.config["method"] == "POST"
    assert http.config["url"] == "https://api.example.com/{{id}}"
    assert http.config["headers"]["X-Key"] == "abc"
    assert http.config["body"] == '{"a": 1}'


def test_n8n_translates_if_v2_and_splits_branches():
    result = import_workflow(N8N_RICH)
    if_node = next(n for n in result.graph.nodes if n.id == "If")
    assert if_node.config["expression"] == "score > 0.7"
    # output0 -> true_next, output1 -> false_next
    assert if_node.true_next == ["Code"]
    assert if_node.false_next == ["Set"]


def test_n8n_translates_if_v1_string():
    result = import_workflow(N8N_RICH)
    if1 = next(n for n in result.graph.nodes if n.id == "IfV1")
    assert if1.config["expression"] == 'status == "approved"'


def test_n8n_translates_code_and_set():
    result = import_workflow(N8N_RICH)
    code = next(n for n in result.graph.nodes if n.id == "Code")
    assert code.config["code"] == "return items;"
    assert code.config["language"] == "javascript"
    set_node = next(n for n in result.graph.nodes if n.id == "Set")
    assert set_node.config["set"] == {"flag": "done"}


# --------------------------------------------------------------------------- #
# Shared connector resolution (adapter-agnostic)
# --------------------------------------------------------------------------- #

def test_n8n_connector_resolves_to_manor_tool():
    wf = {
        "name": "c", "nodes": [
            {"name": "Gmail", "type": "n8n-nodes-base.gmail",
             "parameters": {"resource": "message", "operation": "send"}},
            {"name": "Slack", "type": "n8n-nodes-base.slack",
             "parameters": {"operation": "post"}},
        ], "connections": {},
    }
    result = import_workflow(wf)
    gmail = next(n for n in result.graph.nodes if n.id == "Gmail")
    assert gmail.type == "connector"
    assert gmail.config["server_key"] == "gmail"
    assert gmail.config["tool"] == "mcp__gmail__send"
    assert gmail.config["resolved"] is True
    # manor ships no slack server -> honest unresolved, flagged for wiring
    slack = next(n for n in result.graph.nodes if n.id == "Slack")
    assert slack.config["resolved"] is False
    assert "tool" not in slack.config


DIFY_TOOL = """
app: {name: T, mode: workflow}
kind: app
version: 0.1.5
workflow:
  graph:
    edges: []
    nodes:
      - id: "tool1"
        data:
          type: tool
          title: Create issue
          provider_name: github
          tool_name: create_issue
          tool_parameters: {title: "Bug"}
"""


def test_dify_tool_resolves_to_manor_tool():
    result = import_workflow(DIFY_TOOL)
    t = next(n for n in result.graph.nodes if n.id == "tool1")
    assert t.type == "connector"
    assert t.config["server_key"] == "github"
    assert t.config["tool"] == "mcp__github__create_issue"


def test_connector_resolution_is_symmetric_across_adapters():
    """A github connector from n8n and from Dify produce the same manor shape."""
    n8n_wf = {"name": "n", "nodes": [
        {"name": "GH", "type": "n8n-nodes-base.github",
         "parameters": {"operation": "create_issue"}}], "connections": {}}
    n8n_gh = next(n for n in import_workflow(n8n_wf).graph.nodes if n.id == "GH")
    dify_gh = next(n for n in import_workflow(DIFY_TOOL).graph.nodes if n.id == "tool1")
    assert n8n_gh.type == dify_gh.type == "connector"
    assert n8n_gh.config["server_key"] == dify_gh.config["server_key"] == "github"
    assert n8n_gh.config["tool"] == dify_gh.config["tool"] == "mcp__github__create_issue"


DIFY_AGENT = """
app: {name: A, mode: workflow}
kind: app
version: 0.1.5
workflow:
  graph:
    edges: []
    nodes:
      - id: "ag1"
        data:
          type: agent
          title: Researcher
          agent_strategy_name: function_calling
          agent_parameters:
            instruction: {type: string, value: "Research {{#start1.topic#}} thoroughly."}
            query: {type: string, value: "{{#start1.topic#}}"}
            model: {type: model-selector, value: {provider: openai, model: gpt-4o}}
            maximum_iterations: {type: number, value: 10}
            tools:
              type: array
              value:
                - {tool_name: web_search}
                - {tool_name: rag}
"""


def test_dify_translates_agent_config():
    result = import_workflow(DIFY_AGENT)
    ag = next(n for n in result.graph.nodes if n.id == "ag1")
    assert ag.type == "agent"
    assert ag.config["strategy"] == "function_calling"
    assert ag.config["system_prompt"] == "Research {{topic}} thoroughly."
    assert ag.config["prompt"] == "{{topic}}"
    assert ag.config["model"] == "gpt-4o"
    assert ag.config["max_rounds"] == 10
    assert ag.config["tools"] == ["web_search", "rag"]


def test_dify_ifelse_compound_and():
    dsl = """
app: {name: C, mode: workflow}
kind: app
version: 0.1.5
workflow:
  graph:
    edges: []
    nodes:
      - id: "if1"
        data:
          type: if-else
          title: gate
          cases:
            - case_id: "true"
              logical_operator: and
              conditions:
                - {variable_selector: ["a", "score"], comparison_operator: ">", value: "0.7"}
                - {variable_selector: ["a", "status"], comparison_operator: "is", value: "ok"}
"""
    if1 = next(n for n in import_workflow(dsl).graph.nodes if n.id == "if1")
    assert if1.config["expression"] == 'score > 0.7 and status == "ok"'


def test_n8n_if_compound_or():
    wf = {"name": "c", "nodes": [
        {"name": "If", "type": "n8n-nodes-base.if", "parameters": {"conditions": {
            "combinator": "or",
            "conditions": [
                {"leftValue": "={{ $json.a }}", "rightValue": 1, "operator": {"operation": "equals"}},
                {"leftValue": "={{ $json.b }}", "rightValue": 2, "operator": {"operation": "equals"}},
            ],
        }}},
    ], "connections": {}}
    if_node = next(n for n in import_workflow(wf).graph.nodes if n.id == "If")
    assert if_node.config["expression"] == 'a == 1 or b == 2'


def test_n8n_switch_builds_cases_with_branch_targets():
    wf = {"name": "sw", "nodes": [
        {"name": "Switch", "type": "n8n-nodes-base.switch", "parameters": {"rules": {"values": [
            {"conditions": {"combinator": "and", "conditions": [
                {"leftValue": "={{ $json.tier }}", "rightValue": "vip",
                 "operator": {"operation": "equals"}}]}},
            {"conditions": {"combinator": "and", "conditions": [
                {"leftValue": "={{ $json.tier }}", "rightValue": "pro",
                 "operator": {"operation": "equals"}}]}},
        ]}}},
        {"name": "VipPath", "type": "n8n-nodes-base.noOp", "parameters": {}},
        {"name": "ProPath", "type": "n8n-nodes-base.noOp", "parameters": {}},
        {"name": "Fallback", "type": "n8n-nodes-base.noOp", "parameters": {}},
    ], "connections": {"Switch": {"main": [
        [{"node": "VipPath", "type": "main", "index": 0}],
        [{"node": "ProPath", "type": "main", "index": 0}],
        [{"node": "Fallback", "type": "main", "index": 0}],
    ]}}}
    sw = next(n for n in import_workflow(wf).graph.nodes if n.id == "Switch")
    assert sw.type == "switch"
    cases = sw.config["cases"]
    assert cases[0]["expression"] == 'tier == "vip"'
    assert cases[0]["next"] == ["VipPath"]
    assert cases[1]["expression"] == 'tier == "pro"'
    assert cases[1]["next"] == ["ProPath"]
    # third output (no rule) is the fallback / default
    assert sw.config["default_next"] == ["Fallback"]


def test_n8n_switch_routes_in_runner():
    import asyncio
    from packages.core.ai.workflow_runner import WorkflowRunner
    from packages.core.models.workflow import WorkflowRun

    wf = {"name": "sw", "nodes": [
        {"name": "Switch", "type": "n8n-nodes-base.switch", "parameters": {"rules": {"values": [
            {"conditions": {"combinator": "and", "conditions": [
                {"leftValue": "={{ $json.tier }}", "rightValue": "vip",
                 "operator": {"operation": "equals"}}]}},
        ]}}},
        {"name": "VipPath", "type": "n8n-nodes-base.noOp", "parameters": {}},
        {"name": "Fallback", "type": "n8n-nodes-base.noOp", "parameters": {}},
    ], "connections": {"Switch": {"main": [
        [{"node": "VipPath", "type": "main", "index": 0}],
        [{"node": "Fallback", "type": "main", "index": 0}],
    ]}}}
    sw = next(n for n in import_workflow(wf).graph.nodes if n.id == "Switch")
    runner = WorkflowRunner()
    run = WorkflowRun(id="r", workflow_id="w", entity_id="e",
                      variables={"tier": "vip"}, step_results={})
    res = asyncio.run(runner._execute_step(sw.to_step(), run, None))
    assert res["next_override"] == ["VipPath"]


def test_comfy_new_subgraph_format_is_flattened():
    """New ComfyUI format nests real nodes in definitions.subgraphs."""
    wf = {
        "nodes": [
            {"id": 1, "type": "SaveImage"},
            {"id": 2, "type": "sub-uuid-1"},  # subgraph instance
        ],
        "links": [],
        "definitions": {"subgraphs": [{
            "id": "sub-uuid-1",
            "nodes": [
                {"id": 10, "type": "KSampler"},
                {"id": 11, "type": "VAEDecode"},
            ],
            "links": [[1, 10, 0, 11, 0, "LATENT"]],
        }]},
    }
    nodes = import_workflow(wf).graph.nodes
    # the SaveImage output traces through the flattened subgraph and collapses
    # to one media node behind a synthesized explicit trigger.
    assert [node.type for node in nodes] == ["trigger", "image"]
    image = nodes[1]
    assert image.meta["original_type"] == "SaveImage"
    assert all(n.type != "unsupported" for n in nodes)


def test_comfy_output_nodes_map_to_media():
    wf = {"nodes": [
        {"id": 1, "type": "KSampler"},
        {"id": 2, "type": "SaveImage"},
        {"id": 3, "type": "VHS_VideoCombine"},
    ], "links": [[1, 1, 0, 2, 0, "IMAGE"], [2, 1, 0, 3, 0, "IMAGE"]]}
    types = sorted(n.type for n in import_workflow(wf).graph.nodes)
    assert types == ["image", "trigger", "video"]
    assert "unsupported" not in types    # diffusion plumbing (KSampler) dropped, not kept


def test_comfy_api_extracts_prompt_and_aspect():
    """A real t2i graph (API format): the image node recovers the positive
    prompt and aspect ratio so it actually runs in manor."""
    wf = {
        "4": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "sd.safetensors"}},
        "5": {"class_type": "EmptyLatentImage", "inputs": {"width": 1344, "height": 768}},
        "6": {"class_type": "CLIPTextEncode", "inputs": {"text": "a fox in a forest, golden hour", "clip": ["4", 1]}},
        "7": {"class_type": "CLIPTextEncode", "inputs": {"text": "blurry, lowres", "clip": ["4", 1]}},
        "3": {"class_type": "KSampler", "inputs": {
            "model": ["4", 0], "positive": ["6", 0], "negative": ["7", 0], "latent_image": ["5", 0], "seed": 42,
        }},
        "8": {"class_type": "VAEDecode", "inputs": {"samples": ["3", 0], "vae": ["4", 2]}},
        "9": {"class_type": "SaveImage", "inputs": {"images": ["8", 0]}},
    }
    img = next(n for n in import_workflow(wf).graph.nodes if n.type == "image")
    assert img.config["prompt"] == "a fox in a forest, golden hour"   # positive, not negative
    assert img.config["negative_prompt"] == "blurry, lowres"
    assert img.config["aspect_ratio"] == "16:9"                       # 1344x768 -> landscape


def test_comfy_ui_extracts_prompt_from_widgets():
    """UI format: prompt comes from CLIPTextEncode widgets_values, resolved
    through the sampler's positive slot via links."""
    wf = {"nodes": [
        {"id": 6, "type": "CLIPTextEncode", "widgets_values": ["a neon city at night"]},
        {"id": 7, "type": "CLIPTextEncode", "widgets_values": ["ugly, deformed"]},
        {"id": 5, "type": "EmptyLatentImage", "widgets_values": [768, 1344, 1]},
        {"id": 3, "type": "KSampler", "inputs": [
            {"name": "positive", "link": 60}, {"name": "negative", "link": 70}, {"name": "latent_image", "link": 50},
        ]},
        {"id": 9, "type": "SaveImage", "inputs": [{"name": "images", "link": 30}]},
    ], "links": [
        [60, 6, 0, 3, 0, "CONDITIONING"], [70, 7, 0, 3, 1, "CONDITIONING"],
        [50, 5, 0, 3, 3, "LATENT"], [30, 3, 0, 9, 0, "IMAGE"],
    ]}
    img = next(n for n in import_workflow(wf).graph.nodes if n.type == "image")
    assert img.config["prompt"] == "a neon city at night"
    assert img.config["aspect_ratio"] == "9:16"                       # 768x1344 -> portrait


def test_n8n_new_node_mappings_and_sticky_drop():
    """Real-workflow parity: n8n's aggregate/dateTime/filter/informationExtractor/
    executeWorkflow map to our dedicated nodes, and sticky notes are dropped."""
    wf = {"name": "parity", "nodes": [
        {"name": "Agg", "type": "n8n-nodes-base.aggregate", "parameters": {}},
        {"name": "When", "type": "n8n-nodes-base.dateTime", "parameters": {}},
        {"name": "Keep", "type": "n8n-nodes-base.filter", "parameters": {}},
        {"name": "Extract", "type": "@n8n/n8n-nodes-langchain.informationExtractor", "parameters": {}},
        {"name": "Sub", "type": "n8n-nodes-base.executeWorkflow", "parameters": {}},
        {"name": "Doc", "type": "n8n-nodes-base.stickyNote", "parameters": {}},
    ], "connections": {}}
    by = {n.name: n.type for n in import_workflow(wf).graph.nodes}
    assert by["Agg"] == "aggregate"
    assert by["When"] == "datetime"
    assert by["Keep"] == "filter"
    assert by["Extract"] == "extract"
    assert by["Sub"] == "subworkflow"
    assert "Doc" not in by                      # sticky note dropped


def test_comfy_note_nodes_dropped():
    wf = {"nodes": [
        {"id": 1, "type": "Note", "widgets_values": ["a comment"]},
        {"id": 2, "type": "KSampler"},
        {"id": 3, "type": "SaveImage"},
    ], "links": [[1, 2, 0, 3, 0, "IMAGE"]]}
    types = {n.meta.get("original_type") for n in import_workflow(wf).graph.nodes}
    assert "Note" not in types
    assert "SaveImage" in types


def test_n8n_id_keyed_connections_wire_up():
    """Some n8n exports key connections by node id (not name), and reference
    targets by id or name. Both must resolve to real edges; refs that match no
    node are dropped rather than producing dangling edges."""
    wf = {"name": "id-keyed", "nodes": [
        {"id": "n1", "name": "Start", "type": "n8n-nodes-base.manualTrigger", "parameters": {}},
        {"id": "n2", "name": "Fetch", "type": "n8n-nodes-base.httpRequest", "parameters": {}},
        {"id": "n3", "name": "Done", "type": "n8n-nodes-base.set", "parameters": {}},
    ], "connections": {
        "n1": {"main": [[{"node": "Fetch", "type": "main", "index": 0}]]},   # source id, target name
        "n2": {"main": [[{"node": "n3", "type": "main", "index": 0},
                         {"node": "ghost-placeholder", "type": "main", "index": 0}]]},  # one real, one dangling
    }}
    by = {n.id: n for n in import_workflow(wf).graph.nodes}
    assert by["Start"].next == ["Fetch"]
    assert by["Fetch"].next == ["Done"]        # id target resolved; dangling ref dropped


def test_n8n_no_connections_falls_back_to_layout_order():
    """A workflow with no usable connections is wired left-to-right from the
    node positions, so it imports as a runnable linear flow, not a pile."""
    wf = {"name": "scrubbed", "nodes": [
        {"name": "C", "type": "n8n-nodes-base.set", "parameters": {}, "position": [600, 0]},
        {"name": "A", "type": "n8n-nodes-base.manualTrigger", "parameters": {}, "position": [0, 0]},
        {"name": "B", "type": "n8n-nodes-base.httpRequest", "parameters": {}, "position": [300, 0]},
    ], "connections": {  # all targets are dangling placeholders
        "A": {"main": [[{"node": "error-handler-A", "type": "main", "index": 0}]]},
    }}
    by = {n.id: n for n in import_workflow(wf).graph.nodes}
    assert by["A"].next == ["B"]      # trigger first, then by x position
    assert by["B"].next == ["C"]
    assert by["C"].next == []

    # a Stop-And-Error node must NOT be wired into the inferred main chain
    wf2 = {"name": "with stop", "nodes": [
        {"name": "T", "type": "n8n-nodes-base.manualTrigger", "parameters": {}, "position": [0, 0]},
        {"name": "Work", "type": "n8n-nodes-base.set", "parameters": {}, "position": [300, 0]},
        {"name": "Err", "type": "n8n-nodes-base.stopAndError", "parameters": {}, "position": [150, 200]},
    ], "connections": {}}
    nodes2 = {n.id: n for n in import_workflow(wf2).graph.nodes}
    assert nodes2["T"].next == ["Work"]      # chain skips the stop node
    assert nodes2["Err"].next == []          # stop left disconnected


def test_n8n_news_pipeline_translates_item_stream_and_llm_prompt():
    """A real RSS-style n8n pipeline imports with executable data bindings,
    rather than only mapping the visible node types."""
    wf = {"name": "news", "nodes": [
        {"name": "Start", "type": "n8n-nodes-base.scheduleTrigger", "parameters": {}},
        {"name": "RSS", "type": "n8n-nodes-base.rssFeedRead",
         "parameters": {"url": "https://example.com/feed.xml"}},
        {"name": "Fields", "type": "n8n-nodes-base.set", "parameters": {
            "assignments": {"assignments": [
                {"name": "title", "value": "={{ $json.title }}"},
                {"name": "body", "value": "={{ $json['content:encoded'] }}"},
            ]},
        }},
        {"name": "Merge", "type": "n8n-nodes-base.merge", "parameters": {"numberInputs": 1}},
        {"name": "Filter", "type": "n8n-nodes-base.filter", "parameters": {
            "conditions": {"conditions": [{
                "leftValue": "={{ $json.pubDate }}",
                "rightValue": "={{ $today.minus({ days: 1 }) }}",
                "operator": {"type": "dateTime", "operation": "after"},
            }]},
        }},
        {"name": "Limit", "type": "n8n-nodes-base.limit", "parameters": {"maxItems": 10}},
        {"name": "Aggregate", "type": "n8n-nodes-base.aggregate", "parameters": {
            "aggregate": "aggregateAllItemData", "fieldsToInclude": "title, body",
        }},
        {"name": "Summarize", "type": "@n8n/n8n-nodes-langchain.openAi", "parameters": {
            "text": "=Summarize {{ $json.data[0].title }}",
        }},
    ], "connections": {
        "Start": {"main": [[{"node": "RSS"}]]},
        "RSS": {"main": [[{"node": "Fields"}]]},
        "Fields": {"main": [[{"node": "Merge"}]]},
        "Merge": {"main": [[{"node": "Filter"}]]},
        "Filter": {"main": [[{"node": "Limit"}]]},
        "Limit": {"main": [[{"node": "Aggregate"}]]},
        "Aggregate": {"main": [[{"node": "Summarize"}]]},
    }}

    by = {node.id: node for node in import_workflow(wf).graph.nodes}
    assert by["RSS"].config["response_format"] == "rss"
    assert by["Fields"].config["items"] == "{{RSS}}"
    assert by["Fields"].config["set"]["body"] == "{{content:encoded}}"
    assert by["Merge"].config["sources"] == ["Fields"]
    assert by["Merge"].config["flatten"] is True
    assert by["Filter"].config == {
        **by["Filter"].config,
        "field": "pubDate", "operation": "after", "relative_days": -1,
    }
    assert by["Limit"].config["items"] == "{{Filter}}"
    assert by["Limit"].config["max"] == 10
    assert by["Aggregate"].config["wrap_key"] == "data"
    assert by["Aggregate"].config["fields"] == ["title", "body"]
    assert by["Summarize"].config["inputs"][0]["value"] == "{{Aggregate}}"
    assert by["Summarize"].config["prompt"] == "Summarize {{input.data.0.title}}"


def test_n8n_popular_scrape_template_maps_item_stream_semantics():
    """The official webpage-summarizer shape imports as a real item pipeline:
    deterministic HTML extraction, Split Out, per-item HTTP/LLM, and positional
    merge all retain their n8n behavior."""
    wf = {"nodes": [
        {"name": "Start", "type": "n8n-nodes-base.manualTrigger", "parameters": {}},
        {"name": "Fetch list", "type": "n8n-nodes-base.httpRequest",
         "parameters": {"url": "https://example.com/articles"}},
        {"name": "Extract links", "type": "n8n-nodes-base.html", "parameters": {
            "extractionValues": {"values": [{
                "key": "essay", "cssSelector": "table a", "returnArray": True,
                "returnValue": "attribute", "attribute": "href",
            }]},
        }},
        {"name": "Split", "type": "n8n-nodes-base.splitOut",
         "parameters": {"fieldToSplitOut": "essay"}},
        {"name": "Fetch pages", "type": "n8n-nodes-base.httpRequest",
         "parameters": {"url": "=https://example.com/{{ $json.essay }}"}},
        {"name": "Extract body", "type": "n8n-nodes-base.html", "parameters": {
            "extractionValues": {"values": [{
                "key": "data", "cssSelector": "body", "skipSelectors": "nav",
            }]},
        }},
        {"name": "Summarize", "type": "@n8n/n8n-nodes-langchain.chainSummarization",
         "parameters": {"operationMode": "documentLoader"}},
        {"name": "Merge", "type": "n8n-nodes-base.merge",
         "parameters": {"mode": "combine", "combineBy": "combineByPosition"}},
    ], "connections": {
        "Start": {"main": [[{"node": "Fetch list"}]]},
        "Fetch list": {"main": [[{"node": "Extract links"}]]},
        "Extract links": {"main": [[{"node": "Split"}]]},
        "Split": {"main": [[{"node": "Fetch pages"}]]},
        "Fetch pages": {"main": [[{"node": "Extract body"}, {"node": "Merge"}]]},
        "Extract body": {"main": [[{"node": "Summarize"}]]},
        "Summarize": {"main": [[{"node": "Merge"}]]},
    }}

    by = {node.id: node for node in import_workflow(wf).graph.nodes}
    assert by["Extract links"].config["html_extract"][0] == {
        "key": "essay", "selector": "table a", "return_value": "attribute",
        "attribute": "href", "return_array": True, "skip_selectors": "",
    }
    assert by["Split"].type == "split"
    assert by["Split"].config["items"] == "{{Extract links}}"
    assert by["Split"].config["preserve_field"] is True
    assert by["Fetch pages"].config["url"] == "https://example.com/{{essay}}"
    assert by["Fetch pages"].config["batch"] is True
    assert by["Fetch pages"].config["items"] == "{{Split}}"
    assert by["Summarize"].config["batch"] is True
    assert by["Summarize"].config["response_wrapper"] == "response.text"
    assert by["Merge"].config["mode"] == "combine_by_position"
    assert "flatten" not in by["Merge"].config


def test_n8n_chat_agent_defaults_to_chat_trigger_payload():
    wf = {"nodes": [
        {"name": "Chat", "type": "@n8n/n8n-nodes-langchain.chatTrigger", "parameters": {}},
        {"name": "Agent", "type": "@n8n/n8n-nodes-langchain.agent", "parameters": {}},
        {"name": "Model", "type": "@n8n/n8n-nodes-langchain.lmChatOpenAi",
         "parameters": {"model": {"value": "gpt-4o-mini"}}},
    ], "connections": {
        "Chat": {"main": [[{"node": "Agent"}]]},
        "Model": {"ai_languageModel": [[{"node": "Agent"}]]},
    }}
    by = {node.id: node for node in import_workflow(wf).graph.nodes}
    assert by["Agent"].config["prompt"] == "{{input.chatInput}}"
    assert by["Agent"].config["inputs"] == [
        {"key": "input", "value": "{{Chat}}", "type": "any"},
    ]


def test_n8n_form_trigger_preserves_run_input_schema():
    wf = {"nodes": [
        {"name": "Website form", "type": "n8n-nodes-base.formTrigger", "parameters": {
            "formFields": {"values": [
                {
                    "fieldLabel": "Landing Page Url",
                    "placeholder": "https://example.com",
                    "requiredField": True,
                },
                {
                    "fieldLabel": "Max pages",
                    "fieldType": "number",
                    "defaultValue": 5,
                    "requiredField": False,
                },
            ]},
        }},
    ], "connections": {}}

    trigger = import_workflow(wf).graph.nodes[0]
    assert trigger.config["inputs"] == [
        {
            "key": "Landing Page Url",
            "label": "Landing Page Url",
            "type": "string",
            "required": True,
            "placeholder": "https://example.com",
        },
        {
            "key": "Max pages",
            "label": "Max pages",
            "type": "number",
            "required": False,
            "value": 5,
        },
    ]


def test_n8n_marketing_seo_audit_preserves_parallel_agent_report_pipeline():
    """Shape used by official n8n template 3224: form URL -> scrape -> two
    per-item agents -> port-ordered merge -> aggregate -> Markdown HTML."""
    wf = {"name": "SEO audit", "nodes": [
        {"name": "Landing Page Url", "type": "n8n-nodes-base.formTrigger", "parameters": {}},
        {"name": "Scrape Website", "type": "n8n-nodes-base.httpRequest", "parameters": {
            "url": "={{ $json['Landing Page Url'] }}",
        }},
        {"name": "Technical Audit", "type": "@n8n/n8n-nodes-langchain.agent", "parameters": {
            "promptType": "define", "text": "=Technical audit: {{ $json.data }}",
        }},
        {"name": "Content Audit", "type": "@n8n/n8n-nodes-langchain.agent", "parameters": {
            "promptType": "define", "text": "=Content audit: {{ $json.data }}",
        }},
        {"name": "Technical Model", "type": "@n8n/n8n-nodes-langchain.lmChatOpenAi",
         "parameters": {"model": {"value": "gpt-4o-mini"}}},
        {"name": "Content Model", "type": "@n8n/n8n-nodes-langchain.lmChatOpenAi",
         "parameters": {"model": {"value": "gpt-4o-mini"}}},
        {"name": "Merge", "type": "n8n-nodes-base.merge", "parameters": {}},
        {"name": "Aggregate", "type": "n8n-nodes-base.aggregate", "parameters": {
            "fieldsToAggregate": {"fieldToAggregate": [{"fieldToAggregate": "output"}]},
        }},
        {"name": "Markdown", "type": "n8n-nodes-base.markdown", "parameters": {
            "mode": "markdownToHtml",
            "markdown": "=# Technical\n{{ $json.output[0] }}\n# Content\n{{ $json.output[1] }}",
        }},
    ], "connections": {
        "Landing Page Url": {"main": [[{"node": "Scrape Website"}]]},
        "Scrape Website": {"main": [[{"node": "Technical Audit"}, {"node": "Content Audit"}]]},
        "Technical Audit": {"main": [[{"node": "Merge", "index": 0}]]},
        "Content Audit": {"main": [[{"node": "Merge", "index": 1}]]},
        "Technical Model": {"ai_languageModel": [[{"node": "Technical Audit"}]]},
        "Content Model": {"ai_languageModel": [[{"node": "Content Audit"}]]},
        "Merge": {"main": [[{"node": "Aggregate"}]]},
        "Aggregate": {"main": [[{"node": "Markdown"}]]},
    }}

    by = {node.id: node for node in import_workflow(wf).graph.nodes}
    assert by["Scrape Website"].config["batch"] is True
    assert by["Scrape Website"].config["items"] == "{{Landing Page Url}}"
    assert by["Technical Audit"].config["prompt"] == "Technical audit: {{input.data}}"
    assert by["Technical Audit"].config["model"] == "gpt-4o-mini"
    assert by["Technical Audit"].config["batch"] is True
    assert by["Merge"].config["sources"] == ["Technical Audit", "Content Audit"]
    assert by["Aggregate"].config["wrap_key"] == "output"
    assert by["Markdown"].config["markdown_to_html"] is True
    assert by["Markdown"].config["markdown"] == (
        "# Technical\n{{input.output.0}}\n# Content\n{{input.output.1}}"
    )


def test_n8n_marketing_email_imports_binary_sheet_structured_ai_and_html():
    """Shape used by official n8n template 1978, without its final SMTP send."""
    wf = {"name": "Personalized marketing email", "nodes": [
        {"name": "Start", "type": "n8n-nodes-base.manualTrigger", "parameters": {}},
        {"name": "Download", "type": "n8n-nodes-base.httpRequest", "parameters": {
            "url": "https://example.com/customers.xlsx",
        }},
        {"name": "Rows", "type": "n8n-nodes-base.extractFromFile", "parameters": {
            "operation": "xls",
        }},
        {"name": "Campaign", "type": "n8n-nodes-base.set", "parameters": {
            "includeOtherFields": True,
            "assignments": {"assignments": [{
                "name": "Campaign Target", "type": "string", "value": "Retention",
            }]},
        }},
        {"name": "Draft", "type": "@n8n/n8n-nodes-langchain.informationExtractor", "parameters": {
            "text": "=Feedback: {{ $json.Feedback }}",
            "inputSchema": '{"type":"object","properties":{"Headline":{"type":"string"},"SendCoupon":{"type":"boolean"}}}',
            "options": {"systemPromptTemplate": "=Campaign: {{ $json['Campaign Target'] }}"},
        }},
        {"name": "Model", "type": "@n8n/n8n-nodes-langchain.lmChatOpenAi",
         "parameters": {"model": {"value": "gpt-4o-mini"}}},
        {"name": "Coupon?", "type": "n8n-nodes-base.if", "parameters": {
            "conditions": {"conditions": [{
                "leftValue": "={{ $json.output.SendCoupon }}",
                "operator": {"type": "boolean", "operation": "true", "singleValue": True},
            }]},
        }},
        {"name": "Email HTML", "type": "n8n-nodes-base.html", "parameters": {
            "html": "<h1>{{ $json.output['Headline'] }}</h1>",
        }},
    ], "connections": {
        "Start": {"main": [[{"node": "Download"}]]},
        "Download": {"main": [[{"node": "Rows"}]]},
        "Rows": {"main": [[{"node": "Campaign"}]]},
        "Campaign": {"main": [[{"node": "Draft"}]]},
        "Model": {"ai_languageModel": [[{"node": "Draft"}]]},
        "Draft": {"main": [[{"node": "Coupon?"}]]},
        "Coupon?": {"main": [[{"node": "Email HTML"}], []]},
    }}

    by = {node.id: node for node in import_workflow(wf).graph.nodes}
    assert by["Download"].config["response_format"] == "binary"
    assert by["Rows"].config == {**by["Rows"].config, "format": "xlsx", "input": "{{Download}}"}
    assert by["Campaign"].config["include_other_fields"] is True
    assert by["Draft"].config["batch"] is True
    assert by["Draft"].config["response_wrapper"] == "output"
    assert by["Draft"].config["model"] == "gpt-4o-mini"
    assert by["Draft"].config["input"] == "Feedback: {{input.Feedback}}"
    assert by["Coupon?"].config["expression"] == "output.SendCoupon == true"
    assert by["Coupon?"].config["pass_input"] is True
    assert by["Email HTML"].type == "transform"
    assert by["Email HTML"].config["html_template"] == "<h1>{{input.output.Headline}}</h1>"
