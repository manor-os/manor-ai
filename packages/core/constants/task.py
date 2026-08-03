"""Task constants — single source of truth for statuses, priorities, categories, and types.

Used by:
  - Backend: validation in routers/tasks.py, task_service.py
  - Frontend: should mirror these in Tasks.tsx
"""

# ── Statuses ──
# Covers full lifecycle: creation → scheduling → execution → resolution
TASK_STATUSES = {
    "created":              {"label": "Created",              "color": "#94a3b8", "order": 0},
    # ``proposed``: Strategist suggested this task during a review cycle
    # but the operator hasn't approved it yet. Sits in the
    # workspace_chat as a card with [Approve] [Reject]; on approve
    # dependency-ready rows flip to ``in_progress`` (which triggers
    # plan_and_run_task), while rows waiting on predecessor task output
    # stay ``pending`` until the dependency gate releases them.
    "proposed":             {"label": "Proposed",             "color": "#a78bfa", "order": 1},
    "pending":              {"label": "Pending",              "color": "#f59e0b", "order": 2},
    "scheduled":            {"label": "Scheduled",            "color": "#3b82f6", "order": 3},
    "in_progress":          {"label": "In Progress",          "color": "#2563eb", "order": 4},
    "waiting_on_customer":  {"label": "Waiting on Customer",  "color": "#f97316", "order": 5},
    "on_hold":              {"label": "On Hold",              "color": "#a855f7", "order": 6},
    "blocked":              {"label": "Blocked",              "color": "#ef4444", "order": 7},
    "completed":            {"label": "Completed",            "color": "#10b981", "order": 8},
    "cancelled":            {"label": "Cancelled",            "color": "#64748b", "order": 9},
    "failed":               {"label": "Failed",               "color": "#dc2626", "order": 10},
}

from enum import Enum


class TaskStatus(str, Enum):
    """Every state a Task row takes — one member per TASK_STATUSES key.

    The dict above stays the presentation metadata (label/color/order); this
    enum is what code BRANCHES on, so a status comparison can never typo its
    way past review. The assert below keeps the two from drifting.
    """

    CREATED = "created"
    PROPOSED = "proposed"
    PENDING = "pending"
    SCHEDULED = "scheduled"
    IN_PROGRESS = "in_progress"
    WAITING_ON_CUSTOMER = "waiting_on_customer"
    ON_HOLD = "on_hold"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"

    @classmethod
    def values(cls) -> list[str]:
        return [member.value for member in cls]


assert set(TaskStatus.values()) == set(TASK_STATUSES.keys()), (
    "TaskStatus and TASK_STATUSES drifted apart"
)

VALID_STATUSES = set(TaskStatus.values())

# Board columns (kanban view groups)
BOARD_COLUMNS = ["pending", "scheduled", "in_progress", "waiting_on_customer", "on_hold", "blocked", "completed"]

# ── Priorities ──
# 1 (lowest) to 5 (highest)
TASK_PRIORITIES = {
    5: {"label": "Critical",  "color": "#ef4444"},
    4: {"label": "High",      "color": "#f97316"},
    3: {"label": "Medium",    "color": "#eab308"},
    2: {"label": "Low",       "color": "#60a5fa"},
    1: {"label": "Minimal",   "color": "#94a3b8"},
}

# ── Categories ──
# General-purpose categories that cover most business types
TASK_CATEGORIES = [
    # Core operations
    {"key": "operations",       "label": "Operations",       "icon": "wrench",       "color": "#0f766e"},
    {"key": "maintenance",      "label": "Maintenance",      "icon": "tool",         "color": "#2563eb"},
    {"key": "housekeeping",     "label": "Housekeeping",     "icon": "sparkles",     "color": "#14b8a6"},
    {"key": "inspection",       "label": "Inspection",       "icon": "clipboard",    "color": "#0891b2"},
    {"key": "security",         "label": "Security",         "icon": "lock",         "color": "#1e293b"},
    # Customer-facing
    {"key": "support",          "label": "Support",          "icon": "headphones",   "color": "#7c3aed"},
    {"key": "customer_request", "label": "Customer Request", "icon": "chat",         "color": "#0284c7"},
    {"key": "complaint",        "label": "Complaint",        "icon": "alert",        "color": "#dc2626"},
    {"key": "onboarding",       "label": "Onboarding",       "icon": "rocket",       "color": "#8b5cf6"},
    # Business
    {"key": "sales",            "label": "Sales",            "icon": "trending-up",  "color": "#059669"},
    {"key": "finance",          "label": "Finance",          "icon": "dollar",       "color": "#d97706"},
    {"key": "procurement",      "label": "Procurement",      "icon": "shopping",     "color": "#ea580c"},
    {"key": "billing",          "label": "Billing",          "icon": "receipt",      "color": "#ca8a04"},
    # People
    {"key": "hr",               "label": "HR",               "icon": "users",        "color": "#ec4899"},
    {"key": "training",         "label": "Training",         "icon": "book",         "color": "#a855f7"},
    {"key": "recruitment",      "label": "Recruitment",      "icon": "user-plus",    "color": "#d946ef"},
    # Tech
    {"key": "development",      "label": "Development",      "icon": "code",         "color": "#6366f1"},
    {"key": "it",               "label": "IT",               "icon": "server",       "color": "#4f46e5"},
    {"key": "bug",              "label": "Bug Fix",          "icon": "bug",          "color": "#ef4444"},
    {"key": "devops",           "label": "DevOps",           "icon": "terminal",     "color": "#334155"},
    # Marketing & comms
    {"key": "marketing",        "label": "Marketing",        "icon": "megaphone",    "color": "#f43f5e"},
    {"key": "content",          "label": "Content",          "icon": "document",     "color": "#fb923c"},
    {"key": "design",           "label": "Design",           "icon": "palette",      "color": "#e879f9"},
    {"key": "social_media",     "label": "Social Media",     "icon": "globe",        "color": "#38bdf8"},
    # Logistics & facilities
    {"key": "logistics",        "label": "Logistics",        "icon": "truck",        "color": "#0ea5e9"},
    {"key": "inventory",        "label": "Inventory",        "icon": "box",          "color": "#78716c"},
    {"key": "facilities",       "label": "Facilities",       "icon": "building",     "color": "#57534e"},
    # Governance
    {"key": "compliance",       "label": "Compliance",       "icon": "shield",       "color": "#84cc16"},
    {"key": "legal",            "label": "Legal",            "icon": "scale",        "color": "#475569"},
    {"key": "audit",            "label": "Audit",            "icon": "search",       "color": "#65a30d"},
    # Misc
    {"key": "project",          "label": "Project",          "icon": "layers",       "color": "#0d9488"},
    {"key": "meeting",          "label": "Meeting",          "icon": "calendar",     "color": "#6d28d9"},
    {"key": "research",         "label": "Research",         "icon": "microscope",   "color": "#2563eb"},
    {"key": "other",            "label": "Other",            "icon": "folder",       "color": "#64748b"},
]

# ── Task Types ──
# How the task was created / what kind of work it represents
TASK_TYPES = [
    "general",          # Manual task
    "ai_generated",     # Created by AI agent
    "scheduled",        # From a recurring schedule
    "customer_request",  # Inbound from customer channel
    "incident",         # Urgent issue / incident
    "inspection",       # Routine inspection / audit
    "follow_up",        # Follow-up from previous task
    "approval",         # Requires approval workflow
]


class TaskLogType(str, Enum):
    """Every log type the backend writes to task_logs — one member per
    write site, plus the step/plan lifecycle types the dispatcher and
    executor emit. The frontend icon map (TaskLogItem.tsx LOG_ICONS) keys
    on these same strings; a cross-check test ties the two together.
    """

    CREATE = "create"
    COMMENT = "comment"
    STATUS_CHANGE = "status_change"
    ASSIGNMENT_CHANGE = "assignment_change"
    APPROVAL_DECISION = "approval_decision"
    MANUAL_RETRY = "manual_retry"
    DEPENDENCY_WAIT = "dependency_wait"
    ESCALATION = "escalation"
    REASSIGN = "reassign"
    RUNTIME_CONTEXT = "runtime_context"
    CLIENT_COMMENT = "client_comment"
    EVALUATION = "evaluation"
    AI_HITL_REQUESTED = "ai_hitl_requested"
    AI_HITL_REMINDER = "ai_hitl_reminder"
    AI_HITL_RESUMED = "ai_hitl_resumed"
    AI_SUPERVISOR_VERDICT = "ai_supervisor_verdict"
    AI_NEEDS_REPLAN = "ai_needs_replan"
    AI_EXECUTION_STARTED = "ai_execution_started"
    AI_EXECUTION_COMPLETED = "ai_execution_completed"
    AI_EXECUTION_FAILED = "ai_execution_failed"
    AI_AGENT_TURN = "ai_agent_turn"
    WORKSPACE_AGENT_RESPONSE = "workspace_agent_response"
    WORKSPACE_AGENT_ERROR = "workspace_agent_error"
    STEP_COMPLETED = "step_completed"
    STEP_FAILED = "step_failed"
    STEP_RETRYING = "step_retrying"
    PLAN_STARTED = "plan_started"
    PLAN_COMPLETED = "plan_completed"
    PLAN_FAILED = "plan_failed"
    PLAN_CANCELLED = "plan_cancelled"
    PLAN_REPLANNED = "plan_replanned"

    @classmethod
    def values(cls) -> list[str]:
        return [member.value for member in cls]

    @classmethod
    def coerce(cls, value: object, *, default: "TaskLogType") -> "TaskLogType":
        """The member for a value an agent or API caller supplied.

        Model-supplied log types are suggestions, not identifiers — an
        unknown one falls back to ``default`` rather than raising, per the
        StepResult-envelope rule (offer a vocabulary, then validate; never
        fail a task on the model's spelling).
        """
        try:
            return cls(str(value or "").strip())
        except ValueError:
            return default


#: Log types an AI agent writes. Derived from the enum by prefix once, here,
#: instead of every reader re-deciding with ``startswith("ai_")`` — a prefix
#: test silently adopts any future member whose name happens to start "ai".
AI_LOG_TYPES = frozenset(m for m in TaskLogType if m.value.startswith("ai_"))

#: What counts as conversation on a task: the agent's own entries plus the
#: comments people leave. Used to build agent-facing timelines.
CONVERSATION_LOG_TYPES = AI_LOG_TYPES | {TaskLogType.COMMENT}

#: The open/close pair for a human-input request. ``AI_HITL_RESUMED`` closes
#: whatever the other two opened.
HITL_REQUEST_LOG_TYPES = frozenset(
    {TaskLogType.AI_HITL_REQUESTED, TaskLogType.AI_HITL_REMINDER}
)
HITL_LOG_TYPES = HITL_REQUEST_LOG_TYPES | {TaskLogType.AI_HITL_RESUMED}


def plan_terminal_log_type(plan_status: str) -> str:
    """The log type for a plan reaching ``plan_status`` — the typed form of
    the old f"plan_{status}" string construction. Raises if a plan status has
    no log type, which is the point: a new terminal status must declare one.
    """
    return TaskLogType(f"plan_{plan_status}").value
