"""Virtual tags: rule- and override-based resource metadata inside Flux.

Virtual tags are Flux-side metadata layered over Azure's native tags
without touching the resources themselves. A value can come from four
places, and every consumer sees which one:

- ``native``   -- the Azure tag as inventoried.
- ``rule``     -- matched an active virtual-tag rule.
- ``imported`` -- loaded from an approved enrichment worksheet (DC2A).
- ``manual``   -- an explicit per-resource override.

Precedence, highest first: manual override > imported override > rule
(lower priority number wins among matching rules) > native tag. Rules
carry effective dates, versions, and an append-only audit trail in the
operational store; evaluation itself is pure and testable here.
"""
from __future__ import annotations

import fnmatch
import re
from datetime import date
from typing import Any

RULE_SOURCES = ("rule",)
OVERRIDE_SOURCES = ("manual", "imported")

CONDITION_FIELDS = {
    "subscriptionId", "subscriptionName", "resourceGroup", "resourceType",
    "region", "name", "serviceName", "meterCategory", "billingScope",
    "nativeTag",
}
CONDITION_OPERATORS = {
    "equals", "not_equals", "contains", "starts_with", "in", "exists",
    "not_exists",
}


def _norm(value: Any) -> str:
    return str(value or "").strip().lower()


def _condition_values(condition: dict[str, Any]) -> list[str]:
    value = condition.get("values", condition.get("value"))
    if isinstance(value, list):
        return [_norm(item) for item in value]
    return [_norm(value)] if value is not None else []


def condition_matches(condition: dict[str, Any], resource: dict[str, Any]) -> bool:
    """Evaluate one generalized condition, case-insensitively."""
    field = str(condition.get("field") or "")
    operator = str(condition.get("operator") or "equals")
    if field not in CONDITION_FIELDS or operator not in CONDITION_OPERATORS:
        return False
    if field == "nativeTag":
        tag_key = _norm(condition.get("key"))
        tags = {_norm(key): _norm(value) for key, value in (resource.get("tags") or {}).items()}
        present = tag_key in tags
        actual = tags.get(tag_key, "")
    else:
        actual_value = resource.get(field)
        present = actual_value is not None and str(actual_value).strip() != ""
        actual = _norm(actual_value)
    if operator == "exists":
        return present
    if operator == "not_exists":
        return not present
    values = _condition_values(condition)
    if operator == "equals":
        return bool(values) and actual == values[0]
    if operator == "not_equals":
        return not values or actual != values[0]
    if operator == "contains":
        return any(value in actual for value in values)
    if operator == "starts_with":
        return any(actual.startswith(value) for value in values)
    if operator == "in":
        return actual in set(values)
    return False


def expression_matches(expression: dict[str, Any], resource: dict[str, Any]) -> bool:
    """Evaluate nested AND/OR groups used by the first-class rule editor."""
    combinator = str(expression.get("combinator") or "and").lower()
    if combinator not in ("and", "or"):
        return False
    members = [
        condition_matches(item, resource)
        for item in expression.get("conditions", [])
        if isinstance(item, dict)
    ] + [
        expression_matches(item, resource)
        for item in expression.get("groups", [])
        if isinstance(item, dict)
    ]
    if not members:
        return False
    return all(members) if combinator == "and" else any(members)


def rule_matches(conditions: dict[str, Any], resource: dict[str, Any]) -> bool:
    """Evaluate one rule's conditions against one inventory resource.

    Every present condition must hold (AND); values inside a list
    condition are alternatives (OR). Unknown condition keys fail closed.
    """
    if "combinator" in conditions or "groups" in conditions or "conditions" in conditions:
        return expression_matches(conditions, resource)
    known = {
        "subscriptionIds",
        "resourceGroups",
        "resourceTypes",
        "regions",
        "nameContains",
        "namePatterns",
        "tagEquals",
        "tagExists",
    }
    for key in conditions:
        if key not in known:
            return False

    def norm(value: Any) -> str:
        return str(value or "").strip().lower()

    subscription = norm(resource.get("subscriptionId"))
    group = norm(resource.get("resourceGroup"))
    rtype = norm(resource.get("resourceType"))
    region = norm(resource.get("region"))
    name = norm(resource.get("name"))
    tags = {
        norm(key): str(value or "")
        for key, value in (resource.get("tags") or {}).items()
    }

    wanted = conditions.get("subscriptionIds")
    if wanted and subscription not in {norm(item) for item in wanted}:
        return False
    wanted = conditions.get("resourceGroups")
    if wanted and group not in {norm(item) for item in wanted}:
        return False
    wanted = conditions.get("resourceTypes")
    if wanted and rtype not in {norm(item) for item in wanted}:
        return False
    wanted = conditions.get("regions")
    if wanted and region not in {norm(item) for item in wanted}:
        return False
    wanted = conditions.get("nameContains")
    if wanted and not any(norm(item) in name for item in wanted):
        return False
    wanted = conditions.get("namePatterns")
    if wanted and not any(
        fnmatch.fnmatch(name, norm(item)) for item in wanted
    ):
        return False
    wanted = conditions.get("tagEquals")
    if wanted:
        for key, values in wanted.items():
            allowed = {str(item).strip().lower() for item in values}
            if tags.get(norm(key), "").strip().lower() not in allowed:
                return False
    wanted = conditions.get("tagExists")
    if wanted and not all(norm(item) in tags for item in wanted):
        return False
    return True


def rule_active(rule: dict[str, Any], on: date) -> bool:
    if str(rule.get("status") or "") != "active":
        return False
    effective_from = rule.get("effectiveFrom")
    effective_to = rule.get("effectiveTo")
    if effective_from and str(effective_from) > on.isoformat():
        return False
    if effective_to and str(effective_to) < on.isoformat():
        return False
    return True


def effective_tags(
    resource: dict[str, Any],
    rules: list[dict[str, Any]],
    overrides: list[dict[str, Any]],
    on: date,
) -> dict[str, dict[str, str]]:
    """Resolve every tag key visible on a resource with provenance.

    Returns {tagKey: {"value": ..., "source": native|rule|imported|manual,
    "ruleId"/"ruleName" when source is rule}}.
    """
    resolved: dict[str, dict[str, str]] = {}
    for key, value in (resource.get("tags") or {}).items():
        resolved[str(key)] = {"value": str(value or ""), "source": "native"}
    matching = [
        rule
        for rule in rules
        if rule_active(rule, on)
        and rule_matches(rule.get("conditions") or {}, resource)
    ]
    # Lower priority number wins; evaluate high->low so better rules
    # overwrite weaker ones deterministically.
    ordered = sorted(
        matching,
        key=lambda item: (-int(item.get("priority") or 100), str(item.get("ruleId"))),
    )
    for rule in [item for item in ordered if item.get("effect", "include") == "include"]:
        resolved[str(rule["tagKey"])] = {
            "value": str(rule.get("tagValue") or ""),
            "source": "rule",
            "ruleId": str(rule.get("ruleId") or ""),
            "ruleName": str(rule.get("name") or ""),
        }
    # Exclusions run after assignments. A blank exclusion value suppresses
    # any rule-derived value for the dimension; a value targets only that
    # assignment. Native/manual/imported values are never removed by a rule.
    for rule in [item for item in ordered if item.get("effect") == "exclude"]:
        key = str(rule["tagKey"])
        current = resolved.get(key)
        target = str(rule.get("tagValue") or "")
        if current and current.get("source") == "rule" and (
            not target or current.get("value") == target
        ):
            resolved.pop(key, None)
    ranked = {"imported": 0, "manual": 1}
    for override in sorted(
        overrides, key=lambda item: ranked.get(str(item.get("source")), 0)
    ):
        resolved[str(override["tagKey"])] = {
            "value": str(override.get("tagValue") or ""),
            "source": str(override.get("source") or "manual"),
        }
    return resolved


def validate_rule(payload: dict[str, Any]) -> list[str]:
    """Return human-readable problems with a rule definition."""
    problems: list[str] = []
    if not str(payload.get("name") or "").strip():
        problems.append("A rule name is required.")
    key = str(payload.get("tagKey") or "").strip()
    if not key or not re.fullmatch(r"[\w.:/@-]{1,120}", key):
        problems.append("tagKey must be 1-120 tag-safe characters.")
    effect = str(payload.get("effect") or "include")
    if effect not in ("include", "exclude"):
        problems.append("effect must be include or exclude.")
    if effect == "include" and not str(payload.get("tagValue") or "").strip():
        problems.append("tagValue is required.")
    if str(payload.get("status") or "active") not in ("active", "inactive"):
        problems.append("status must be active or inactive.")
    effective_from = str(payload.get("effectiveFrom") or "")
    effective_to = str(payload.get("effectiveTo") or "")
    if effective_from and effective_to and effective_from > effective_to:
        problems.append("effectiveFrom must not be after effectiveTo.")
    conditions = payload.get("conditions")
    if not isinstance(conditions, dict) or not conditions:
        problems.append("At least one condition is required.")
    else:
        generalized = (
            "combinator" in conditions or "groups" in conditions
            or "conditions" in conditions
        )
        known_only = generalized or all(
            item
            in {
                "subscriptionIds",
                "resourceGroups",
                "resourceTypes",
                "regions",
                "nameContains",
                "namePatterns",
                "tagEquals",
                "tagExists",
            }
            for item in conditions
        )
        if not known_only:
            problems.append("conditions contains unknown keys.")
        if generalized:
            def validate_group(group: dict[str, Any]) -> None:
                if str(group.get("combinator") or "and").lower() not in ("and", "or"):
                    problems.append("condition combinator must be and or or.")
                for item in group.get("conditions", []):
                    if not isinstance(item, dict):
                        problems.append("Each condition must be an object.")
                        continue
                    if item.get("field") not in CONDITION_FIELDS:
                        problems.append(f"Unknown condition field: {item.get('field')}.")
                    if item.get("operator", "equals") not in CONDITION_OPERATORS:
                        problems.append(f"Unknown condition operator: {item.get('operator')}.")
                    if item.get("field") == "nativeTag" and not str(item.get("key") or "").strip():
                        problems.append("nativeTag conditions require a key.")
                for child in group.get("groups", []):
                    if isinstance(child, dict):
                        validate_group(child)
            validate_group(conditions)
    try:
        priority = int(payload.get("priority", 100))
        if not 1 <= priority <= 1000:
            problems.append("priority must be between 1 and 1000.")
    except (TypeError, ValueError):
        problems.append("priority must be an integer.")
    return problems
