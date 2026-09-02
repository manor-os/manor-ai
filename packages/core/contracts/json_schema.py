"""Shared JSON Schema dialect and validation contract for Manor runtimes."""

from __future__ import annotations

from enum import Enum
from typing import Any

from jsonschema import Draft202012Validator
from referencing import Registry
from referencing.jsonschema import DRAFT202012


class SchemaContractError(ValueError):
    """A JSON Schema cannot be used as a deterministic runtime contract."""


class JsonSchemaDialect(str, Enum):
    """JSON Schema dialects accepted by Manor runtime contracts."""

    DRAFT_2020_12 = "https://json-schema.org/draft/2020-12/schema"


class _SchemaConstraintState(str, Enum):
    """Whether a valid schema is open, closed, or filters some JSON values."""

    OPEN = "open"
    CLOSED = "closed"
    CONSTRAINING = "constraining"


_SINGLE_SUBSCHEMA_KEYS = frozenset(
    {
        "additionalProperties",
        "contains",
        "contentSchema",
        "else",
        "if",
        "items",
        "not",
        "propertyNames",
        "then",
        "unevaluatedItems",
        "unevaluatedProperties",
    }
)
_SUBSCHEMA_MAP_KEYS = frozenset(
    {"$defs", "dependentSchemas", "patternProperties", "properties"}
)
_SUBSCHEMA_LIST_KEYS = frozenset({"allOf", "anyOf", "oneOf", "prefixItems"})
_SCHEMA_ROOT_URI = "urn:manor:json-schema-contract"
_JSON_BASE_TYPES = frozenset(
    {"array", "boolean", "null", "number", "object", "string"}
)
_JSON_TYPE_ATOMS = frozenset(
    {"array", "boolean", "integer", "non_integer_number", "null", "object", "string"}
)
_SAME_INSTANCE_SUBSCHEMA_KEYS = frozenset({"not"})
_SAME_INSTANCE_SUBSCHEMA_MAP_KEYS = frozenset({"dependentSchemas"})
_SAME_INSTANCE_SUBSCHEMA_LIST_KEYS = frozenset({"allOf", "anyOf", "oneOf"})


def _iter_schema_nodes(schema: Any):
    """Yield schemas without descending into const/default instance data."""

    if not isinstance(schema, dict):
        return
    yield schema
    for key in _SINGLE_SUBSCHEMA_KEYS:
        child = schema.get(key)
        if isinstance(child, dict):
            yield from _iter_schema_nodes(child)
    for key in _SUBSCHEMA_MAP_KEYS:
        children = schema.get(key)
        if isinstance(children, dict):
            for child in children.values():
                yield from _iter_schema_nodes(child)
    for key in _SUBSCHEMA_LIST_KEYS:
        children = schema.get(key)
        if isinstance(children, list):
            for child in children:
                yield from _iter_schema_nodes(child)


def _resource_registry(schema: dict[str, Any]) -> tuple[Registry, str]:
    """Build an offline registry containing the root and embedded resources."""

    try:
        resource = DRAFT202012.create_resource(schema)
        root_uri = str(resource.id() or _SCHEMA_ROOT_URI)
        registry = Registry().with_resource(root_uri, resource).crawl()
    except Exception as exc:  # noqa: BLE001 - normalize referencing errors
        raise SchemaContractError(f"invalid schema resource graph: {exc}") from exc
    return registry, root_uri


def _child_resolver(resolver: Any, child: Any) -> Any:
    if not isinstance(child, dict) or "$id" not in child:
        return resolver
    try:
        return resolver.in_subresource(DRAFT202012.create_resource(child))
    except Exception as exc:  # noqa: BLE001 - normalize referencing errors
        raise SchemaContractError(f"invalid nested schema resource: {exc}") from exc


def _validate_resource_refs(schema: dict[str, Any], resolver: Any) -> None:
    """Resolve every ref from the resource scope in which it is declared."""

    for key in ("$ref", "$dynamicRef"):
        if key not in schema:
            continue
        ref = schema[key]
        try:
            resolved = resolver.lookup(ref)
        except Exception as exc:  # noqa: BLE001 - normalize referencing errors
            label = "unresolved local" if isinstance(ref, str) and ref.startswith("#") else "unsupported non-local"
            raise SchemaContractError(f"{label} schema ref: {ref!r}") from exc
        if not isinstance(resolved.contents, (dict, bool)):
            raise SchemaContractError(
                f"schema ref {ref!r} does not target a schema"
            )

    for key in _SINGLE_SUBSCHEMA_KEYS:
        child = schema.get(key)
        if isinstance(child, dict):
            _validate_resource_refs(child, _child_resolver(resolver, child))
    for key in _SUBSCHEMA_MAP_KEYS:
        children = schema.get(key)
        if isinstance(children, dict):
            for child in children.values():
                if isinstance(child, dict):
                    _validate_resource_refs(child, _child_resolver(resolver, child))
    for key in _SUBSCHEMA_LIST_KEYS:
        children = schema.get(key)
        if isinstance(children, list):
            for child in children:
                if isinstance(child, dict):
                    _validate_resource_refs(child, _child_resolver(resolver, child))


def _validate_productive_ref_recursion(schema: dict[str, Any], resolver: Any) -> None:
    """Reject reference cycles that can recurse without consuming input.

    Recursive object and array contracts are valid when the recursive edge is
    reached through a child value (for example ``properties.child``). Direct
    ref/composition cycles keep evaluating the same instance and make the
    runtime validator recurse forever.
    """

    scanned: set[int] = set()

    def condition_truth_state(condition: Any) -> bool | None:
        if condition is True or condition is False:
            return condition
        domain = _pure_type_domain(condition)
        if domain == _JSON_TYPE_ATOMS:
            return True
        if domain == frozenset():
            return False
        return None

    def same_instance_children(node: dict[str, Any], node_resolver: Any):
        for key in ("$ref", "$dynamicRef"):
            if key in node:
                resolved = node_resolver.lookup(node[key])
                if isinstance(resolved.contents, dict):
                    yield resolved.contents, resolved.resolver
        for key in _SAME_INSTANCE_SUBSCHEMA_KEYS:
            child = node.get(key)
            if isinstance(child, dict):
                yield child, _child_resolver(node_resolver, child)
        if "if" in node:
            condition = node["if"]
            if isinstance(condition, dict):
                yield condition, _child_resolver(node_resolver, condition)
            truth_state = condition_truth_state(condition)
            branch_keys = (
                ("then",)
                if truth_state is True
                else ("else",)
                if truth_state is False
                else ("then", "else")
            )
            for key in branch_keys:
                child = node.get(key)
                if isinstance(child, dict):
                    yield child, _child_resolver(node_resolver, child)
        for key in _SAME_INSTANCE_SUBSCHEMA_MAP_KEYS:
            children = node.get(key)
            if isinstance(children, dict):
                for child in children.values():
                    if isinstance(child, dict):
                        yield child, _child_resolver(node_resolver, child)
        for key in _SAME_INSTANCE_SUBSCHEMA_LIST_KEYS:
            children = node.get(key)
            if isinstance(children, list):
                for child in children:
                    if isinstance(child, dict):
                        yield child, _child_resolver(node_resolver, child)

    def detect(node: dict[str, Any], node_resolver: Any, active: set[int]) -> None:
        identity = id(node)
        if identity in active:
            raise SchemaContractError(
                "non-consuming recursive schema ref would not terminate validation"
            )
        active.add(identity)
        try:
            for child, child_resolver in same_instance_children(node, node_resolver):
                detect(child, child_resolver, active)
        finally:
            active.remove(identity)

    def scan(node: dict[str, Any], node_resolver: Any) -> None:
        identity = id(node)
        if identity in scanned:
            return
        scanned.add(identity)
        detect(node, node_resolver, set())
        for key in _SINGLE_SUBSCHEMA_KEYS - {"if", "then", "else"}:
            child = node.get(key)
            if isinstance(child, dict):
                scan(child, _child_resolver(node_resolver, child))
        if "if" in node:
            condition = node["if"]
            if isinstance(condition, dict):
                scan(condition, _child_resolver(node_resolver, condition))
            truth_state = condition_truth_state(condition)
            branch_keys = (
                ("then",)
                if truth_state is True
                else ("else",)
                if truth_state is False
                else ("then", "else")
            )
            for key in branch_keys:
                child = node.get(key)
                if isinstance(child, dict):
                    scan(child, _child_resolver(node_resolver, child))
        for key in _SUBSCHEMA_MAP_KEYS:
            children = node.get(key)
            if isinstance(children, dict):
                for child in children.values():
                    if isinstance(child, dict):
                        scan(child, _child_resolver(node_resolver, child))
        for key in _SUBSCHEMA_LIST_KEYS:
            children = node.get(key)
            if isinstance(children, list):
                for child in children:
                    if isinstance(child, dict):
                        scan(child, _child_resolver(node_resolver, child))

    scan(schema, resolver)


def iter_schema_refs(schema: Any):
    """Yield references declared by actual schema nodes."""

    for node in _iter_schema_nodes(schema):
        for key in ("$ref", "$dynamicRef"):
            if key in node:
                yield node[key]


class SchemaContractValidatorFactory:
    """Build every runtime validator with one dialect and format policy."""

    _format_checker = Draft202012Validator.FORMAT_CHECKER

    @classmethod
    def assertion_keywords(cls) -> frozenset[str]:
        """Keywords that can actively constrain a runtime JSON value."""

        return frozenset(Draft202012Validator.VALIDATORS)

    @classmethod
    def supported_formats(cls) -> frozenset[str]:
        return frozenset(cls._format_checker.checkers)

    @classmethod
    def build(cls, schema: dict[str, Any]) -> Draft202012Validator:
        registry, _ = _validated_contract(schema)
        return Draft202012Validator(
            schema,
            registry=registry,
            format_checker=cls._format_checker,
        )

    @classmethod
    def constrains_values(cls, schema: dict[str, Any]) -> bool:
        """Return whether a valid contract rejects at least one JSON value class.

        This deliberately recognizes common no-op schemas instead of equating
        validator keyword registration with a concrete producer contract.
        """

        _, state = _validated_contract(schema)
        return state is _SchemaConstraintState.CONSTRAINING


def _pure_type_domain(schema: Any) -> frozenset[str] | None:
    """Resolve schemas composed only from JSON types and boolean operators."""

    if schema is True:
        return _JSON_TYPE_ATOMS
    if schema is False:
        return frozenset()
    if not isinstance(schema, dict):
        return None

    assertion_keys = set(schema) & set(Draft202012Validator.VALIDATORS)
    if not assertion_keys.issubset({"allOf", "anyOf", "not", "oneOf", "type"}):
        return None

    domain = set(_JSON_TYPE_ATOMS)
    declared_types = schema.get("type")
    if declared_types is not None:
        values = [declared_types] if isinstance(declared_types, str) else declared_types
        type_domain: set[str] = set()
        for value in values:
            if value == "number":
                type_domain.update({"integer", "non_integer_number"})
            elif value == "integer":
                type_domain.add("integer")
            else:
                type_domain.add(value)
        domain.intersection_update(type_domain)

    all_of = schema.get("allOf")
    if isinstance(all_of, list):
        for child in all_of:
            child_domain = _pure_type_domain(child)
            if child_domain is None:
                return None
            domain.intersection_update(child_domain)

    any_of = schema.get("anyOf")
    if isinstance(any_of, list):
        child_domains = [_pure_type_domain(child) for child in any_of]
        if any(child is None for child in child_domains):
            return None
        accepted = set().union(*(child for child in child_domains if child is not None))
        domain.intersection_update(accepted)

    one_of = schema.get("oneOf")
    if isinstance(one_of, list):
        child_domains = [_pure_type_domain(child) for child in one_of]
        if any(child is None for child in child_domains):
            return None
        accepted = {
            atom
            for atom in _JSON_TYPE_ATOMS
            if sum(atom in child for child in child_domains if child is not None) == 1
        }
        domain.intersection_update(accepted)

    if "not" in schema:
        child_domain = _pure_type_domain(schema["not"])
        if child_domain is None:
            return None
        domain.difference_update(child_domain)

    return frozenset(domain)


def _assertion_view(schema: Any) -> Any:
    if not isinstance(schema, dict):
        return schema
    assertion_keywords = set(Draft202012Validator.VALIDATORS)
    return {
        key: value
        for key, value in schema.items()
        if key in assertion_keywords
    }


def _are_direct_complements(left: Any, right: Any) -> bool:
    if any(True for _ in iter_schema_refs(left)) or any(
        True for _ in iter_schema_refs(right)
    ):
        # Textually equal refs can resolve to different schemas under nested
        # resource scopes. Keep those contracts constraining unless their
        # resolved semantics are proven equivalent.
        return False
    left_view = _assertion_view(left)
    right_view = _assertion_view(right)
    return (
        isinstance(left_view, dict)
        and set(left_view) == {"not"}
        and _assertion_view(left_view["not"]) == right_view
    ) or (
        isinstance(right_view, dict)
        and set(right_view) == {"not"}
        and _assertion_view(right_view["not"]) == left_view
    )


def _has_direct_complement_pair(schemas: list[Any]) -> bool:
    return any(
        _are_direct_complements(left, right)
        for index, left in enumerate(schemas)
        for right in schemas[index + 1:]
    )


def _finite_candidate_values(schema: Any) -> list[Any] | None:
    """Return a finite superset of every value a schema could accept."""

    if schema is False:
        return []
    if schema is True or not isinstance(schema, dict):
        return None
    if "const" in schema:
        return [schema["const"]]
    if "enum" in schema:
        return list(schema["enum"])

    all_of = schema.get("allOf")
    if isinstance(all_of, list):
        for child in all_of:
            candidates = _finite_candidate_values(child)
            if candidates is not None:
                return candidates

    for key in ("anyOf", "oneOf"):
        children = schema.get(key)
        if isinstance(children, list):
            candidates_by_child = [
                _finite_candidate_values(child) for child in children
            ]
            if all(candidates is not None for candidates in candidates_by_child):
                return [
                    candidate
                    for candidates in candidates_by_child
                    if candidates is not None
                    for candidate in candidates
                ]
    return None


def _all_of_state(states: list[_SchemaConstraintState]) -> _SchemaConstraintState:
    if _SchemaConstraintState.CLOSED in states:
        return _SchemaConstraintState.CLOSED
    if _SchemaConstraintState.CONSTRAINING in states:
        return _SchemaConstraintState.CONSTRAINING
    return _SchemaConstraintState.OPEN


def _any_of_state(states: list[_SchemaConstraintState]) -> _SchemaConstraintState:
    if not states or all(state is _SchemaConstraintState.CLOSED for state in states):
        return _SchemaConstraintState.CLOSED
    if _SchemaConstraintState.OPEN in states:
        return _SchemaConstraintState.OPEN
    return _SchemaConstraintState.CONSTRAINING


def _one_of_state(states: list[_SchemaConstraintState]) -> _SchemaConstraintState:
    if not states or all(state is _SchemaConstraintState.CLOSED for state in states):
        return _SchemaConstraintState.CLOSED
    open_count = states.count(_SchemaConstraintState.OPEN)
    constraining_count = states.count(_SchemaConstraintState.CONSTRAINING)
    if open_count == 1 and constraining_count == 0:
        return _SchemaConstraintState.OPEN
    if open_count > 1 and constraining_count == 0:
        return _SchemaConstraintState.CLOSED
    return _SchemaConstraintState.CONSTRAINING


def _not_state(state: _SchemaConstraintState) -> _SchemaConstraintState:
    if state is _SchemaConstraintState.OPEN:
        return _SchemaConstraintState.CLOSED
    if state is _SchemaConstraintState.CLOSED:
        return _SchemaConstraintState.OPEN
    return _SchemaConstraintState.CONSTRAINING


def _subschema_state(schema: Any, resolver: Any, active: set[int]) -> _SchemaConstraintState:
    if schema is True:
        return _SchemaConstraintState.OPEN
    if schema is False:
        return _SchemaConstraintState.CLOSED
    if not isinstance(schema, dict):
        return _SchemaConstraintState.OPEN
    return _constraint_state(
        schema,
        _child_resolver(resolver, schema),
        active=active,
    )


def _constraint_state(
    schema: Any,
    resolver: Any,
    *,
    active: set[int],
) -> _SchemaConstraintState:
    if schema is True:
        return _SchemaConstraintState.OPEN
    if schema is False:
        return _SchemaConstraintState.CLOSED
    if not isinstance(schema, dict):
        return _SchemaConstraintState.OPEN

    pure_type_domain = _pure_type_domain(schema)
    if pure_type_domain is not None:
        if not pure_type_domain:
            return _SchemaConstraintState.CLOSED
        if pure_type_domain == _JSON_TYPE_ATOMS:
            return _SchemaConstraintState.OPEN
        return _SchemaConstraintState.CONSTRAINING

    identity = id(schema)
    if identity in active:
        # Recursive schemas are concrete unless a full fixed-point proof says
        # otherwise. This avoids treating a live recursive contract as `{}`.
        return _SchemaConstraintState.CONSTRAINING
    active.add(identity)
    try:
        states: list[_SchemaConstraintState] = []

        for key in ("$ref", "$dynamicRef"):
            if key in schema:
                resolved = resolver.lookup(schema[key])
                states.append(
                    _constraint_state(
                        resolved.contents,
                        resolved.resolver,
                        active=active,
                    )
                )

        if "const" in schema:
            states.append(_SchemaConstraintState.CONSTRAINING)
        if "enum" in schema:
            states.append(
                _SchemaConstraintState.CONSTRAINING
                if schema["enum"]
                else _SchemaConstraintState.CLOSED
            )

        declared_types = schema.get("type")
        if isinstance(declared_types, str):
            states.append(_SchemaConstraintState.CONSTRAINING)
        elif isinstance(declared_types, list):
            covered = set(declared_types)
            if not _JSON_BASE_TYPES.issubset(covered):
                states.append(_SchemaConstraintState.CONSTRAINING)

        if any(
            key in schema
            for key in (
                "exclusiveMaximum",
                "exclusiveMinimum",
                "maximum",
                "minimum",
                "multipleOf",
            )
        ):
            states.append(_SchemaConstraintState.CONSTRAINING)
        if "maxLength" in schema or schema.get("minLength", 0) > 0:
            states.append(_SchemaConstraintState.CONSTRAINING)
        if schema.get("pattern") not in (None, "") or "format" in schema:
            states.append(_SchemaConstraintState.CONSTRAINING)
        if "maxItems" in schema or schema.get("minItems", 0) > 0:
            states.append(_SchemaConstraintState.CONSTRAINING)
        if schema.get("uniqueItems") is True:
            states.append(_SchemaConstraintState.CONSTRAINING)
        if "maxProperties" in schema or schema.get("minProperties", 0) > 0:
            states.append(_SchemaConstraintState.CONSTRAINING)
        if schema.get("required"):
            states.append(_SchemaConstraintState.CONSTRAINING)
        if any(schema.get("dependentRequired", {}).values()):
            states.append(_SchemaConstraintState.CONSTRAINING)

        for key in (
            "additionalProperties",
            "items",
            "propertyNames",
            "unevaluatedItems",
            "unevaluatedProperties",
        ):
            if key in schema:
                child_state = _subschema_state(schema[key], resolver, active)
                if child_state is not _SchemaConstraintState.OPEN:
                    # These applicators only govern a subset of instances
                    # (for example array items or object properties). A false
                    # child therefore constrains the parent; it does not make
                    # the parent schema globally false.
                    states.append(_SchemaConstraintState.CONSTRAINING)

        prefix_items = schema.get("prefixItems")
        if isinstance(prefix_items, list):
            if any(
                _subschema_state(child, resolver, active)
                is not _SchemaConstraintState.OPEN
                for child in prefix_items
            ):
                states.append(_SchemaConstraintState.CONSTRAINING)

        for key in ("properties", "patternProperties", "dependentSchemas"):
            children = schema.get(key)
            if isinstance(children, dict) and any(
                _subschema_state(child, resolver, active)
                is not _SchemaConstraintState.OPEN
                for child in children.values()
            ):
                states.append(_SchemaConstraintState.CONSTRAINING)

        if "contains" in schema:
            contains_state = _subschema_state(schema["contains"], resolver, active)
            minimum = schema.get("minContains", 1)
            maximum_declared = "maxContains" in schema
            if minimum > 0 or (
                maximum_declared
                and contains_state is not _SchemaConstraintState.CLOSED
            ):
                states.append(_SchemaConstraintState.CONSTRAINING)

        all_of = schema.get("allOf")
        if isinstance(all_of, list) and all_of:
            states.append(
                _all_of_state(
                    [_subschema_state(child, resolver, active) for child in all_of]
                )
            )
        any_of = schema.get("anyOf")
        if isinstance(any_of, list):
            states.append(
                _SchemaConstraintState.OPEN
                if _has_direct_complement_pair(any_of)
                else _any_of_state(
                    [_subschema_state(child, resolver, active) for child in any_of]
                )
            )
        one_of = schema.get("oneOf")
        if isinstance(one_of, list):
            states.append(
                _SchemaConstraintState.OPEN
                if len(one_of) == 2 and _are_direct_complements(*one_of)
                else _one_of_state(
                    [_subschema_state(child, resolver, active) for child in one_of]
                )
            )
        if "not" in schema:
            states.append(_not_state(_subschema_state(schema["not"], resolver, active)))

        if "if" in schema and ("then" in schema or "else" in schema):
            condition = _subschema_state(schema["if"], resolver, active)
            then_state = _subschema_state(schema.get("then", True), resolver, active)
            else_state = _subschema_state(schema.get("else", True), resolver, active)
            if condition is _SchemaConstraintState.OPEN:
                states.append(then_state)
            elif condition is _SchemaConstraintState.CLOSED:
                states.append(else_state)
            elif then_state is else_state:
                states.append(then_state)
            elif (
                then_state is not _SchemaConstraintState.OPEN
                or else_state is not _SchemaConstraintState.OPEN
            ):
                states.append(_SchemaConstraintState.CONSTRAINING)

        return _all_of_state(states)
    finally:
        active.remove(identity)


def _validated_schema_registry(schema: Any) -> Registry:
    """Validate syntax/policy and return the resolver registry used at runtime."""

    if not isinstance(schema, dict):
        raise SchemaContractError("contract schema must be an object")
    nodes = list(_iter_schema_nodes(schema))
    for node in nodes:
        declared_dialect = node.get("$schema")
        if declared_dialect is None:
            continue
        try:
            JsonSchemaDialect(declared_dialect)
        except (TypeError, ValueError) as exc:
            raise SchemaContractError(
                f"unsupported JSON Schema dialect: {declared_dialect!r}; "
                f"expected {JsonSchemaDialect.DRAFT_2020_12.value!r}"
            ) from exc
    try:
        Draft202012Validator.check_schema(schema)
    except Exception as exc:  # noqa: BLE001 - normalize validator exceptions
        raise SchemaContractError(f"invalid output contract schema: {exc}") from exc

    supported_formats = SchemaContractValidatorFactory.supported_formats()
    unsupported_formats = sorted(
        {
            declared
            for node in nodes
            if isinstance((declared := node.get("format")), str)
            if declared not in supported_formats
        }
    )
    if unsupported_formats:
        raise SchemaContractError(
            "unsupported JSON Schema format(s): " + ", ".join(unsupported_formats)
        )

    registry, root_uri = _resource_registry(schema)
    resolver = registry.resolver(root_uri)
    _validate_resource_refs(schema, resolver)
    _validate_productive_ref_recursion(schema, resolver)
    return registry


def _validated_contract(
    schema: Any,
) -> tuple[Registry, _SchemaConstraintState]:
    registry = _validated_schema_registry(schema)
    resource = DRAFT202012.create_resource(schema)
    root_uri = str(resource.id() or _SCHEMA_ROOT_URI)
    state = _constraint_state(
        schema,
        registry.resolver(root_uri),
        active=set(),
    )
    candidates = _finite_candidate_values(schema)
    if candidates is not None:
        validator = Draft202012Validator(
            schema,
            registry=registry,
            format_checker=SchemaContractValidatorFactory._format_checker,
        )
        if not any(validator.is_valid(candidate) for candidate in candidates):
            state = _SchemaConstraintState.CLOSED
    if state is _SchemaConstraintState.CLOSED:
        raise SchemaContractError("contract schema does not accept any JSON value")
    return registry, state


def validate_schema_contract(schema: Any) -> dict[str, Any]:
    """Validate the shared dialect, formats, syntax, and local references."""

    _validated_contract(schema)
    return schema
