"""Deterministic business-to-Ledger matching for new Workspaces.

Ledger contracts are durable Workspace configuration.  Explicit
``settings.ledger_contracts`` always wins (including an explicit empty list),
while a newly created Workspace without that key receives conservative
defaults inferred from its business framing.  The resulting settings remain
portable through Workspace Blueprint export/install.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import re
from typing import Any, Iterable

from packages.core.services.content_ledger import (
    CONTENT_LEDGER_CONTRACT_ID,
    DEFAULT_CONTENT_LEDGER_DIRECTORY,
)
from packages.core.services.finance_ledger import (
    DEFAULT_FINANCE_LEDGER_DIRECTORY,
    FINANCE_LEDGER_CONTRACT_ID,
)
from packages.core.services.relationship_ledger import (
    DEFAULT_RELATIONSHIP_LEDGER_DIRECTORY,
    RELATIONSHIP_LEDGER_CONTRACT_ID,
)
from packages.core.services.recruiting_ledger import (
    DEFAULT_RECRUITING_LEDGER_DIRECTORY,
    RECRUITING_LEDGER_CONTRACT_ID,
)


@dataclass(frozen=True)
class WorkspaceLedgerDefinition:
    contract_id: str
    directory: str
    aliases: tuple[str, ...]
    signals: tuple[str, ...]

    def config(self) -> dict[str, Any]:
        return {
            "contract_id": self.contract_id,
            "schema_version": 1,
            "directory": self.directory,
        }


WORKSPACE_LEDGER_DEFINITIONS: tuple[WorkspaceLedgerDefinition, ...] = (
    WorkspaceLedgerDefinition(
        RECRUITING_LEDGER_CONTRACT_ID,
        DEFAULT_RECRUITING_LEDGER_DIRECTORY,
        ("recruiting_ledger", "hr_ledger", "people_ledger"),
        (
            "recruit", "recruiting", "hiring", "human resources", "people operations",
            "talent acquisition", "candidate", "interview", "employee onboarding",
            "招聘", "人力资源", "人才招聘", "候选人", "面试", "员工", "入职", "离职",
        ),
    ),
    WorkspaceLedgerDefinition(
        RELATIONSHIP_LEDGER_CONTRACT_ID,
        DEFAULT_RELATIONSHIP_LEDGER_DIRECTORY,
        ("relationship_ledger", "crm_ledger"),
        (
            "crm", "customer relationship", "sales pipeline", "sales", "prospect",
            "lead generation", "client", "customer", "partner", "vendor", "investor",
            "fundraising", "客户", "销售", "潜客", "合作伙伴", "供应商", "投资人", "融资",
        ),
    ),
    WorkspaceLedgerDefinition(
        FINANCE_LEDGER_CONTRACT_ID,
        DEFAULT_FINANCE_LEDGER_DIRECTORY,
        ("finance_ledger", "accounting_ledger"),
        (
            "finance", "financial", "accounting", "bookkeeping", "invoice", "billing",
            "payment", "cash flow", "revenue", "expense", "fundraising",
            "财务", "会计", "记账", "发票", "账单", "付款", "现金流", "收入", "支出", "融资",
        ),
    ),
    WorkspaceLedgerDefinition(
        CONTENT_LEDGER_CONTRACT_ID,
        DEFAULT_CONTENT_LEDGER_DIRECTORY,
        ("content_ledger", "media_ledger"),
        (
            "content", "editorial", "publishing", "media production", "social media",
            "video production", "podcast", "newsletter", "creator studio",
            "内容", "编辑", "发布", "媒体", "视频", "播客", "社交媒体", "自媒体",
        ),
    ),
)

_DEFINITIONS_BY_CONTRACT = {
    definition.contract_id: definition for definition in WORKSPACE_LEDGER_DEFINITIONS
}
_CONTRACT_BY_ALIAS = {
    alias.casefold(): definition.contract_id
    for definition in WORKSPACE_LEDGER_DEFINITIONS
    for alias in (definition.contract_id, *definition.aliases)
}


def _business_text(values: Iterable[object]) -> str:
    return " \n ".join(str(value or "").strip().casefold() for value in values if value)


def _contains_signal(text: str, signal: str) -> bool:
    needle = signal.casefold()
    if any("\u4e00" <= character <= "\u9fff" for character in needle):
        return needle in text
    return bool(re.search(rf"(?<![a-z0-9]){re.escape(needle)}(?![a-z0-9])", text))


def infer_workspace_ledger_contracts(
    *,
    name: str = "",
    description: str = "",
    category: str = "",
    kind: str = "",
    operating_context: str = "",
    primary_work: str = "",
    operating_model: object = None,
) -> list[dict[str, Any]]:
    """Return conservative, deterministic Ledger defaults for a business."""

    model = operating_model if isinstance(operating_model, dict) else {}
    text = _business_text((
        name,
        description,
        category,
        kind,
        operating_context,
        primary_work,
        model.get("business_model"),
        model.get("services"),
        model.get("goals"),
    ))
    if not text:
        return []
    return [
        definition.config()
        for definition in WORKSPACE_LEDGER_DEFINITIONS
        if any(_contains_signal(text, signal) for signal in definition.signals)
    ]


def normalize_workspace_ledger_contracts(values: object) -> list[dict[str, Any]]:
    """Validate and normalize supported manual/Blueprint Ledger configs."""

    if values is None:
        return []
    if not isinstance(values, list):
        raise ValueError("ledger_contracts must be a list")

    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in values:
        if isinstance(raw, str):
            contract_id = _CONTRACT_BY_ALIAS.get(raw.strip().casefold(), raw.strip())
            source: dict[str, Any] = {}
        elif isinstance(raw, dict):
            source = dict(raw)
            raw_contract = str(source.get("contract_id") or "").strip()
            contract_id = _CONTRACT_BY_ALIAS.get(raw_contract.casefold(), raw_contract)
        else:
            raise ValueError("Each ledger contract must be a contract id or object")
        definition = _DEFINITIONS_BY_CONTRACT.get(contract_id)
        if definition is None:
            raise ValueError(f"Unsupported Workspace Ledger contract: {contract_id or '<empty>'}")
        if contract_id in seen:
            continue
        directory = str(source.get("directory") or definition.directory).strip().strip("/")
        if (
            not directory
            or directory in {".", ".."}
            or any(part in {"", ".", ".."} for part in directory.split("/"))
        ):
            raise ValueError("Ledger directory must be Workspace-relative")
        config = deepcopy(source)
        config.update({
            "contract_id": contract_id,
            "schema_version": 1,
            "directory": directory,
        })
        normalized.append(config)
        seen.add(contract_id)
    return normalized


def settings_with_business_ledgers(
    settings: object,
    *,
    infer_if_absent: bool = True,
    name: str = "",
    description: str = "",
    category: str = "",
    kind: str = "",
    operating_context: str = "",
    primary_work: str = "",
    operating_model: object = None,
) -> dict[str, Any]:
    """Preserve explicit Ledger settings or add creation-time business matches."""

    result = deepcopy(settings) if isinstance(settings, dict) else {}
    if "ledger_contracts" in result:
        result["ledger_contracts"] = normalize_workspace_ledger_contracts(
            result.get("ledger_contracts")
        )
        return result
    if not infer_if_absent:
        return result
    inferred = infer_workspace_ledger_contracts(
        name=name,
        description=description,
        category=category,
        kind=kind,
        operating_context=operating_context,
        primary_work=primary_work,
        operating_model=operating_model,
    )
    if inferred:
        result["ledger_contracts"] = inferred
        result["ledger_matching"] = {
            "version": 1,
            "source": "workspace_business",
            "contract_ids": [item["contract_id"] for item in inferred],
        }
    return result


__all__ = [
    "WORKSPACE_LEDGER_DEFINITIONS",
    "WorkspaceLedgerDefinition",
    "infer_workspace_ledger_contracts",
    "normalize_workspace_ledger_contracts",
    "settings_with_business_ledgers",
]
