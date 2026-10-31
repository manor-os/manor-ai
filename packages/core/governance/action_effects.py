"""The single declaration point for what every governed action *does*.

Approval policy selects rules by ``action_key``; this table states each key's
effect so every caller (runtime gate, discovery, settings, Strategist,
channels, rooms, files) computes identical :class:`ActionFacts`. Nothing here
is inferred from verbs, prefixes or tool names at evaluation time: a key is
either declared, belongs to a namespace minted by exactly one factory (see
``_FAMILY_EFFECTS``), or it is undeclared and evaluates as ``unknown`` (which
asks in every mode).

``gate`` says whether the action itself crosses the consent gate:

* ``policy`` — the normal inherited evaluator (mode baseline + rules).
* ``leaf_only`` — the action only routes, asks or hands off; the leaf calls
  it causes cross the gate on their own. Mode baselines and category rules do
  not apply to it; an explicit action/resource rule the user wrote for this
  exact key still does (the user is the authority).

Dynamic MCP actions (``<server>.<action>``) are not listed: their factory
(``RuntimeMCPApprovalActionFactory``) writes impact from the shared
``MCPActionEffect`` and category from :data:`MCP_SERVER_CATEGORIES`.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Mapping, get_args

from packages.core.constants.chrome import CHROME_READ_ACTIONS
from packages.core.governance.approval_policy import ApprovalCategory, ApprovalImpact

STRICT_EFFECT_CONTRACT_ENV = "MANOR_STRICT_EFFECT_CONTRACT"
# A read is in the read category; a basic Chrome read is in the browser
# category, so the user's browser rules reach it and legacy ``read`` rules do
# not (basic browsing is allowed by default in every mode).
_READ_CATEGORIES = frozenset({"read", "browser"})


class EffectGate(StrEnum):
    POLICY = "policy"
    LEAF_ONLY = "leaf_only"


class UndeclaredEffectError(LookupError):
    """An action reached the approval evaluator without a declared effect."""


@dataclass(frozen=True)
class ActionEffect:
    impact: ApprovalImpact
    category: ApprovalCategory
    gate: EffectGate = EffectGate.POLICY
    reason: str = ""

    def __post_init__(self) -> None:
        if self.impact not in get_args(ApprovalImpact) or self.impact == "unknown":
            raise ValueError(f"declared impact must be known, got {self.impact!r}")
        if self.category not in get_args(ApprovalCategory):
            raise ValueError(f"unknown approval category {self.category!r}")
        if self.impact == "read" and self.category not in _READ_CATEGORIES:
            raise ValueError("a read effect is in the read (or basic browser) category")
        object.__setattr__(self, "gate", EffectGate(self.gate))


def _effect(impact: ApprovalImpact, category: ApprovalCategory, reason: str = "") -> ActionEffect:
    return ActionEffect(impact, category, EffectGate.POLICY, reason)


def _leaf(impact: ApprovalImpact, category: ApprovalCategory, reason: str) -> ActionEffect:
    return ActionEffect(impact, category, EffectGate.LEAF_ONLY, reason)


_WORKSPACE = _effect("reversible", "workspace")
_FILES = _effect("reversible", "files")
_DELETION = _effect("destructive", "deletion")
_COMMUNICATION = _effect("external", "communication")
_PUBLISHING = _effect("external", "publishing")
_AUTOMATION = _effect("external", "automation")
# Creating a sandbox only manages the disposable container.
_SANDBOX = _effect("reversible", "unknown", "Manages a disposable sandbox container.")
# The Sandbox tool's own work (commands, answers to a running command,
# cancelling one). Product decision (2026-10-06, #1071): a sandbox is an
# isolated, disposable container and its work runs by default; the only way a
# result reaches the user's files is save_result, a workspace file create.
# confirm_changes and explicit rules still ask.
_SANDBOX_WORK = _effect("reversible", "workspace", "Work inside an isolated, disposable sandbox container.")
_HUMAN_INPUT = _leaf(
    "reversible", "workspace",
    "Asks the user a question; the answer is the user's own decision.",
)

_DECLARED: dict[str, ActionEffect] = {
    # ── Workspace records (internal, reversible) ───────────────────────────
    "workspace.task.create": _WORKSPACE,
    "workspace.task.update": _WORKSPACE,
    "workspace.update_task": _WORKSPACE,
    "workspace.add_task_comment": _WORKSPACE,
    "workspace.knowledge.update": _WORKSPACE,
    "workspace.goal.create": _WORKSPACE,
    "workspace.goal.update": _WORKSPACE,
    "workspace.stat.create": _WORKSPACE,
    "workspace.stat.update": _WORKSPACE,
    "workspace.stat.record": _WORKSPACE,
    "workspace.update_workspace_goals": _WORKSPACE,
    "workspace.dashboard.module.update": _WORKSPACE,
    "workspace.skill.create": _WORKSPACE,
    "workspace.skill.modify": _WORKSPACE,
    "workspace.update_skill": _WORKSPACE,
    "workspace.invoke_skill": _WORKSPACE,
    "workspace.workflow.create": _WORKSPACE,
    "workspace.workflow.modify": _WORKSPACE,
    "workspace.agent_file.modify": _WORKSPACE,
    "workspace.agent.create": _WORKSPACE,
    "workspace.agent.capabilities.modify": _WORKSPACE,
    "workspace.operation.draft": _WORKSPACE,
    "workspace.operation.update": _WORKSPACE,
    "workspace.operation.discard": _WORKSPACE,
    "workspace.operation.apply": _WORKSPACE,
    "workspace.draft.start": _WORKSPACE,
    "workspace.draft.update": _WORKSPACE,
    "workspace.update_workspace": _WORKSPACE,
    "workspace.update_operating_model": _WORKSPACE,
    "workspace.add_workspace_service": _WORKSPACE,
    "workspace.remove_workspace_service": _WORKSPACE,
    "workspace.map_agent_to_service": _WORKSPACE,
    "workspace.unmap_agent_from_service": _WORKSPACE,
    "workspace.bind_channel": _WORKSPACE,
    "workspace.unbind_channel": _WORKSPACE,
    "workspace.strategist.run": _WORKSPACE,
    "workspace.rule.update": _WORKSPACE,
    "workspace.update_workspace_rules": _WORKSPACE,
    # ── CRM records ────────────────────────────────────────────────────────
    "workspace.client.create": _WORKSPACE,
    "workspace.client.update": _WORKSPACE,
    "workspace.client.delete": _DELETION,
    "workspace.client.relationship.link": _WORKSPACE,
    "workspace.client.relationship.unlink": _WORKSPACE,
    "workspace.client.contact.create": _WORKSPACE,
    "workspace.client.contact.update": _WORKSPACE,
    "workspace.client.contact.delete": _DELETION,
    "workspace.client.activity.create": _WORKSPACE,
    "workspace.client.activity.update": _WORKSPACE,
    "workspace.client.activity.delete": _DELETION,
    "workspace.client.opportunity.create": _WORKSPACE,
    "workspace.client.opportunity.update": _WORKSPACE,
    "workspace.client.opportunity.delete": _DELETION,
    "workspace.client.followup.create": _WORKSPACE,
    "workspace.client.followup.update": _WORKSPACE,
    "workspace.client.followup.delete": _DELETION,
    # A private Workspace invitation reaches someone outside the account.
    "workspace.client.delivery.prepare": _effect("external", "workspace"),
    "workspace.client.delivery.rotate": _effect("external", "workspace"),
    "workspace.client.delivery.revoke": _effect("external", "workspace"),
    # An internal BusinessOrder record plus an internal order event; no
    # payment or outside party is involved (order_service.create_order).
    "workspace.order.create": _WORKSPACE,
    "workspace.update_order": _WORKSPACE,
    # ── Skills / workflows / automations ───────────────────────────────────
    "workspace.skill.delete": _DELETION,
    "workspace.workflow.delete": _DELETION,
    "workspace.workflow.deploy": _PUBLISHING,
    "workspace.workflow.run": _effect("external", "workspace"),
    "workspace.workflow.youtube.publish": _PUBLISHING,
    "workspace.site.publish": _PUBLISHING,
    "workspace.automation.create": _AUTOMATION,
    "workspace.automation.update": _AUTOMATION,
    "workspace.automation.run": _AUTOMATION,
    "workspace.automation.delete": _DELETION,
    "workspace.automation.agent_initiative.create": _AUTOMATION,
    "workspace.automation.agent_initiative.update": _AUTOMATION,
    "workspace.automation.agent_initiative.mute": _AUTOMATION,
    "workspace.automation.agent_initiative.unmute": _AUTOMATION,
    "workspace.automation.agent_initiative.pause": _AUTOMATION,
    "workspace.automation.agent_initiative.resume": _AUTOMATION,
    "workspace.automation.agent_initiative.run": _AUTOMATION,
    "workspace.automation.agent_initiative.delete": _DELETION,
    # ── Files ──────────────────────────────────────────────────────────────
    "workspace.file.create": _FILES,
    "workspace.file.modify": _FILES,
    "workspace.file.write": _FILES,
    "workspace.file.delete": _DELETION,
    "workspace.upload_document": _FILES,
    "workspace.create_document_folder": _FILES,
    "workspace.rename_document_folder": _FILES,
    "workspace.move_document_folder": _FILES,
    "workspace.move_document_to_folder": _FILES,
    "workspace.move_documents_to_folder": _FILES,
    "workspace.delete_document_folder": _DELETION,
    "room.file.create": _FILES,
    "room.file.modify": _FILES,
    "room.file.delete": _DELETION,
    "tool.save_sandbox_file": _FILES,
    # A scratch file inside the sandbox (see _SANDBOX_WORK).
    "sandbox.file.modify": _FILES,
    # ── Sandbox / code / browser ───────────────────────────────────────────
    "sandbox.create": _SANDBOX,
    "sandbox.exec": _SANDBOX_WORK,
    "sandbox.exec.write_unknown": _SANDBOX_WORK,
    "sandbox.respond": _SANDBOX_WORK,
    "sandbox.cancel": _SANDBOX_WORK,
    # Runtime bash routes only non-mutating commands here (writes/deletes are
    # file keys); they run locally read-only or in a network-less sandbox, and
    # the runtime ``code`` tool only validates a dashboard bundle.
    "cli.exec": _effect("reversible", "unknown", "Isolated command with no outside effect."),
    # Local-worker steps run on the user's own machine (local_worker_dispatch).
    "bash.run": _effect("external", "unknown", "Runs a shell command on the user's local worker."),
    "code.run": _effect("external", "unknown", "Runs a coding agent on the user's local worker."),
    # Clicks, fills and runs JavaScript on a live public web page.
    "browser.interact": _effect("external", "unknown", "Acts on a live web page."),
    # Basic Chrome navigation, form filling and non-final clicks (the Chrome
    # call classifier's ``ordinary`` calls). Its factory writes the read and
    # file-selection effects of the same key; final submits, sensitive input
    # and publishing are their own ``chrome.<category>.confirm`` keys.
    "chrome.browser.interact": _effect("reversible", "browser", "Basic browsing in the user's Chrome."),
    "chrome.publication.publish": _PUBLISHING,
    # ── Messages / publishing ──────────────────────────────────────────────
    "external_message.send": _COMMUNICATION,
    "external_presence.update": _COMMUNICATION,
    "channel.reply": _COMMUNICATION,
    "email.send": _COMMUNICATION,
    "email.draft": _COMMUNICATION,
    "email.delete": _DELETION,
    "workspace.send_email": _COMMUNICATION,
    "workspace.notify_members": _COMMUNICATION,
    "social_post.publish": _PUBLISHING,
    "social_post.mutate": _PUBLISHING,
    "social_post.delete": _DELETION,
    "facebook.create_live_video": _PUBLISHING,
    "facebook.end_live_video": _PUBLISHING,
    "external.action": _effect("external", "unknown", "Plan step with an undeclared external effect."),
    # ── Human review / input and hand-offs ─────────────────────────────────
    "task.approve": _WORKSPACE,
    "task.recover": _WORKSPACE,
    "human.input.request": _HUMAN_INPUT,
    "chat.request_input": _HUMAN_INPUT,
    "task.request_input": _HUMAN_INPUT,
    "workspace.service.delegate": _leaf(
        "reversible", "workspace",
        "Hands work to another Agent; every leaf call it makes crosses the gate in its own loop.",
    ),
}

ACTION_EFFECTS: Mapping[str, ActionEffect] = MappingProxyType(_DECLARED)

# The Sandbox tool's own action keys (the classifier's ``sandbox`` composite
# and its legacy per-action aliases); they run by default (_SANDBOX_WORK and
# the sandbox scratch file). Other ``sandbox.*`` keys (e.g. delegating to an
# external agent) are not Sandbox tool work. tests/test_sandbox_approval_defaults.py
# derives this set from the classifier over every SandboxToolAction.
SANDBOX_TOOL_ACTION_KEYS = frozenset({
    "sandbox.exec",
    "sandbox.exec.write_unknown",
    "sandbox.file.modify",
    "sandbox.cancel",
    "sandbox.respond",
})

# Keys whose effect is the effect of another operation, resolved by the
# runtime gate from durable state at call time (never declared statically).
# ``workspace.hitl.resolve`` relays a decision on a card: approving a review
# card applies its draft, rejecting discards it, responding relays the user's
# own answer to an input card (human input). Unresolvable -> unknown (ask).
GATE_RESOLVED_ACTION_KEYS = frozenset({"workspace.hitl.resolve"})

# Namespaces minted by exactly one factory. Each entry names its owner; keys in
# these namespaces are never produced by any other code path.
_CONTROL_EFFECT = _leaf(
    "reversible", "unknown",
    "Loop control executes nothing itself (runtime.control.*, minted by the runtime classifier).",
)
_FAMILY_EFFECTS: tuple[tuple[str, str, ActionEffect], ...] = (
    # (prefix, suffix, effect)
    ("runtime.control.", "", _CONTROL_EFFECT),
    ("tool.", ".read", ActionEffect(
        "read", "read", EffectGate.POLICY,
        "READ_ONLY authorization key minted by the runtime classifier.",
    )),
    ("workspace.proposal.", "", _effect(
        "external", "automation", "Strategist proposal cohort (packages.core.proposals.constants).",
    )),
)
# Discovery tools read only the Agent's own tool/skill catalog or its own
# context archive, never user data. They do not cross the consent gate even
# under an explicit ``read: ask`` (owner decision Q3); hard authorization still
# applies. acquire_capabilities search/prepare stay ordinary READ_ONLY reads.
DISCOVERY_LEAF_TOOL_NAMES = frozenset({
    "search_tools", "list_skills", "get_skill_details", "read_context_archive",
})
_EXACT_FAMILY_EFFECTS: Mapping[str, ActionEffect] = MappingProxyType({
    "skill.invoke": _leaf(
        "reversible", "unknown",
        "Every nested skill tool call re-enters the runtime authorization boundary.",
    ),
    **{"tool." + name + ".read": _leaf(
        "read", "read", "Reads only the Agent's own discovery catalog or context archive.",
    ) for name in DISCOVERY_LEAF_TOOL_NAMES},
    # The closed set of basic Chrome reads (page, tab and target state, read-
    # only CDP) is browsing: the browser category, not the read category.
    # Other Chrome reads (clipboard, recordings, downloads) stay plain reads.
    **{"tool.mcp__chrome__" + name + ".read": ActionEffect(
        "read", "browser", EffectGate.POLICY, "Basic Chrome read (minted by the runtime classifier).",
    ) for name in (*CHROME_READ_ACTIONS, "send_cdp")},
})


def declared_action_effect(action_key: str | None) -> ActionEffect | None:
    """The declared effect for one action key, or ``None`` when undeclared."""
    key = str(action_key or "").strip()
    if not key:
        return None
    effect = ACTION_EFFECTS.get(key) or _EXACT_FAMILY_EFFECTS.get(key)
    if effect is not None:
        return effect
    for prefix, suffix, family in _FAMILY_EFFECTS:
        if key.startswith(prefix) and key.endswith(suffix) and len(key) > len(prefix) + len(suffix):
            return family
    return None


def internal_action_namespaces() -> frozenset[str]:
    """First segments of the keys only Manor's own factories mint.

    A dotted key outside these is a dynamic ``<mcp server>.<action>`` key.
    Servers that also have declared keys (``email``, ``facebook``) stay open.
    """
    namespaces = {key.split(".", 1)[0] for key in (*ACTION_EFFECTS, *_EXACT_FAMILY_EFFECTS)}
    namespaces.update(prefix.split(".", 1)[0] for prefix, _, _ in _FAMILY_EFFECTS)
    return frozenset(namespaces - set(MCP_SERVER_CATEGORIES))


def strict_effect_contract() -> bool:
    return os.getenv(STRICT_EFFECT_CONTRACT_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


# MCP server families → approval category. Impact always comes from the
# shared MCPActionEffect (READ → read, WRITE → external, DESTRUCTIVE →
# destructive); an unlisted server keeps the honest ``unknown`` category.
MCP_SERVER_CATEGORIES: Mapping[str, ApprovalCategory] = MappingProxyType({
    **{server: "communication" for server in (
        "email", "gmail", "outlook", "telegram", "wechat_official", "wechat_personal",
        "whatsapp", "discord", "ms_teams", "twilio",
    )},
    **{server: "publishing" for server in (
        "twitter_x", "linkedin", "facebook", "instagram", "tiktok", "youtube",
        "xiaohongshu", "rednote", "producthunt",
    )},
    **{server: "purchases" for server in ("stripe", "paypal", "square", "robinhood")},
    # Storefront writes (products, listings, prices, inventory, order status)
    # change what the store's customers see: a public listing, not a purchase.
    **{server: "publishing" for server in ("shopify", "woocommerce", "tiktok_shop", "amazon")},
    # Invoices, payments and customer billing records are financial.
    "quickbooks": "purchases",
    # Events and invite responses notify attendees.
    "google_calendar": "communication",
    "ms_calendar": "communication",
})

_MCP_IMPACT_BY_EFFECT: Mapping[str, ApprovalImpact] = MappingProxyType({
    "read": "read", "write": "external", "destructive": "destructive",
})


# Per-action category where the server family's category is not the fact.
_MCP_RECORD_ACTIONS = (
    # Customer and order bookkeeping: a record, neither a purchase nor a listing.
    "stripe.create_customer", "square.create_customer", "square.update_customer",
    "shopify.create_customer", "quickbooks.create_customer", "shopify.add_order_tags",
    "woocommerce.update_order_status", "paypal.create_shipment_tracking",
    "paypal.update_shipment_tracking",
    # Organizing one's own mailbox sends nothing to anyone.
    "gmail.add_label", "gmail.archive_message", "gmail.batch_modify", "gmail.create_label",
    "gmail.mark_read", "gmail.mark_spam", "gmail.mark_unread", "gmail.untrash_message",
    "outlook.categorize", "outlook.create_folder", "outlook.flag_message", "outlook.mark_read",
    "outlook.mark_unread", "outlook.move_message", "email.mark_read", "email.mark_unread",
    "email.move_message",
)
MCP_ACTION_CATEGORY_OVERRIDES: Mapping[str, ApprovalCategory] = MappingProxyType({
    **{key: "unknown" for key in _MCP_RECORD_ACTIONS},
    # Catalog, price and stock listings are sale listings, not purchases.
    **{key: "publishing" for key in (
        "stripe.create_product", "stripe.create_price", "stripe.create_coupon",
        "paypal.create_product", "square.create_catalog_item", "square.adjust_inventory",
        "whatsapp.update_business_profile",
    )},
    # Moving mail to the trash removes it (recoverable, but a deletion).
    "gmail.trash_message": "deletion",
    "gmail.trash_thread": "deletion",
})


def mcp_action_effect(server: str | None, effect: str, action: str | None = None) -> ActionEffect:
    """The declared effect of one MCP action from its shared effect enum.

    Impact comes from the enum; the category from the per-action override,
    else the server family, else ``unknown``.
    """
    impact = _MCP_IMPACT_BY_EFFECT[str(effect)]
    if impact == "read":
        return ActionEffect("read", "read")
    if impact == "destructive":
        return ActionEffect("destructive", "deletion")
    override = MCP_ACTION_CATEGORY_OVERRIDES.get(f"{server or ''}.{action or ''}")
    return ActionEffect(impact, override or MCP_SERVER_CATEGORIES.get(str(server or ""), "unknown"))


# Provider-native approval categories (e.g. the Chrome confirmation classifier)
# mapped to Manor facts, so the user's category rules match a provider gate
# (``purchases: ask`` catches a Chrome checkout). Categories absent here have
# no factual Manor equivalent or are human-only gates (authentication, captcha,
# login, mfa, browser permission, sensitive data, browser history, clipboard,
# explicit side effects...) and stay unknown.
PROVIDER_CATEGORY_EFFECTS: Mapping[str, ActionEffect] = MappingProxyType({
    "financial_transaction": ActionEffect("external", "purchases"),
    "deletion": ActionEffect("destructive", "deletion"),
    "representational_communication": ActionEffect("external", "communication"),
    "file_upload": ActionEffect("external", "communication"),
    "notification_subscription": ActionEffect("external", "communication"),
    "youtube_upload_start": ActionEffect("external", "publishing"),
    # Installs or runs downloaded software on the user's machine.
    "software_installation": ActionEffect("external", "unknown"),
})


def provider_policy_effect(provider: str | None, policy_category: str | None) -> dict[str, str]:
    """Effect keywords for one provider-native approval gate.

    A mapped provider category is a declared fact; an unmapped one is unknown
    and follows the user's mode like any unnamed effect (one policy on every
    execution surface, Workflows included).
    """
    del provider
    effect = PROVIDER_CATEGORY_EFFECTS.get(str(policy_category or "").strip())
    if effect is None:
        return {"impact": "unknown", "category": "unknown"}
    return {"impact": effect.impact, "category": effect.category}


# Labels for the settings matrix; impact/category are always the declaration.
_CATALOG_LABELS: tuple[tuple[str, str], ...] = (
    ("tool.read_file.read", "Read a file"),
    ("chrome.browser.interact", "Use browser"),
    ("workspace.file.create", "Create a file"),
    ("workspace.file.modify", "Edit a file"),
    ("external_message.send", "Send an external message"),
    ("social_post.publish", "Publish a social post"),
    ("workspace.file.delete", "Delete a file"),
    ("workspace.automation.create", "Create an automation"),
    ("workspace.operation.draft", "Draft a workspace change"),
)


def _action_catalog() -> list[dict[str, str]]:
    rows = []
    for action_key, label in _CATALOG_LABELS:
        effect = declared_action_effect(action_key)
        if effect is None:  # pragma: no cover - guarded by tests
            raise UndeclaredEffectError(action_key)
        rows.append({"category": effect.category, "action_key": action_key,
                     "label": label, "impact": effect.impact})
    return rows


ACTION_CATALOG: list[dict[str, str]] = _action_catalog()

__all__ = [
    "ACTION_CATALOG", "ACTION_EFFECTS", "ActionEffect", "GATE_RESOLVED_ACTION_KEYS", "DISCOVERY_LEAF_TOOL_NAMES", "EffectGate",
    "MCP_SERVER_CATEGORIES", "PROVIDER_CATEGORY_EFFECTS", "SANDBOX_TOOL_ACTION_KEYS", "provider_policy_effect",
    "STRICT_EFFECT_CONTRACT_ENV", "UndeclaredEffectError", "declared_action_effect",
    "internal_action_namespaces", "mcp_action_effect", "strict_effect_contract",
]
