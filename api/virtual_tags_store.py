"""Virtual-tags domain extracted from the 19k-line god object (see #13).

FluxDatabase delegates to this mixin so the public surface
``from api.database import FluxDatabase`` is unchanged.
"""

from __future__ import annotations

import json
import re
from datetime import date
from typing import Any
from uuid import uuid4


def _utc_now():
    from .database import utc_now
    return utc_now()

def _json_value(value):
    from .database import json_value
    return json_value(value)


class VirtualTagsMixin:
    """Virtual-tag CRUD + showback. Requires host to provide ``operational_connect``, ``connect``, etc."""

    def virtual_tag_rules(self, include_inactive: bool = True) -> list[dict[str, Any]]:  # type: ignore[no-redef]
        with self.operational_connect(read_only=True) as db:  # type: ignore[attr-defined]
            rows = db.execute(
                """
                SELECT rule_id, name, tag_key, tag_value, priority,
                       conditions_json, effect, status, effective_from, effective_to,
                       version, updated_by, updated_at
                FROM virtual_tag_rules
                ORDER BY priority, name
                """
            ).fetchall()
        rules = [
            {
                "ruleId": row[0],
                "name": row[1],
                "tagKey": row[2],
                "tagValue": row[3],
                "priority": int(row[4]),
                "conditions": json.loads(row[5] or "{}"),
                "effect": row[6],
                "status": row[7],
                "effectiveFrom": row[8].isoformat() if row[8] else None,
                "effectiveTo": row[9].isoformat() if row[9] else None,
                "version": int(row[10]),
                "updatedBy": row[11],
                "updatedAt": row[12].isoformat() if row[12] else None,
            }
            for row in rows
        ]
        if include_inactive:
            return rules
        return [rule for rule in rules if rule["status"] == "active"]

    def save_virtual_tag_rule(self, payload: dict[str, Any], actor: str) -> dict[str, Any]:  # type: ignore[no-redef]
        from .virtual_tags import validate_rule

        problems = validate_rule(payload)
        if problems:
            raise ValueError("; ".join(problems))
        rule_id = str(payload.get("ruleId") or uuid4())
        now = _utc_now()  # type: ignore[attr-defined]
        with self.operational_connect() as db:  # type: ignore[attr-defined]
            existing = db.execute(
                "SELECT version FROM virtual_tag_rules WHERE rule_id = ?",
                [rule_id],
            ).fetchone()
            version = (int(existing[0]) + 1) if existing else 1
            action = "updated" if existing else "created"
            snapshot = {
                "name": payload["name"],
                "tagKey": payload["tagKey"],
                "tagValue": str(payload.get("tagValue") or ""),
                "priority": int(payload.get("priority", 100)),
                "conditions": payload.get("conditions") or {},
                "effect": str(payload.get("effect") or "include"),
                "status": str(payload.get("status") or "active"),
                "effectiveFrom": payload.get("effectiveFrom"),
                "effectiveTo": payload.get("effectiveTo"),
            }
            if existing:
                db.execute(
                    """
                    UPDATE virtual_tag_rules
                    SET name = ?, tag_key = ?, tag_value = ?, priority = ?,
                        conditions_json = ?, effect = ?, status = ?, effective_from = ?,
                        effective_to = ?, version = ?, updated_by = ?,
                        updated_at = ?
                    WHERE rule_id = ?
                    """,
                    [
                        snapshot["name"], snapshot["tagKey"],
                        snapshot["tagValue"], snapshot["priority"],
                        _json_value(snapshot["conditions"]),  # type: ignore[attr-defined]
                        snapshot["effect"], snapshot["status"], snapshot["effectiveFrom"],
                        snapshot["effectiveTo"], version, actor, now, rule_id,
                    ],
                )
            else:
                db.execute(
                    """
                    INSERT INTO virtual_tag_rules (
                        rule_id, name, tag_key, tag_value, priority,
                        conditions_json, effect, status, effective_from,
                        effective_to, version, updated_by, updated_at
                    ) VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                    )
                    """,
                    [
                        rule_id, snapshot["name"], snapshot["tagKey"],
                        snapshot["tagValue"], snapshot["priority"],
                        _json_value(snapshot["conditions"]),  # type: ignore[attr-defined]
                        snapshot["effect"], snapshot["status"], snapshot["effectiveFrom"],
                        snapshot["effectiveTo"], version, actor, now,
                    ],
                )
            db.execute(
                "INSERT INTO virtual_tag_rule_audit VALUES (?, ?, ?, ?, ?, ?)",
                [rule_id, version, action, _json_value(snapshot), actor, now],  # type: ignore[attr-defined]
            )
        return {"ruleId": rule_id, "version": version, "action": action}

    def virtual_tag_dimensions(self, include_inactive: bool = True) -> list[dict[str, Any]]:  # type: ignore[no-redef]
        with self.operational_connect(read_only=True) as db:  # type: ignore[attr-defined]
            rows = db.execute(
                "SELECT dimension_key, name, description, status, version, "
                "updated_by, updated_at FROM virtual_tag_dimensions ORDER BY name"
            ).fetchall()
            legacy = db.execute(
                "SELECT DISTINCT tag_key FROM virtual_tag_rules UNION "
                "SELECT DISTINCT tag_key FROM virtual_tag_overrides"
            ).fetchall()
        dimensions = {
            str(row[0]).lower(): {
                "key": row[0], "name": row[1], "description": row[2],
                "status": row[3], "version": int(row[4]),
                "updatedBy": row[5],
                "updatedAt": row[6].isoformat() if row[6] else None,
                "implicit": False,
            }
            for row in rows
        }
        for row in legacy:
            key = str(row[0] or "").strip()
            if key and key.lower() not in dimensions:
                dimensions[key.lower()] = {
                    "key": key, "name": key, "description": "",
                    "status": "active", "version": 0, "updatedBy": "",
                    "updatedAt": None, "implicit": True,
                }
        result = sorted(dimensions.values(), key=lambda item: item["name"].lower())
        return result if include_inactive else [item for item in result if item["status"] == "active"]

    def save_virtual_tag_dimension(self, payload: dict[str, Any], actor: str) -> dict[str, Any]:  # type: ignore[no-redef]
        key = str(payload.get("key") or "").strip()
        name = str(payload.get("name") or key).strip()
        if not key or not re.fullmatch(r"[\w.:/@-]{1,120}", key):
            raise ValueError("key must be 1-120 tag-safe characters.")
        if not name:
            raise ValueError("name is required.")
        status = str(payload.get("status") or "active")
        if status not in ("active", "inactive"):
            raise ValueError("status must be active or inactive.")
        now = _utc_now()  # type: ignore[attr-defined]
        with self.operational_connect() as db:  # type: ignore[attr-defined]
            existing = db.execute(
                "SELECT dimension_key, version FROM virtual_tag_dimensions "
                "WHERE lower(dimension_key) = ?",
                [key.lower()],
            ).fetchone()
            version = int(existing[1]) + 1 if existing else 1
            if existing:
                key = str(existing[0])
                db.execute(
                    "UPDATE virtual_tag_dimensions SET name = ?, description = ?, "
                    "status = ?, version = ?, updated_by = ?, updated_at = ? "
                    "WHERE dimension_key = ?",
                    [name, str(payload.get("description") or ""), status,
                     version, actor, now, key],
                )
            else:
                db.execute(
                    "INSERT INTO virtual_tag_dimensions (dimension_key, name, "
                    "description, status, version, updated_by, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    [key, name, str(payload.get("description") or ""), status,
                     version, actor, now],
                )
        return {"key": key, "version": version, "status": status}

    def delete_virtual_tag_dimension(self, key: str, actor: str) -> None:  # type: ignore[no-redef]
        now = _utc_now()  # type: ignore[attr-defined]
        with self.operational_connect() as db:  # type: ignore[attr-defined]
            row = db.execute(
                "SELECT version FROM virtual_tag_dimensions WHERE lower(dimension_key) = ?",
                [key.lower()],
            ).fetchone()
            if not row:
                raise ValueError("Unknown dimension.")
            db.execute(
                "UPDATE virtual_tag_dimensions SET status = 'inactive', "
                "version = ?, updated_by = ?, updated_at = ? "
                "WHERE lower(dimension_key) = ?",
                [int(row[0]) + 1, actor, now, key.lower()],
            )

    def delete_virtual_tag_rule(self, rule_id: str, actor: str) -> None:  # type: ignore[no-redef]
        self.set_virtual_tag_rule_status(rule_id, "inactive", actor)  # type: ignore[attr-defined]

    def set_virtual_tag_rule_status(self, rule_id: str, status: str, actor: str) -> None:  # type: ignore[no-redef]
        if status not in ("active", "inactive"):
            raise ValueError("status must be active or inactive")
        now = _utc_now()  # type: ignore[attr-defined]
        with self.operational_connect() as db:  # type: ignore[attr-defined]
            row = db.execute(
                "SELECT version FROM virtual_tag_rules WHERE rule_id = ?",
                [rule_id],
            ).fetchone()
            if not row:
                raise ValueError("Unknown rule.")
            version = int(row[0]) + 1
            db.execute(
                "UPDATE virtual_tag_rules SET status = ?, version = ?, "
                "updated_by = ?, updated_at = ? WHERE rule_id = ?",
                [status, version, actor, now, rule_id],
            )
            db.execute(
                "INSERT INTO virtual_tag_rule_audit VALUES (?, ?, ?, ?, ?, ?)",
                [
                    rule_id, version,
                    "deactivated" if status == "inactive" else "activated",
                    _json_value({"status": status}), actor, now,  # type: ignore[attr-defined]
                ],
            )

    def virtual_tag_overrides_for(self, resource_ids: list[str]) -> dict[str, list[dict[str, Any]]]:  # type: ignore[no-redef]
        if not resource_ids:
            return {}
        placeholders = ", ".join("?" for _ in resource_ids)
        with self.operational_connect(read_only=True) as db:  # type: ignore[attr-defined]
            rows = db.execute(
                f"""
                SELECT resource_id, tag_key, tag_value, source, note,
                       updated_by, updated_at
                FROM virtual_tag_overrides
                WHERE resource_id IN ({placeholders})
                """,
                [item.lower() for item in resource_ids],
            ).fetchall()
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            grouped.setdefault(row[0], []).append(
                {
                    "tagKey": row[1],
                    "tagValue": row[2],
                    "source": row[3],
                    "note": row[4],
                    "updatedBy": row[5],
                    "updatedAt": row[6].isoformat() if row[6] else None,
                }
            )
        return grouped

    def import_virtual_tag_overrides(self, overrides: list[dict[str, Any]], actor: str) -> dict[str, int]:  # type: ignore[no-redef]
        now = _utc_now()  # type: ignore[attr-defined]
        applied = 0
        previous: list[dict[str, Any]] = []
        with self.operational_connect() as db:  # type: ignore[attr-defined]
            for item in overrides:
                resource_id = str(item.get("resourceId") or "").lower()
                tag_key = str(item.get("tagKey") or "").strip()
                tag_value = str(item.get("tagValue") or "").strip()
                source = str(item.get("source") or "imported")
                if not resource_id or not tag_key or not tag_value:
                    continue
                if source not in ("imported", "manual"):
                    source = "imported"
                before = db.execute(
                    "SELECT tag_value, source FROM virtual_tag_overrides "
                    "WHERE resource_id = ? AND tag_key = ?",
                    [resource_id, tag_key],
                ).fetchone()
                previous.append(
                    {
                        "resourceId": resource_id,
                        "tagKey": tag_key,
                        "previousValue": before[0] if before else None,
                        "previousSource": before[1] if before else None,
                    }
                )
                db.execute(
                    """
                    INSERT INTO virtual_tag_overrides VALUES (
                        ?, ?, ?, ?, ?, ?, ?
                    )
                    ON CONFLICT (resource_id, tag_key) DO UPDATE SET
                        tag_value = excluded.tag_value,
                        source = excluded.source,
                        note = excluded.note,
                        updated_by = excluded.updated_by,
                        updated_at = excluded.updated_at
                    """,
                    [
                        resource_id, tag_key, tag_value, source,
                        str(item.get("note") or ""), actor, now,
                    ],
                )
                applied += 1
        return {"applied": applied, "previous": previous}

    def rollback_virtual_tag_overrides(self, previous: list[dict[str, Any]], actor: str) -> dict[str, int]:  # type: ignore[no-redef]
        now = _utc_now()  # type: ignore[attr-defined]
        restored = 0
        skipped = 0
        conflicts = 0
        with self.operational_connect() as db:  # type: ignore[attr-defined]
            for item in previous:
                resource_id = str(item.get("resourceId") or "").lower()
                tag_key = str(item.get("tagKey") or "").strip()
                if not resource_id or not tag_key:
                    skipped += 1
                    continue
                current = db.execute(
                    "SELECT tag_value FROM virtual_tag_overrides "
                    "WHERE resource_id = ? AND tag_key = ?",
                    [resource_id, tag_key],
                ).fetchone()
                expected = item.get("expectedValue")
                if expected is not None and (
                    not current or str(current[0]) != str(expected)
                ):
                    conflicts += 1
                    continue
                previous_value = item.get("previousValue")
                if previous_value is None:
                    db.execute(
                        "DELETE FROM virtual_tag_overrides "
                        "WHERE resource_id = ? AND tag_key = ?",
                        [resource_id, tag_key],
                    )
                else:
                    db.execute(
                        """
                        UPDATE virtual_tag_overrides
                        SET tag_value = ?, source = ?, note = ?,
                            updated_by = ?, updated_at = ?
                        WHERE resource_id = ? AND tag_key = ?
                        """,
                        [
                            str(previous_value),
                            str(item.get("previousSource") or "imported"),
                            "Restored by governed virtual-tag rollback",
                            actor, now, resource_id, tag_key,
                        ],
                    )
                restored += 1
        return {
            "restored": restored,
            "skipped": skipped,
            "conflicts": conflicts,
        }

    def virtual_tags_preview(self, payload: dict[str, Any], limit: int = 25) -> dict[str, Any]:  # type: ignore[no-redef]
        from .virtual_tags import rule_matches, validate_rule

        problems = validate_rule(payload)
        if problems:
            raise ValueError("; ".join(problems))
        conditions = payload.get("conditions") or {}
        with self.connect(read_only=True) as db:  # type: ignore[attr-defined]
            rows = db.execute(
                """
                SELECT resource.resource_id, resource.name, resource.subscription_id, resource.resource_group,
                       resource.resource_type, resource.region, resource.tags_json,
                       COALESCE(cost.monthly_cost, 0)
                FROM resources_current AS resource
                LEFT JOIN (
                    SELECT lower(resource_id) AS resource_id,
                           sum(amount) AS monthly_cost
                    FROM costs_current WHERE cost_type = 'ActualCost'
                    GROUP BY lower(resource_id)
                ) AS cost ON cost.resource_id = lower(resource.resource_id)
                """
            ).fetchall()
        matched = []
        for row in rows:
            resource = {
                "resourceId": row[0],
                "name": row[1],
                "subscriptionId": row[2],
                "resourceGroup": row[3],
                "resourceType": row[4],
                "region": row[5],
                "tags": json.loads(row[6] or "{}"),
                "monthlyCost": float(row[7] or 0),
            }
            if rule_matches(conditions, resource):
                matched.append(resource)
        return {
            "matchedCount": len(matched),
            "totalResources": len(rows),
            "matchedMonthlyCost": round(sum(item["monthlyCost"] for item in matched), 2),
            "sample": [
                {
                    "resourceId": item["resourceId"],
                    "name": item["name"],
                    "resourceGroup": item["resourceGroup"],
                    "region": item["region"],
                    "monthlyCost": round(item["monthlyCost"], 2),
                }
                for item in matched[:limit]
            ],
        }

    def effective_virtual_tags(self, resource_id: str) -> dict[str, dict[str, str]]:  # type: ignore[no-redef]
        normalized = resource_id.lower()
        return self.effective_virtual_tags_for([resource_id]).get(normalized, {})

    def effective_virtual_tags_for(
        self, resource_ids: list[str]
    ) -> dict[str, dict[str, dict[str, str]]]:  # type: ignore[no-redef]
        """Effective tags for many resources in one pass, keyed by the
        lower-cased resource id.

        One resources_current read, one rules load, and one overrides load
        replace a connection plus three queries per resource — calling the
        single-resource variant in a loop was the pool-exhaustion path in
        remediation_package (#72)."""
        from .virtual_tags import effective_tags

        originals: dict[str, str] = {}
        for resource_id in resource_ids:
            if resource_id:
                originals.setdefault(resource_id.lower(), resource_id)
        if not originals:
            return {}
        normalized = list(originals)
        rows: dict[str, Any] = {}
        with self.connect(read_only=True) as db:  # type: ignore[attr-defined]
            placeholders = ", ".join("?" for _ in normalized)
            for row in db.execute(
                f"""
                SELECT resource_id, name, subscription_id, resource_group,
                       resource_type, region, tags_json
                FROM resources_current
                WHERE lower(resource_id) IN ({placeholders})
                """,
                normalized,
            ).fetchall():
                rows[str(row[0]).lower()] = row
        rules = self.virtual_tag_rules(include_inactive=False)  # type: ignore[attr-defined]
        overrides = self.virtual_tag_overrides_for(normalized)
        today = _utc_now().date()  # type: ignore[attr-defined]
        result: dict[str, dict[str, dict[str, str]]] = {}
        for key in normalized:
            row = rows.get(key)
            resource = {
                "resourceId": row[0] if row else originals[key],
                "name": row[1] if row else "",
                "subscriptionId": row[2] if row else "",
                "resourceGroup": row[3] if row else "",
                "resourceType": row[4] if row else "",
                "region": row[5] if row else "",
                "tags": json.loads(row[6] or "{}") if row else {},
            }
            result[key] = effective_tags(
                resource, rules, overrides.get(key, []), today
            )
        return result

    def virtual_tag_report(
        self,
        *,
        dimension: str = "",
        value: str = "",
        cost_type: str = "AmortizedCost",
        currency: str = "",
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> dict[str, Any]:  # type: ignore[no-redef]
        from .virtual_tags import effective_tags

        dimensions = self.virtual_tag_dimensions(include_inactive=False)  # type: ignore[attr-defined]
        selected = dimension or (dimensions[0]["key"] if dimensions else "")
        rules = self.virtual_tag_rules(include_inactive=False)  # type: ignore[attr-defined]
        conditions = ["cost.cost_type = ?"]
        params: list[Any] = [cost_type]
        if start_date:
            conditions.append("cost.usage_date >= ?")
            params.append(start_date)
        if end_date:
            conditions.append("cost.usage_date <= ?")
            params.append(end_date)
        with self.connect(read_only=True) as db:  # type: ignore[attr-defined]
            rows = db.execute(
                f"""
                SELECT CAST(date_trunc('month', cost.usage_date) AS DATE),
                       lower(cost.resource_id), cost.service_name,
                       sum(cost.amount), cost.currency,
                       resource.name, resource.subscription_id,
                       resource.subscription_name, resource.resource_group,
                       resource.resource_type, resource.region,
                       resource.tags_json
                FROM daily_cost_history AS cost
                LEFT JOIN resources_current AS resource
                  ON lower(resource.resource_id) = lower(cost.resource_id)
                WHERE {' AND '.join(conditions)}
                GROUP BY CAST(date_trunc('month', cost.usage_date) AS DATE),
                         lower(cost.resource_id), cost.service_name,
                         cost.currency, resource.name,
                         resource.subscription_id, resource.subscription_name,
                         resource.resource_group, resource.resource_type,
                         resource.region, resource.tags_json
                """,
                params,
            ).fetchall()
        currency_weights: dict[str, float] = {}
        for row in rows:
            code = str(row[4] or "")
            currency_weights[code] = currency_weights.get(code, 0.0) + abs(
                float(row[3] or 0)
            )
        selected_currency = currency.upper() if currency else (
            max(currency_weights, key=currency_weights.get)
            if currency_weights else "USD"
        )
        excluded_currencies = sorted(
            code for code in currency_weights
            if code and code != selected_currency
        )
        rows = [row for row in rows if str(row[4] or "") == selected_currency]
        resource_ids = sorted({str(row[1]) for row in rows if row[1]})
        overrides = self.virtual_tag_overrides_for(resource_ids)  # type: ignore[attr-defined]
        by_value: dict[str, dict[str, Any]] = {}
        monthly: dict[tuple[str, str], float] = {}
        resource_totals: dict[tuple[str, str], dict[str, Any]] = {}
        total = 0.0
        classified = 0.0
        for row in rows:
            usage_date, resource_id, service_name, amount, currency_code = row[:5]
            amount = float(amount or 0)
            total += amount
            resource = {
                "resourceId": resource_id or "", "name": row[5] or "",
                "subscriptionId": row[6] or "", "subscriptionName": row[7] or "",
                "resourceGroup": row[8] or "", "resourceType": row[9] or "",
                "region": row[10] or "", "serviceName": service_name or "",
                "tags": json.loads(row[11] or "{}") if row[11] else {},
            }
            resolved = effective_tags(
                resource, rules, overrides.get(resource_id or "", []), _utc_now().date()  # type: ignore[attr-defined]
            ) if selected else {}
            match = next(
                (item for key, item in resolved.items() if key.lower() == selected.lower()),
                None,
            )
            bucket_name = str(match.get("value")) if match else "Unclassified"
            if match:
                classified += amount
            if value and bucket_name.lower() != value.lower():
                continue
            bucket = by_value.setdefault(bucket_name, {
                "value": bucket_name, "cost": 0.0, "resourceIds": set(),
                "sources": {},
            })
            bucket["cost"] += amount
            if resource_id:
                bucket["resourceIds"].add(resource_id)
            source = str(match.get("source")) if match else "unclassified"
            bucket["sources"][source] = bucket["sources"].get(source, 0) + amount
            month = usage_date.strftime("%Y-%m") if hasattr(usage_date, "strftime") else str(usage_date)[:7]
            monthly[(month, bucket_name)] = monthly.get((month, bucket_name), 0) + amount
            if resource_id:
                key = (resource_id, bucket_name)
                item = resource_totals.setdefault(key, {
                    "resourceId": resource_id, "name": resource["name"] or resource_id,
                    "subscriptionName": resource["subscriptionName"] or resource["subscriptionId"],
                    "resourceGroup": resource["resourceGroup"],
                    "resourceType": resource["resourceType"], "value": bucket_name,
                    "source": source, "cost": 0.0,
                })
                item["cost"] += amount
        values = []
        for bucket in by_value.values():
            values.append({
                "value": bucket["value"], "cost": round(bucket["cost"], 2),
                "resourceCount": len(bucket["resourceIds"]),
                "percentOfTotal": round(bucket["cost"] / total * 100, 1) if total else None,
                "sources": bucket["sources"],
            })
        values.sort(key=lambda item: item["cost"], reverse=True)
        resources = sorted(resource_totals.values(), key=lambda item: item["cost"], reverse=True)
        for item in resources:
            item["cost"] = round(item["cost"], 2)
        return {
            "dimension": selected, "dimensions": dimensions, "costType": cost_type,
            "currency": selected_currency, "summary": {
                "totalCost": round(total, 2), "classifiedCost": round(classified, 2),
                "classifiedPercent": round(classified / total * 100, 1) if total else None,
                "valueCount": len([item for item in values if item["value"] != "Unclassified"]),
                "resourceCount": len({item[0] for item in resource_totals}),
            },
            "values": values,
            "monthly": [
                {"month": key[0], "value": key[1], "cost": round(amount, 2)}
                for key, amount in sorted(monthly.items())
            ],
            "resources": resources, "resourcesTruncated": False,
            "lineage": {
                "costSource": "daily_cost_history", "tagEvaluation": "current effective tags",
                "precedence": "manual > imported > rule > native",
                "otherCurrencies": excluded_currencies,
                "limitation": (
                    "Historical charges are mapped through current inventory; "
                    "charges without a resolvable resource remain Unclassified. "
                    "Currencies are never combined."
                ),
            },
        }

    def subscription_labels(self) -> dict[str, str]:  # type: ignore[no-redef]
        """Configured friendly labels keyed by lowercase subscription ID."""
        return {
            str(item.get("subscriptionId") or "").lower(): str(
                item.get("label") or ""
            )
            for item in self.integration().get("subscriptions", [])  # type: ignore[attr-defined]
            if item.get("subscriptionId")
        }

    def save_integration(self, payload: dict[str, Any]) -> dict[str, Any]:  # type: ignore[no-redef]
        with self.operational_connect() as db:  # type: ignore[attr-defined]
            db.execute(
                """
                UPDATE azure_integration
                SET name = ?, tenant_id = ?, enabled = ?, auth_mode = ?,
                    subscriptions_json = ?, updated_at = ?
                WHERE id = 'azure'
                """,
                [
                    payload["name"],
                    payload.get("tenantId", ""),
                    payload["enabled"],
                    payload["authMode"],
                    _json_value(payload.get("subscriptions", [])),
                    _utc_now(),
                ],
            )
        return payload

