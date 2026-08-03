"""n8n workflow (JSON) importer.

n8n exports ``nodes`` (each with ``name`` + ``type`` like
``n8n-nodes-base.httpRequest``) and ``connections`` keyed by source node *name*.
Triggers, core logic and a handful of common integrations map cleanly; the long
tail of 400+ integration nodes degrades to ``connector`` placeholders.
"""
from __future__ import annotations

import json
import re

from .base import WorkflowImporter
from .connectors import resolve_connector
from .model import GraphNode, ImportReport, WorkflowGraph

# n8n comparison operations (v1 + v2) -> operators manor's evaluator supports.
N8N_OP_MAP = {
    "equal": "==", "equals": "==", "notEqual": "!=", "notEquals": "!=",
    "larger": ">", "gt": ">", "largerEqual": ">=", "gte": ">=",
    "smaller": "<", "lt": "<", "smallerEqual": "<=", "lte": "<=",
}

# bare node type (after the last '.') -> canonical manor node type
N8N_NODE_MAP = {
    "httpRequest": "http",
    "if": "condition",
    "filter": "filter",
    "switch": "switch",
    "set": "transform",
    "code": "code",
    "function": "code",
    "functionItem": "code",
    "merge": "merge",
    "noOp": "transform",
    "splitInBatches": "loop",
    "itemLists": "transform",
    "splitOut": "split",
    "limit": "limit",
    "sort": "sort",
    "removeDuplicates": "dedupe",
    "stopAndError": "stop",
    "extractFromFile": "extractfromfile",
    "aggregate": "aggregate",
    "summarize": "aggregate",
    "dateTime": "datetime",
    "wait": "wait",
    "respondToWebhook": "respond",
    "executeWorkflow": "subworkflow",
    "executeCommand": "code",
    "cron": "trigger",
    "interval": "trigger",
    "renameKeys": "transform",
    "itemListsV2": "transform",
    # HTML / markup extraction & conversion
    "html": "extract",
    "htmlExtract": "extract",
    "xml": "transform",
    "markdown": "transform",
    # fetch / feed
    "rssFeedRead": "http",
    "graphql": "http",
    # files
    "spreadsheetFile": "extractfromfile",
    "readWriteFile": "extractfromfile",
    "readBinaryFile": "extractfromfile",
    "readBinaryFiles": "extractfromfile",
    "convertToFile": "transform",
    "moveBinaryData": "transform",
    "writeBinaryFile": "extractfromfile",
    "compareDatasets": "merge",
    "editImage": "image",
    "crypto": "code",
    # legacy / misc core
    "start": "trigger",
    "manualTrigger": "trigger",
    "form": "transform",
    "n8nTrainingCustomerDatastore": "connector",
    # email / transport / databases — external services → connector
    "emailSend": "connector",
    "sendEmail": "connector",
    "emailReadImap": "connector",
    "ftp": "connector",
    "ssh": "connector",
    "postgres": "connector",
    "mySql": "connector",
    "microsoftSql": "connector",
    "mongoDb": "connector",
    "redis": "connector",
    "supabase": "connector",
    "elasticsearch": "connector",
    # LangChain AI nodes
    "agent": "agent",
    "chainLlm": "llm",
    "openAi": "llm",
    "lmChatOpenAi": "llm",
    "lmChatAnthropic": "llm",
    "informationExtractor": "extract",
    "textClassifier": "classifier",
    "vectorStoreQdrant": "rag",
    "vectorStoreInMemory": "rag",
    "retrieverVectorStore": "rag",
}

# Pure-annotation nodes with no execution / no edges — dropped on import.
N8N_DROP = {"stickyNote"}

# common first-party (``n8n-nodes-base.*``) integrations -> connector. These
# live in the base package so the third-party-package rule can't catch them;
# the long tail of n8n service nodes is enumerated here for parity.
KNOWN_CONNECTORS = {
    "slack", "gmail", "googleSheets", "github", "telegram", "discord",
    "notion", "airtable", "hubspot", "stripe", "twilio", "sendGrid",
    "jira", "linear", "salesforce", "zendesk", "mattermost", "microsoftTeams",
    "todoist", "raindrop", "mailerLite", "googleTasks", "googleCalendar",
    "googleDrive", "googleDocs", "trello", "asana", "mailchimp", "twitter",
    "facebook", "instagram", "wordpress", "shopify", "woocommerce", "clickup",
    "pipedrive", "intercom", "freshdesk", "gitlab", "dropbox", "openWeatherMap",
    # CRM / sales / support
    "agileCrm", "zohoCrm", "freshworksCrm", "copper", "keap", "drift",
    "helpScout", "front", "gong", "zammad", "twake", "activeCampaign",
    # content / CMS / commerce
    "storyblok", "contentful", "strapi", "directus", "ghost", "webflow",
    "magento2", "bigcommerce", "wix", "paypal", "square", "chargebee",
    # productivity / data / forms
    "baserow", "coda", "mondayCom", "typeform", "jotform", "calendly",
    "cal", "clockify", "harvest", "n8n", "uproc", "mindee", "zulip",
    # email / marketing
    "mandrill", "sendinblue", "getResponse", "convertKit", "lemlist",
    "emelia", "clearbit", "hunter", "dropcontact", "brevo",
    # databases / infra / messaging / cloud
    "snowflake", "rabbitmq", "kafka", "mqtt", "amqp", "nats", "awsS3",
    "awsSes", "awsSns", "awsLambda", "awsDynamoDb", "s3", "nextCloud", "box",
    "microsoftOutlook", "microsoftExcel", "microsoftOneDrive", "googleBigQuery",
    "googleAnalytics", "googleAds", "segment", "posthog", "mixpanel", "amplitude",
}


def _bare_type(node_type: str) -> str:
    return node_type.rsplit(".", 1)[-1] if node_type else ""


def _package(node_type: str) -> str:
    """The package portion of an n8n type — everything before the last dot.

    ``n8n-nodes-base.html`` -> ``n8n-nodes-base``;
    ``@n8n/n8n-nodes-langchain.agent`` -> ``@n8n/n8n-nodes-langchain``;
    ``n8n-nodes-brightdata.brightData`` -> ``n8n-nodes-brightdata``.
    """
    return node_type.rsplit(".", 1)[0] if "." in (node_type or "") else ""


def _langchain_type(bare: str) -> str:
    """Map a LangChain (``@n8n/...-langchain``) sub-node to a canonical type by
    prefix, so the whole AI node family resolves instead of degrading."""
    if bare == "agent":
        return "agent"
    if bare.startswith("informationExtractor"):
        return "extract"
    if bare.startswith("textClassifier"):
        return "classifier"
    if bare.startswith(("vectorStore", "retriever", "embeddings", "documentDefaultDataLoader", "textSplitter")):
        return "rag"
    if bare.startswith("tool"):
        return "tool"
    if bare.startswith(("memory", "outputParser")):
        return "transform"
    # lmChat*, lm*, chain*, and anything else in the family → an LLM call
    return "llm"


class N8nImporter(WorkflowImporter):
    source_tool = "n8n"

    @classmethod
    def detect(cls, raw: dict) -> bool:
        return (
            isinstance(raw, dict)
            and isinstance(raw.get("nodes"), list)
            and isinstance(raw.get("connections"), dict)
        )

    def parse(self, raw: dict, name: str | None = None) -> tuple[WorkflowGraph, ImportReport]:
        report = ImportReport(source_tool=self.source_tool)
        wf_name = name or raw.get("name") or "Imported n8n workflow"

        # n8n connections are keyed by the source node — usually its *name*, but
        # some exports key by node *id*. Targets (``{node}``) likewise. Resolve
        # both through a name/id -> graph-key map so either convention wires up,
        # and drop edges whose endpoint isn't a real node (dangling refs).
        raw_nodes = raw.get("nodes") or []
        ref_to_key: dict[str, str] = {}
        for rn in raw_nodes:
            key = rn.get("name") or rn.get("id") or ""
            if rn.get("name"):
                ref_to_key[rn["name"]] = key
            if rn.get("id"):
                ref_to_key[rn["id"]] = key

        branched: dict[str, list[list[str]]] = {}
        succ: dict[str, list[str]] = {}
        incoming_ports: dict[str, list[tuple[int, int, str]]] = {}
        edge_sequence = 0
        for src_ref, outs in (raw.get("connections") or {}).items():
            src = ref_to_key.get(src_ref, src_ref)
            arr: list[list[str]] = []
            for branch in (outs.get("main") or []):
                branch_targets = []
                for connection in (branch or []):
                    if not connection or connection.get("node") not in ref_to_key:
                        continue
                    target = ref_to_key[connection["node"]]
                    branch_targets.append(target)
                    incoming_ports.setdefault(target, []).append((
                        int(connection.get("index") or 0), edge_sequence, src,
                    ))
                    edge_sequence += 1
                arr.append(branch_targets)
            branched[src] = arr
            flat = [t for b in arr for t in b]
            if flat:
                succ[src] = flat

        # AI sub-node attachments. In n8n an agent's model / memory / tools are
        # *separate* nodes wired INTO the agent via ``ai_*`` connections (the
        # sub-node is the source, the agent the target). manor's agent is a
        # single node with inline config, so we collect these per agent and fold
        # them in — then drop the sub-nodes so they don't litter the canvas as
        # disconnected islands. Mirrors how n8n executes an agent as one unit.
        raw_by_key: dict[str, dict] = {}
        for rn in raw_nodes:
            raw_by_key[rn.get("name") or rn.get("id") or ""] = rn
        ai_attach: dict[str, dict[str, list[str]]] = {}
        for src_ref, outs in (raw.get("connections") or {}).items():
            src = ref_to_key.get(src_ref, src_ref)
            for ctype, branches in outs.items():
                if not isinstance(ctype, str) or not ctype.startswith("ai_"):
                    continue
                role = ctype[3:]  # languageModel / memory / tool / outputParser …
                for branch in (branches or []):
                    for c in (branch or []):
                        tgt = ref_to_key.get(c.get("node")) if c else None
                        if tgt:
                            ai_attach.setdefault(tgt, {}).setdefault(role, []).append(src)
        folded: set[str] = {k for roles in ai_attach.values() for ks in roles.values() for k in ks}

        nodes: list[GraphNode] = []
        for rn in (raw.get("nodes") or []):
            name_id = rn.get("name") or rn.get("id") or ""
            ntype = rn.get("type") or ""
            bare = _bare_type(ntype)

            if bare in N8N_DROP:  # sticky notes etc. — annotations, not flow
                continue
            if name_id in folded:  # model/memory/tool — folded into its agent
                continue

            canonical = N8N_NODE_MAP.get(bare)
            if canonical is None and (bare.endswith("Trigger") or bare == "webhook"):
                canonical = "trigger"
            if canonical is None and bare in KNOWN_CONNECTORS:
                canonical = "connector"
            # app integrations exposed as agent tools (e.g. googleTasksTool,
            # discordTool) — treat as a connector call.
            if canonical is None and bare.endswith("Tool") and not bare.startswith("tool"):
                canonical = "connector"
            # LangChain AI family (@n8n/...-langchain.*) — resolve by prefix so
            # models, tools, memory, vector stores etc. all map to a real type.
            if canonical is None and "langchain" in ntype:
                canonical = _langchain_type(bare)
            # Any node from a third-party / community package (outside core
            # ``n8n-nodes-base`` and the LangChain bundle) is an external
            # service call — map it to a connector rather than skipping it.
            if canonical is None:
                pkg = _package(ntype)
                if pkg and pkg != "n8n-nodes-base" and "langchain" not in pkg:
                    canonical = "connector"

            if canonical is None:
                node = self._placeholder(
                    report, name_id, name_id, ntype, rn,
                    reason=f"n8n node type '{ntype}' not in the supported/connector set",
                )
                node.next = succ.get(name_id, [])
                nodes.append(node)
                continue

            params = rn.get("parameters") or {}
            if bare in {"itemLists", "itemListsV2"} and params.get("fieldToSplitOut"):
                canonical = "split"
            if bare == "html" and params.get("html") and not params.get("extractionValues"):
                canonical = "transform"
            native = self._translate(canonical, params, bare)
            if canonical == "agent":
                native = self._fold_agent(native, params, ai_attach.get(name_id, {}), raw_by_key)
            elif canonical in {"llm", "extract"} and ai_attach.get(name_id):
                native = self._fold_model(native, ai_attach[name_id], raw_by_key)
            n8n_raw = {"type": ntype, "parameters": params}
            if rn.get("credentials"):
                n8n_raw["credentials"] = rn["credentials"]

            node = GraphNode(
                id=name_id, type=canonical, name=name_id,
                config={**native, "n8n": n8n_raw},
                next=succ.get(name_id, []),
                meta={"source_tool": self.source_tool, "original_type": ntype},
            )
            if canonical == "condition":
                outs = branched.get(name_id, [])
                node.true_next = outs[0] if len(outs) > 0 else []
                node.false_next = outs[1] if len(outs) > 1 else []
            elif canonical == "switch":
                node.config.update(self._switch_cases(params, branched.get(name_id, [])))
            nodes.append(node)

        # n8n passes an item stream along each edge. Manor keeps step outputs in
        # named variables, so make that implicit data flow explicit for the
        # common list-processing nodes. This is what turns an imported graph
        # from a visually-correct canvas into an actually runnable pipeline.
        incoming: dict[str, list[str]] = {}
        for source, targets in succ.items():
            for target in targets:
                incoming.setdefault(target, []).append(source)
        for node in nodes:
            sources = incoming.get(node.id, [])
            if node.type == "merge" and sources:
                ordered_sources = [
                    source for _port, _seq, source
                    in sorted(incoming_ports.get(node.id, []))
                ]
                node.config.setdefault("sources", ordered_sources or sources)
                if node.config.get("mode") != "combine_by_position":
                    node.config.setdefault("flatten", True)
            elif node.type in {"transform", "filter", "limit", "aggregate"} and len(sources) == 1:
                # A noOp has no mapping and remains a regular pass-through.
                if node.type != "transform" or any(
                    key in node.config for key in ("set", "html_template", "markdown")
                ):
                    node.config.setdefault("items", f"{{{{{sources[0]}}}}}")
            elif node.type == "split" and len(sources) == 1:
                node.config.setdefault("items", f"{{{{{sources[0]}}}}}")
            elif node.type == "http" and len(sources) == 1 and node.config.get("batch"):
                node.config.setdefault("items", f"{{{{{sources[0]}}}}}")
            elif node.type == "extractfromfile" and len(sources) == 1:
                node.config.setdefault("input", f"{{{{{sources[0]}}}}}")
            elif node.type == "condition" and len(sources) == 1:
                node.config.setdefault(
                    "inputs",
                    [{"key": "input", "value": f"{{{{{sources[0]}}}}}", "type": "any"}],
                )
                # n8n If nodes route the current item; the boolean only decides
                # which output port is followed.  Preserve the item for the
                # downstream marketing-email composition nodes.
                node.config.setdefault("pass_input", True)
            elif node.type in {"llm", "agent", "classifier", "extract"} and len(sources) == 1:
                node.config.setdefault(
                    "inputs",
                    [{"key": "input", "value": f"{{{{{sources[0]}}}}}", "type": "any"}],
                )
                source_node = next((candidate for candidate in nodes if candidate.id == sources[0]), None)
                if source_node and source_node.config.get("batch") and node.type in {"llm", "agent"}:
                    node.config.setdefault("batch", True)
                if node.type == "agent" and "prompt" not in node.config:
                    source_type = _bare_type((raw_by_key.get(sources[0]) or {}).get("type") or "")
                    node.config["prompt"] = (
                        "{{input.chatInput}}" if source_type == "chatTrigger" else "{{input}}"
                    )

        # Fallback: if the source had no usable connections (missing or scrubbed),
        # the import would be a disconnected pile. Wire the nodes left-to-right
        # from the n8n canvas layout (triggers first, then by x/y position) so the
        # workflow is at least a runnable linear flow the user can rearrange.
        if len(nodes) > 1 and not any(n.next for n in nodes):
            pos: dict[str, tuple] = {}
            for rn in (raw.get("nodes") or []):
                nid = rn.get("name") or rn.get("id") or ""
                p = rn.get("position")
                pos[nid] = (p[0], p[1]) if isinstance(p, list) and len(p) >= 2 else (0, 0)
            # 'stop' (Stop-And-Error) nodes are error branches, not main flow —
            # leave them out of the inferred chain so the linear path doesn't
            # dead-end on a deliberate failure.
            chain = sorted(
                (n for n in nodes if n.type != "stop"),
                key=lambda n: (n.type != "trigger", pos.get(n.id, (0, 0))),
            )
            for a, b in zip(chain, chain[1:]):
                if a.type == "end":
                    continue
                a.next = [b.id]
                if a.type == "condition":
                    a.true_next, a.false_next = [b.id], []
            report.warnings.append(
                "Source had no usable connections; nodes were wired left-to-right "
                "from the canvas layout as a best-effort linear flow — review the order."
            )

        self._finalize(report, nodes)
        return WorkflowGraph(name=wf_name, source_tool=self.source_tool, nodes=nodes), report

    # ── Config translation: n8n parameters -> manor-native config ────────

    def _translate(self, canonical: str, params: dict, bare: str = "") -> dict:
        try:
            if canonical == "connector":
                # Integration node -> shared connector resolution (same layer
                # Dify tool nodes use), so the canonical node is identical.
                return resolve_connector(
                    bare,
                    operation=params.get("operation") or params.get("resource"),
                    args=params,
                )
            if canonical == "trigger":
                return self._t_trigger(params, bare)
            if canonical == "http":
                return self._t_http(params, bare)
            if canonical == "extract" and bare in {"html", "htmlExtract"}:
                return self._t_html_extract(params)
            if canonical == "extract" and bare.startswith("informationExtractor"):
                return self._t_information_extractor(params)
            if canonical == "extractfromfile":
                return self._t_extract_from_file(params)
            if canonical == "transform" and bare == "html" and params.get("html"):
                return self._t_html_template(params)
            if canonical == "transform" and bare == "markdown":
                return self._t_markdown(params)
            fn = {
                "condition": self._t_if,
                "code": self._t_code,
                "transform": self._t_set,
                "filter": self._t_filter,
                "limit": self._t_limit,
                "aggregate": self._t_aggregate,
                "split": self._t_split,
                "merge": self._t_merge,
            }.get(canonical)
            if canonical == "llm":
                return self._t_llm(params, bare)
            return fn(params) if fn else {}
        except Exception:  # never let translation break the import
            return {}

    def _t_trigger(self, params: dict, bare: str = "") -> dict:
        """Preserve n8n form fields as explicit inputs for a manual run.

        n8n's Form Trigger is both an entry point and its input UI.  Manor's
        canvas Run action renders these rows in its own run dialog, then sends
        the entered values as ``trigger_data``.  Keeping the schema on the
        trigger makes an imported form workflow runnable without inspecting
        downstream expressions or hand-authoring variables first.
        """
        if bare != "formTrigger":
            return {}
        values = (params.get("formFields") or {}).get("values") or []
        inputs = []
        type_map = {
            "number": "number",
            "checkbox": "boolean",
            "boolean": "boolean",
            "textarea": "string",
            "email": "string",
            "password": "string",
            "text": "string",
        }
        for field in values:
            key = str(field.get("fieldLabel") or field.get("fieldName") or "").strip()
            if not key:
                continue
            row = {
                "key": key,
                "label": key,
                "type": type_map.get(str(field.get("fieldType") or "text").lower(), "string"),
                "required": bool(field.get("requiredField")),
            }
            default = field.get("defaultValue")
            if default is not None:
                row["value"] = default
            placeholder = field.get("placeholder")
            if placeholder:
                row["placeholder"] = str(placeholder)
            inputs.append(row)
        return {"inputs": inputs} if inputs else {}

    def _norm_expr(self, text):
        """Normalise n8n ``={{ $json.x }}`` templates to manor ``{{x}}``."""
        if not isinstance(text, str):
            return text
        t = re.sub(
            r"\{\{\s*\$json\.([A-Za-z0-9_:-]+(?:\.[A-Za-z0-9_:-]+)*)\s*\}\}",
            r"{{\1}}",
            text,
        )
        t = re.sub(
            r"\{\{\s*\$json\[['\"]([^'\"]+)['\"]\]\s*\}\}",
            r"{{\1}}",
            t,
        )
        t = re.sub(
            r"\{\{\s*\$\(['\"]([^'\"]+)['\"]\)\.first\(\)\.json\."
            r"([A-Za-z0-9_:-]+(?:\.[A-Za-z0-9_:-]+)*)\s*\}\}",
            r"{{\1.0.\2}}",
            t,
        )
        t = re.sub(
            r"\{\{\s*\$node\[['\"]([^'\"]+)['\"]\]\.json\[['\"]([^'\"]+)['\"]\]"
            r"(?:\+\+)?\s*\}\}",
            r"{{\1.\2}}",
            t,
        )
        return t[1:] if t.startswith("=") else t

    def _norm_llm_expr(self, text):
        """Translate n8n's current-item ``$json`` refs to a named input.

        Array indexes become dotted numeric path segments so Manor can resolve
        ``$json.data[0].title`` as ``{{input.data.0.title}}``.
        """
        if not isinstance(text, str):
            return text

        pattern = re.compile(
            r"\{\{\s*\$json((?:\.[A-Za-z0-9_:-]+|\[['\"][^'\"]+['\"]\]|\[\d+\])*)\s*\}\}"
        )

        def replace(match):
            suffix = match.group(1)
            parts = re.findall(r"\.([A-Za-z0-9_:-]+)|\[['\"]([^'\"]+)['\"]\]|\[(\d+)\]", suffix)
            path = [next(value for value in group if value) for group in parts]
            return "{{input" + ("." + ".".join(path) if path else "") + "}}"

        out = pattern.sub(replace, text)
        return out[1:] if out.startswith("=") else out

    def _bare_ref(self, text):
        """Extract a bare variable name from an n8n value/expression."""
        if not isinstance(text, str):
            return text
        m = re.search(
            r"\$json((?:\.[A-Za-z0-9_:-]+|\[['\"][^'\"]+['\"]\])*)",
            text,
        )
        if m:
            parts = re.findall(r"\.([A-Za-z0-9_:-]+)|\[['\"]([^'\"]+)['\"]\]", m.group(1))
            return ".".join(left or right for left, right in parts)
        m = re.search(r"\$\(['\"][^'\"]+['\"]\)\.item\.json\.([A-Za-z0-9_]+)", text)
        if m:
            return m.group(1)
        m = re.search(
            r"\$node\[['\"]([^'\"]+)['\"]\]\.json\[['\"]([^'\"]+)['\"]\]",
            text,
        )
        if m:
            return f"{m.group(1)}.{m.group(2)}"
        return text.strip().lstrip("=").strip()

    def _t_http(self, params: dict, bare: str = "") -> dict:
        cfg: dict = {}
        if params.get("url"):
            cfg["url"] = self._norm_expr(params["url"])
            if "$json" in str(params["url"]):
                cfg["batch"] = True
            if re.search(r"\.(?:xlsx|xls)(?:\?|$)", str(params["url"]), re.IGNORECASE):
                cfg["response_format"] = "binary"
        if params.get("method"):
            cfg["method"] = str(params["method"]).upper()
        hp = (params.get("headerParameters") or {}).get("parameters")
        if isinstance(hp, list):
            headers = {
                p["name"]: self._norm_expr(str(p.get("value", "")))
                for p in hp if p.get("name")
            }
            if headers:
                cfg["headers"] = headers
        qp = (params.get("queryParameters") or {}).get("parameters")
        if isinstance(qp, list):
            query = {
                p["name"]: self._norm_expr(str(p.get("value", "")))
                for p in qp if p.get("name")
            }
            if query:
                cfg["query"] = query
        if params.get("jsonBody"):
            cfg["body"] = self._norm_expr(params["jsonBody"])
        else:
            bp = (params.get("bodyParameters") or {}).get("parameters")
            if isinstance(bp, list):
                body = {
                    p["name"]: self._norm_expr(str(p.get("value", "")))
                    for p in bp if p.get("name")
                }
                if body:
                    cfg["body"] = body
        if bare == "rssFeedRead":
            cfg["response_format"] = "rss"
        response = (((params.get("options") or {}).get("response") or {}).get("response") or {})
        if str(response.get("responseFormat") or "").lower() in {"file", "binary"}:
            cfg["response_format"] = "binary"
        cfg.setdefault("headers", {}).setdefault("User-Agent", "Manor-Workflow/1.0")
        return cfg

    def _t_extract_from_file(self, params: dict) -> dict:
        operation = str(params.get("operation") or "auto").lower()
        format_ = "xlsx" if operation in {"xls", "xlsx"} else operation
        return {"format": format_}

    def _t_html_extract(self, params: dict) -> dict:
        values = (params.get("extractionValues") or {}).get("values") or []
        fields = []
        for value in values:
            key = value.get("key")
            selector = value.get("cssSelector")
            if not key or not selector:
                continue
            fields.append({
                "key": key,
                "selector": selector,
                "return_value": value.get("returnValue") or "text",
                "attribute": value.get("attribute"),
                "return_array": bool(value.get("returnArray")),
                "skip_selectors": value.get("skipSelectors") or "",
            })
        return {"html_extract": fields}

    def _t_html_template(self, params: dict) -> dict:
        template = self._norm_llm_expr(str(params.get("html") or ""))
        # n8n templates commonly use a JS ternary solely to add a fallback
        # greeting. Manor templates stay declarative; preserve the actual field.
        template = re.sub(
            r"\{\{\s*\$json\[['\"]([^'\"]+)['\"]\]\s*\?.*?\}\}",
            r"{{input.\1}}",
            template,
        )
        return {"html_template": template}

    def _t_markdown(self, params: dict) -> dict:
        source = params.get("markdown") or params.get("text") or ""
        return {
            "markdown": self._norm_llm_expr(str(source)),
            "markdown_to_html": str(params.get("mode") or "").lower() == "markdowntohtml",
        }

    def _t_information_extractor(self, params: dict) -> dict:
        cfg: dict = {
            "input": self._norm_llm_expr(str(params.get("text") or "{{input}}")),
            "batch": True,
            "response_wrapper": "output",
        }
        schema = params.get("inputSchema")
        if schema:
            try:
                cfg["schema"] = json.loads(schema) if isinstance(schema, str) else schema
            except (TypeError, ValueError):
                cfg["schema"] = schema
        system = (params.get("options") or {}).get("systemPromptTemplate")
        if system:
            cfg["system_prompt"] = self._norm_llm_expr(str(system))
        return cfg

    def _t_split(self, params: dict) -> dict:
        field = params.get("fieldToSplitOut") or params.get("field")
        return {
            "field": field,
            # n8n Split Out keeps the source field name on every emitted item.
            "preserve_field": bool(field),
        }

    def _t_merge(self, params: dict) -> dict:
        if params.get("mode") == "combine" and (
            params.get("combineBy") == "combineByPosition"
            or params.get("combinationMode") == "mergeByPosition"
        ):
            return {"mode": "combine_by_position"}
        return {}

    def _t_filter(self, params: dict) -> dict:
        conditions = (params.get("conditions") or {}).get("conditions") or []
        if not conditions:
            return {}
        condition = conditions[0]
        operator = (condition.get("operator") or {}).get("operation")
        field = self._bare_ref(condition.get("leftValue"))
        right = condition.get("rightValue")
        if operator in {"after", "before"} and field:
            cfg: dict = {"field": field, "operation": operator}
            relative = re.search(
                r"\$today\.(minus|plus)\s*\(\s*\{\s*days\s*:\s*(\d+)",
                str(right),
            )
            if relative:
                days = int(relative.group(2))
                cfg["relative_days"] = -days if relative.group(1) == "minus" else days
            elif right not in (None, ""):
                cfg["threshold"] = self._norm_expr(str(right))
            return cfg
        expr = self._expr_from_conditions(params.get("conditions") or {})
        return {"condition": expr} if expr else {}

    def _t_limit(self, params: dict) -> dict:
        return {
            "max": params.get("maxItems", params.get("max", 1)),
            "keep": "last" if params.get("keep") == "lastItems" else "first",
        }

    def _t_aggregate(self, params: dict) -> dict:
        field_specs = (params.get("fieldsToAggregate") or {}).get("fieldToAggregate") or []
        aggregate_fields = [
            spec.get("fieldToAggregate") for spec in field_specs
            if isinstance(spec, dict) and spec.get("fieldToAggregate")
        ]
        if aggregate_fields:
            return {"operation": "collect", "wrap_key": aggregate_fields[0]}
        fields = [
            field.strip()
            for field in str(params.get("fieldsToInclude") or "").split(",")
            if field.strip()
        ]
        if params.get("aggregate") == "aggregateAllItemData":
            cfg: dict = {
                "operation": "collect",
                "wrap_key": params.get("destinationFieldName") or "data",
            }
            if fields:
                cfg["fields"] = fields
            # Keep imported news/article prompts within normal model limits.
            cfg["max_field_chars"] = 2400
            return cfg
        return {"operation": str(params.get("operation") or "collect").lower()}

    def _t_llm(self, params: dict, bare: str = "") -> dict:
        cfg: dict = {}
        text = params.get("text") or params.get("prompt")
        if text:
            cfg["prompt"] = self._norm_llm_expr(str(text))
        options = params.get("options") or {}
        system = options.get("systemMessage") or params.get("systemMessage")
        if system:
            cfg["system_prompt"] = self._norm_llm_expr(str(system))
        if options.get("temperature") is not None:
            cfg["temperature"] = options["temperature"]
        if bare.startswith("chainSummarization"):
            cfg.setdefault(
                "prompt",
                "Summarize the following document clearly and concisely:\n{{input.data}}",
            )
            cfg["batch"] = True
            cfg["response_wrapper"] = "response.text"
        return cfg

    def _t_if(self, params: dict) -> dict:
        expr = self._expr_from_conditions(params.get("conditions") or {})
        return {"expression": expr} if expr else {}

    def _expr_from_conditions(self, conds) -> str | None:
        """Build a (compound) expression from an n8n conditions object (v1/v2)."""
        clauses: list[tuple] = []
        logic = "and"
        # v2: {combinator, conditions: [{leftValue, rightValue, operator}]}
        if isinstance(conds, dict) and isinstance(conds.get("conditions"), list) and conds["conditions"]:
            logic = conds.get("combinator", "and")
            for c in conds["conditions"]:
                clauses.append((
                    self._bare_ref(c.get("leftValue")),
                    (c.get("operator") or {}).get("operation"),
                    c.get("rightValue"),
                ))
        elif isinstance(conds, dict):
            # v1: {number|string|boolean: [{value1, operation, value2}]}
            for kind in ("number", "string", "boolean"):
                items = conds.get(kind)
                if isinstance(items, list) and items:
                    for c in items:
                        clauses.append(
                            (self._bare_ref(c.get("value1")), c.get("operation"), c.get("value2"))
                        )
                    break
        exprs = [e for e in (self._cond_expr(*c) for c in clauses) if e]
        if not exprs:
            return None
        joiner = " or " if str(logic).lower() == "or" else " and "
        return joiner.join(exprs)

    def _switch_cases(self, params: dict, branch_outputs: list[list[str]]) -> dict:
        """n8n switch -> {cases: [{expression, next}], default_next}.

        Each rule (output index i) becomes a case routed to that output's branch
        targets; the fallback output (beyond the rules) becomes default_next.
        """
        rules = params.get("rules") or {}
        values = rules.get("values") or rules.get("rules") or []
        cases = []
        for i, rule in enumerate(values):
            nxt = branch_outputs[i] if i < len(branch_outputs) else []
            expr = None
            if isinstance(rule, dict):
                if rule.get("conditions"):
                    expr = self._expr_from_conditions(rule["conditions"])
                else:  # v1 rule: compared against the switch's value1
                    expr = self._cond_expr(
                        self._bare_ref(params.get("value1")),
                        rule.get("operation"), rule.get("value2"),
                    )
            cases.append({"expression": expr or "", "next": nxt})
        default_next = branch_outputs[len(values)] if len(branch_outputs) > len(values) else []
        return {"cases": cases, "default_next": default_next}

    def _cond_expr(self, left, op, right) -> str | None:
        if left and str(op) in {"isEmpty", "empty"}:
            return f"len({left}) == 0"
        if left and str(op) in {"isNotEmpty", "notEmpty"}:
            return f"len({left}) > 0"
        if left and str(op) in {"true", "isTrue"}:
            return f"{left} == true"
        if left and str(op) in {"false", "isFalse"}:
            return f"{left} == false"
        mapped = N8N_OP_MAP.get(str(op))
        if not left or not mapped:
            return None
        val_str = str(right)
        try:
            float(val_str)
            rhs = val_str
        except ValueError:
            rhs = f'"{val_str}"'
        return f"{left} {mapped} {rhs}"

    def _t_code(self, params: dict) -> dict:
        cfg: dict = {}
        code = (
            params.get("jsCode") or params.get("functionCode")
            or params.get("pythonCode") or params.get("code")
        )
        if code is not None:
            cfg["code"] = code
        if params.get("language"):
            cfg["language"] = params["language"]
        elif params.get("pythonCode"):
            cfg["language"] = "python"
        elif params.get("jsCode") or params.get("functionCode"):
            cfg["language"] = "javascript"
        return cfg

    def _fold_agent(self, native: dict, params: dict, attach: dict, raw_by_key: dict) -> dict:
        """Fold an n8n agent's attached sub-nodes (model / memory / tools) and
        its own prompt into manor's inline agent config — see ``ai_attach``."""
        out = dict(native)
        opts = params.get("options") or {}
        sysmsg = opts.get("systemMessage") or params.get("systemMessage")
        if sysmsg and "system_prompt" not in out:
            out["system_prompt"] = self._norm_llm_expr(str(sysmsg))
        text = params.get("text")
        if text and str(params.get("promptType")) == "define" and "prompt" not in out:
            out["prompt"] = self._norm_llm_expr(str(text))

        out = self._fold_model(out, attach, raw_by_key)

        # tools — use each attached tool node's name as the tool label
        tools = list(attach.get("tool", []))
        if tools:
            out["tools"] = tools
        if attach.get("memory"):
            out["memory"] = True
        return out

    @staticmethod
    def _fold_model(native: dict, attach: dict, raw_by_key: dict) -> dict:
        out = dict(native)

        # model name from the attached ai_languageModel node's parameters
        for mkey in attach.get("languageModel", []):
            mp = (raw_by_key.get(mkey) or {}).get("parameters") or {}
            model = mp.get("model") or mp.get("modelId") or (mp.get("options") or {}).get("model")
            if isinstance(model, dict):
                model = model.get("value") or model.get("name") or model.get("mode")
            if model:
                out["model"] = str(model)
                break
        return out

    def _t_set(self, params: dict) -> dict:
        out: dict = {}
        asn = (params.get("assignments") or {}).get("assignments")
        if isinstance(asn, list):  # v2
            for a in asn:
                if a.get("name"):
                    value = a.get("value", "")
                    out[a["name"]] = self._norm_expr(value) if isinstance(value, str) else value
        else:  # v1: values.{string,number,boolean}
            values = params.get("values") or {}
            for kind in ("string", "number", "boolean"):
                for a in (values.get(kind) or []):
                    if a.get("name"):
                        default = 0 if kind == "number" else False if kind == "boolean" else ""
                        value = a.get("value", default)
                        out[a["name"]] = self._norm_expr(value) if isinstance(value, str) else value
        cfg = {"set": out} if out else {}
        if params.get("includeOtherFields") is True:
            cfg["include_other_fields"] = True
        return cfg
