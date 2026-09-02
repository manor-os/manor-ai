from packages.core.services.recruiting_ledger import RECRUITING_LEDGER_CONTRACT_ID
from packages.core.services.workspace_ledger_matching import (
    infer_workspace_ledger_contracts,
    settings_with_business_ledgers,
)
from packages.core.services.ledger_query_service import workspace_ledger_runtime_tools
from packages.core.ai.runtime.tool_visibility import runtime_expand_workspace_ledger_tools


def _contracts(values: list[dict]) -> list[str]:
    return [str(item["contract_id"]) for item in values]


def test_recruiting_workspace_gets_recruiting_hr_ledger() -> None:
    matched = infer_workspace_ledger_contracts(
        name="Talent Operations",
        operating_context="Manage recruiting, candidate interviews, and employee onboarding.",
        primary_work="Move candidates through the hiring pipeline.",
    )

    assert _contracts(matched) == [RECRUITING_LEDGER_CONTRACT_ID]
    assert matched[0]["directory"] == "recruiting-ledger"


def test_business_matcher_supports_chinese_and_multiple_business_domains() -> None:
    matched = infer_workspace_ledger_contracts(
        name="融资与投资人关系",
        primary_work="管理投资人、融资、收入和支出",
    )

    assert _contracts(matched) == [
        "manor.relationship_ledger/v1",
        "manor.finance_ledger/v1",
    ]


def test_explicit_ledger_configuration_is_authoritative() -> None:
    disabled = settings_with_business_ledgers(
        {"ledger_contracts": []},
        name="Recruiting and HR",
    )
    assert disabled["ledger_contracts"] == []
    assert "ledger_matching" not in disabled

    explicit = settings_with_business_ledgers(
        {"ledger_contracts": ["hr_ledger"]},
        name="Finance and sales",
    )
    assert _contracts(explicit["ledger_contracts"]) == [RECRUITING_LEDGER_CONTRACT_ID]


def test_blueprint_materialization_does_not_infer_undeclared_ledgers() -> None:
    settings = settings_with_business_ledgers(
        {},
        infer_if_absent=False,
        name="Recruiting and HR",
        primary_work="Manage candidates and employee onboarding",
    )

    assert "ledger_contracts" not in settings
    assert "ledger_matching" not in settings


def test_installed_contract_exposes_recruiting_tools_to_workspace_master() -> None:
    settings = {"ledger_contracts": [RECRUITING_LEDGER_CONTRACT_ID]}
    assert workspace_ledger_runtime_tools(settings) == {"read_recruiting_ledger"}
    assert workspace_ledger_runtime_tools(settings, include_write=True) == {
        "read_recruiting_ledger",
        "record_recruiting_ledger",
    }


def test_recruiting_read_binding_exposes_generic_query_tool() -> None:
    assert runtime_expand_workspace_ledger_tools({"read_recruiting_ledger"}) == {
        "read_recruiting_ledger",
        "query_ledger",
        "visualize_workspace_ledgers",
    }
