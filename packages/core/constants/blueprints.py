"""The blueprint publication lifecycle.

A ``WorkspaceBlueprint`` row moves ``draft`` → ``pending_review`` →
``published``, and ``archived`` takes it back off the marketplace. The
vocabulary was previously four module constants inside the blueprints
*router*, which meant everything outside that router — the marketplace
listing, the draft service, the admin MCP, the platform-admin review
endpoints, the architecture tools — spelled the values by hand instead.

Note that "published" and "archived" are also used by unrelated tables
(agent reviews, memories, workspaces). This enum is the blueprint's
vocabulary only; a shared spelling is not a shared meaning.
"""
from __future__ import annotations

from enum import Enum


# Inline Knowledge is copied into the Blueprint JSON document and therefore
# passes through API memory, secret scanning, JSON serialization, and JSONB.
# Keep these budgets centralized so request, export, validation, and install
# cannot drift onto different resource limits.
BLUEPRINT_KNOWLEDGE_MAX_DOCUMENTS = 64
BLUEPRINT_KNOWLEDGE_MAX_DOCUMENT_BYTES = 1024 * 1024
BLUEPRINT_KNOWLEDGE_MAX_TOTAL_BYTES = 8 * 1024 * 1024
BLUEPRINT_KNOWLEDGE_DOCUMENT_KEY_MAX_LENGTH = 160
BLUEPRINT_KNOWLEDGE_LIST_PAGE_SIZE = 50
BLUEPRINT_KNOWLEDGE_LIST_MAX_PAGE_SIZE = 100


class BlueprintStatus(str, Enum):
    """Every state a workspace blueprint can hold."""

    #: Being edited by its author; not listed anywhere.
    DRAFT = "draft"

    #: Submitted for platform review, awaiting an admin decision.
    PENDING_REVIEW = "pending_review"

    #: Live on the workspace marketplace and installable.
    PUBLISHED = "published"

    #: Withdrawn from the marketplace. Existing installs are unaffected —
    #: an install is a copy, not a link.
    ARCHIVED = "archived"

    @classmethod
    def values(cls) -> list[str]:
        return [member.value for member in cls]


class BlueprintPurchaseStatus(str, Enum):
    """Canonical entitlement states for a paid Blueprint purchase."""

    PENDING = "pending"
    REFUND_PENDING = "refund_pending"
    COMPLETED = "completed"
    DISPUTED = "disputed"
    REFUNDED = "refunded"


class BlueprintCheckoutAttemptStatus(str, Enum):
    """Immutable Stripe Checkout attempt lifecycle."""

    PENDING = "pending"
    EXPIRED = "expired"
    COMPLETED = "completed"
    REFUND_PENDING = "refund_pending"
    REFUNDED = "refunded"


class BlueprintCheckoutRefundStatus(str, Enum):
    """Durable compensating-refund job lifecycle."""

    PENDING = "pending"
    PROCESSING = "processing"
    SUCCEEDED = "succeeded"


#: The states from which a blueprint may be edited or submitted — i.e. not
#: currently listed or under review.
BLUEPRINT_EDITABLE_STATUSES: tuple[BlueprintStatus, ...] = (
    BlueprintStatus.DRAFT,
    BlueprintStatus.ARCHIVED,
)


class BlueprintInstallTodoKind(str, Enum):
    """Stable machine-readable setup work emitted by Blueprint install."""

    CHANNEL = "channel"
    BROWSER_SESSION = "browser_session"
    MISSING_AGENT = "missing_agent"
    MISSING_INTEGRATION = "missing_integration"
    MCP_SERVER = "mcp_server"
    MCP_CONFIGURATION = "mcp_configuration"
    MISSING_SKILL = "missing_skill"
    KNOWLEDGE_PACK_CONTENT = "knowledge_pack_content"
    POST_INSTALL_CHECK = "post_install_check"
    BLOCKING_SETUP = "blocking_setup"
    LIVE_SETUP_CONTRACT = "live_setup_contract"
    NOTE = "note"


class BlueprintInstallRequirementKind(str, Enum):
    """Account-scoped requirements that can be checked before installation."""

    INTEGRATION = "integration"
    CHANNEL = "channel"
    BROWSER_SESSION = "browser_session"


def installed_blueprint_job_id(job_id: object, workspace_id: object) -> str:
    """Return the concrete globally unique id for a Blueprint job install."""

    base = str(job_id or "").strip()
    workspace_suffix = str(workspace_id or "").strip()[-8:]
    suffix = f"-{workspace_suffix}" if workspace_suffix else ""
    if suffix and base.endswith(suffix):
        return base
    return f"{base}{suffix}"


# These rows are projections of Workspace/Goal runtime configuration. Exporting
# them as ordinary Blueprint jobs and then reinstalling the runtime schedules
# creates duplicate or source-id-dependent automation.
DERIVED_WORKSPACE_SCHEDULE_PREFIXES: tuple[str, ...] = (
    "sr:",
    "oe:",
    "cie:",
    "gm:",
)

DERIVED_ONLY_SCHEDULE_EXECUTION_TYPES: frozenset[str] = frozenset({
    "outcome_evaluation",
    "chat_insight_extraction",
    "goal_measurement",
})


def is_runtime_derived_scheduled_job(
    *,
    job_id: object,
    execution_type: object,
) -> bool:
    """Whether a ScheduledJob must be recreated from portable runtime config."""

    normalized_id = str(job_id or "").strip()
    normalized_type = str(execution_type or "").strip()
    return (
        normalized_id.startswith(DERIVED_WORKSPACE_SCHEDULE_PREFIXES)
        or normalized_type in DERIVED_ONLY_SCHEDULE_EXECUTION_TYPES
    )
