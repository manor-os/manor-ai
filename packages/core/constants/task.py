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

VALID_STATUSES = set(TASK_STATUSES.keys())

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
