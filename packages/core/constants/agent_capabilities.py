"""Shared limits for Agent capability selection and runtime loading."""

# Exact actor-scoped catalog ids accepted for one Agent after all semantic
# selection rounds have been merged.
AGENT_CAPABILITY_SELECTION_LIMIT = 200

# Narrow Agents keep their exact first-party tool schemas in the first model
# round. Wider Agents retain the same authorization scope but discover the
# non-core schemas progressively through ``search_tools``.
AGENT_EAGER_BOUND_TOOL_LIMIT = 16
