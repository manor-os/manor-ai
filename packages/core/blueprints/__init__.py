"""Workspace Blueprints — shareable workspace configuration packages.

Three pieces:

  payload.py    The portable JSON document schema (what's IN a blueprint).
  exporter.py   workspace → blueprint payload (sanitises secrets,
                replaces IDs with portable handles, drops runtime data).
  installer.py  blueprint payload → new workspace (simulate or live mode).
                Returns an InstallResult with a todo list of unmet
                requirements (channels to pair, sessions to capture).
  promote.py    sandbox workspace → live workspace, with preflight
                check that all required channels / sessions are bound.
"""
from packages.core.blueprints.exporter import (
    ExportError,
    export_workspace,
)
from packages.core.blueprints.installer import (
    InstallError,
    InstallMode,
    InstallResult,
    InstallTodo,
    install_blueprint,
)
from packages.core.blueprints.payload import (
    BLUEPRINT_VERSION,
    SUPPORTED_VERSIONS,
    PayloadError,
    detect_version,
    migrate_payload,
    validate_payload,
)
from packages.core.blueprints.promote import (
    PromoteError,
    PromoteResult,
    preflight_promote,
    promote_workspace,
)
from packages.core.blueprints.report import (
    ActivitySection,
    CostSection,
    CounterfactualOutcome,
    GoalPace,
    SimulationReport,
    simulate_report,
)
from packages.core.blueprints.solo_company import (
    FROZEN_AT as SOLO_COMPANY_BLUEPRINTS_FROZEN_AT,
    SOLO_COMPANY_BLUEPRINTS,
    FrozenSoloCompanyBlueprint,
    get_solo_company_blueprint,
    get_solo_company_blueprints,
    validate_solo_company_blueprints,
)

__all__ = [
    "ActivitySection",
    "BLUEPRINT_VERSION",
    "CostSection",
    "CounterfactualOutcome",
    "ExportError",
    "GoalPace",
    "InstallError",
    "InstallMode",
    "InstallResult",
    "InstallTodo",
    "PayloadError",
    "PromoteError",
    "PromoteResult",
    "SOLO_COMPANY_BLUEPRINTS",
    "SOLO_COMPANY_BLUEPRINTS_FROZEN_AT",
    "SUPPORTED_VERSIONS",
    "SimulationReport",
    "FrozenSoloCompanyBlueprint",
    "detect_version",
    "export_workspace",
    "get_solo_company_blueprint",
    "get_solo_company_blueprints",
    "install_blueprint",
    "migrate_payload",
    "preflight_promote",
    "promote_workspace",
    "simulate_report",
    "validate_solo_company_blueprints",
    "validate_payload",
]
