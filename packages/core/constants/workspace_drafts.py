"""Internal persistence keys for conversational Workspace drafts."""

WORKSPACE_DRAFT_SCHEMA_VERSION_FIELD = "_draft_schema_version"
CURRENT_WORKSPACE_DRAFT_SCHEMA_VERSION = 4
CREATION_PREFERENCES_FIELD = "_creation_preferences"


def uses_ui_runtime_mode(fields: dict) -> bool:
    """New drafts default to automatic; only the creation UI changes mode."""
    version = fields.get(WORKSPACE_DRAFT_SCHEMA_VERSION_FIELD)
    return isinstance(version, int) and version >= 4
