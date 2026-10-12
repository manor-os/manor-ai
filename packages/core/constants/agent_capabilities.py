"""Shared limits for Agent capability selection and runtime loading."""

# Exact actor-scoped catalog ids accepted for one Agent after all semantic
# selection rounds have been merged.
AGENT_CAPABILITY_SELECTION_LIMIT = 200

# Narrow Agents keep their exact first-party tool schemas in the first model
# round. Wider Agents retain the same authorization scope but discover the
# non-core schemas progressively through ``search_tools``.
AGENT_EAGER_BOUND_TOOL_LIMIT = 16

# Serialized schema budget for a narrow Agent's eagerly loaded first-party
# bindings. Count remains a defensive ceiling, while this budget catches a few
# unusually large schemas before they consume the first model round.
AGENT_EAGER_BOUND_TOOL_SCHEMA_CHAR_BUDGET = 20_000
