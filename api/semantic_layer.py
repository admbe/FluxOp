"""The governed semantic layer: one place where Flux metrics are defined.

Historically every consumer -- overview cards, report builders, Ask Flux
tools, and the optional Rill project -- re-derived measures like "amortized
month-to-date" in its own SQL, and the definitions drifted. This module owns
those definitions:

- Each :class:`SemanticModel` is a governed flat view (``semantic_<name>``)
  over snapshot tables, with named dimensions and measures.
- ``create_semantic_views`` builds the views wherever analytics live: the
  mutable database at init/refresh time and every published snapshot
  candidate, so all consumers see identical definitions.
- ``build_semantic_query`` compiles a bounded query request (measures,
  dimensions, filters, time grain and range) into SQL using only registry
  names. Nothing caller-supplied is interpolated into SQL text; filter
  values travel as bind parameters. This is the same no-arbitrary-SQL
  contract the report catalog states.
- The Rill project under ``rill/`` is generated from this registry
  (``scripts/generate_rill_project.py``), so local exploration uses the
  same definitions rather than a hand-maintained mirror.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any


@dataclass(frozen=True)
class Dimension:
    name: str
    column: str
    description: str = ""


@dataclass(frozen=True)
class Measure:
    name: str
    expression: str
    description: str = ""
    format: str = "number"  # number | currency | percent
    higher_is: str = "neutral"  # good | bad | neutral


@dataclass(frozen=True)
class SemanticModel:
    name: str
    display_name: str
    description: str
    sql: str
    grain: str
    requires: tuple[str, ...]
    dimensions: tuple[Dimension, ...]
    measures: tuple[Measure, ...]
    time_column: str | None = None
    # Days at the end of the timeline that are still filling at the source
    # (Cost Management ingestion runs ~24-48h behind). Queries without an
    # explicit end date exclude them so partial days never chart as a
    # cliff; passing an end date overrides deliberately.
    completeness_lag_days: int = 0
    # Filters applied when a query neither filters nor groups by the
    # dimension. Exists for dimensions whose members must never be summed
    # together (ActualCost + AmortizedCost charted as one ~2x total in
    # production); the safe basis is the default and the caller opts out by
    # addressing the dimension explicitly.
    default_filters: tuple[tuple[str, tuple[str, ...]], ...] = ()

    @property
    def view_name(self) -> str:
        return f"semantic_{self.name}"

    def dimension(self, name: str) -> Dimension | None:
        return next((d for d in self.dimensions if d.name == name), None)

    def measure(self, name: str) -> Measure | None:
        return next((m for m in self.measures if m.name == name), None)


SEMANTIC_MODELS: tuple[SemanticModel, ...] = (
    SemanticModel(
        name="daily_cost",
        display_name="Daily cost",
        description=(
            "Governed daily Cost Management history by resource and service. "
            "Actual and amortized lines are separate cost types and must not "
            "be summed together."
        ),
        sql=(
            # Cost lines carry only the subscription GUID. Resolve the
            # friendly name the same way the rest of the application does,
            # falling back to the id so a subscription with no inventory
            # still reads as something rather than disappearing.
            "WITH names AS (\n"
            "    SELECT subscription_id,\n"
            "           any_value(NULLIF(subscription_name, '')) AS name\n"
            "    FROM resources_current GROUP BY subscription_id\n"
            ")\n"
            "SELECT cost.usage_date, cost.cost_type, cost.subscription_id,\n"
            "       COALESCE(names.name, cost.subscription_id)\n"
            "           AS subscription_name,\n"
            "       cost.resource_id, cost.service_name, cost.amount,\n"
            "       cost.currency, cost.source\n"
            "FROM daily_cost_history AS cost\n"
            "LEFT JOIN names USING (subscription_id)"
        ),
        grain="One row per resource, service, day, and cost type",
        requires=("daily_cost_history", "resources_current"),
        time_column="usage_date",
        completeness_lag_days=2,
        default_filters=(("cost_type", ("ActualCost",)),),
        dimensions=(
            Dimension("cost_type", "cost_type", "ActualCost or AmortizedCost"),
            Dimension(
                "subscription_name", "subscription_name",
                "Friendly subscription name; falls back to the id",
            ),
            Dimension("subscription_id", "subscription_id"),
            Dimension("service_name", "service_name"),
            Dimension("resource_id", "resource_id"),
            Dimension("currency", "currency"),
            Dimension("source", "source", "Collector that produced the line"),
        ),
        measures=(
            Measure(
                "total_cost",
                "SUM(amount)",
                "Cost within one explicit cost type and currency.",
                format="currency",
                higher_is="bad",
            ),
            Measure(
                "average_daily_cost",
                "SUM(amount) / NULLIF(COUNT(DISTINCT usage_date), 0)",
                "Total cost divided by the number of observed days.",
                format="currency",
                higher_is="bad",
            ),
            Measure(
                "distinct_resources",
                "COUNT(DISTINCT NULLIF(resource_id, ''))",
                "Resources with at least one cost line.",
            ),
            Measure("cost_rows", "COUNT(*)", "Underlying cost lines."),
        ),
    ),
    SemanticModel(
        name="focus_cost",
        display_name="FOCUS cost",
        description=(
            "FOCUS v1.0 charge lines from the Cost Management export: billed, "
            "effective, contracted, and list cost with commitment context."
        ),
        sql=(
            "SELECT charge_period_start, charge_period_end, billed_cost,\n"
            "       effective_cost, contracted_cost, list_cost,\n"
            "       billing_currency, charge_category, charge_frequency,\n"
            "       pricing_category, commitment_discount_category,\n"
            "       commitment_discount_type, service_category, service_name,\n"
            "       resource_id, resource_name, resource_type, resource_group,\n"
            "       subscription_id, subscription_name, provider_name,\n"
            "       region_name, sku_id, meter_category, meter_name\n"
            "FROM focus_cost_current"
        ),
        grain="One FOCUS charge line",
        requires=("focus_cost_current",),
        time_column="charge_period_start",
        completeness_lag_days=2,
        dimensions=(
            Dimension("charge_category", "charge_category"),
            Dimension("pricing_category", "pricing_category"),
            Dimension(
                "commitment_discount_category", "commitment_discount_category"
            ),
            Dimension("commitment_discount_type", "commitment_discount_type"),
            Dimension("service_category", "service_category"),
            Dimension("service_name", "service_name"),
            Dimension("subscription_name", "subscription_name"),
            Dimension("resource_group", "resource_group"),
            Dimension("resource_type", "resource_type"),
            Dimension("region_name", "region_name"),
            Dimension("billing_currency", "billing_currency"),
        ),
        measures=(
            Measure(
                "billed_cost",
                "SUM(billed_cost)",
                "Invoice-basis cost.",
                format="currency",
                higher_is="bad",
            ),
            Measure(
                "effective_cost",
                "SUM(effective_cost)",
                "Amortized-basis cost with commitments spread.",
                format="currency",
                higher_is="bad",
            ),
            Measure(
                "contracted_cost", "SUM(contracted_cost)", format="currency"
            ),
            Measure("list_cost", "SUM(list_cost)", format="currency"),
            Measure(
                "billed_vs_effective",
                "SUM(billed_cost - effective_cost)",
                "Positive when invoices front-load commitment purchases.",
                format="currency",
            ),
            Measure(
                "negotiated_discount",
                "SUM(list_cost - contracted_cost)",
                "Value of negotiated rates against list price. Understated "
                "where the export omits list price; the price sheet feed "
                "closes that gap.",
                format="currency",
                higher_is="good",
            ),
            Measure(
                "commitment_discount_savings",
                "SUM(contracted_cost - effective_cost)",
                "Savings from reservations and savings plans against the "
                "negotiated rate.",
                format="currency",
                higher_is="good",
            ),
            Measure(
                "total_savings",
                "SUM(list_cost - effective_cost)",
                "Negotiated plus commitment savings against list price.",
                format="currency",
                higher_is="good",
            ),
            Measure(
                "effective_savings_rate",
                "100.0 * SUM(list_cost - effective_cost)"
                " / NULLIF(SUM(list_cost), 0)",
                "The FinOps Framework ESR: share of list price you do not "
                "pay. Understated where list price is missing at the source.",
                format="percent",
                higher_is="good",
            ),
            Measure(
                "commitment_covered_cost",
                "SUM(CASE WHEN coalesce(commitment_discount_category, '')"
                " <> '' THEN effective_cost ELSE 0 END)",
                "Effective cost carrying a reservation or savings plan.",
                format="currency",
                higher_is="good",
            ),
            Measure(
                "on_demand_cost",
                "SUM(CASE WHEN coalesce(commitment_discount_category, '')"
                " = '' THEN effective_cost ELSE 0 END)",
                "Effective cost with no commitment discount applied.",
                format="currency",
            ),
            Measure(
                "commitment_coverage_percent",
                "100.0 * SUM(CASE WHEN"
                " coalesce(commitment_discount_category, '') <> ''"
                " THEN effective_cost ELSE 0 END)"
                " / NULLIF(SUM(effective_cost), 0)",
                "Share of effective cost covered by commitments.",
                format="percent",
                higher_is="good",
            ),
            Measure("charge_count", "COUNT(*)"),
            Measure(
                "resource_count", "COUNT(DISTINCT NULLIF(resource_id, ''))"
            ),
        ),
    ),
    SemanticModel(
        name="cost_anomalies",
        display_name="Cost anomalies",
        description=(
            "Daily anomaly evaluations against the governed baseline, by "
            "scope and severity."
        ),
        sql=(
            "WITH names AS (\n"
            "    SELECT subscription_id,\n"
            "           any_value(NULLIF(subscription_name, '')) AS name\n"
            "    FROM resources_current GROUP BY subscription_id\n"
            ")\n"
            "SELECT anomaly.evaluation_date, anomaly.evaluated_at,\n"
            "       anomaly.cost_type, anomaly.scope_type, anomaly.scope_id,\n"
            "       anomaly.subscription_id,\n"
            "       COALESCE(names.name, anomaly.subscription_id)\n"
            "           AS subscription_name,\n"
            "       anomaly.resource_id, anomaly.resource_name,\n"
            "       anomaly.resource_type, anomaly.resource_group,\n"
            "       anomaly.service_name, anomaly.current_amount,\n"
            "       anomaly.baseline_median, anomaly.absolute_change,\n"
            "       anomaly.percent_change, anomaly.severity,\n"
            "       anomaly.currency\n"
            "FROM cost_anomalies_current AS anomaly\n"
            "LEFT JOIN names USING (subscription_id)"
        ),
        grain="One anomaly evaluation",
        requires=("cost_anomalies_current", "resources_current"),
        time_column="evaluation_date",
        dimensions=(
            Dimension("severity", "severity"),
            Dimension("cost_type", "cost_type"),
            Dimension("scope_type", "scope_type"),
            Dimension("service_name", "service_name"),
            Dimension(
                "subscription_name", "subscription_name",
                "Friendly subscription name; falls back to the id",
            ),
            Dimension("subscription_id", "subscription_id"),
            Dimension("resource_group", "resource_group"),
        ),
        measures=(
            Measure("anomaly_count", "COUNT(*)", higher_is="bad"),
            Measure(
                "total_absolute_change",
                "SUM(absolute_change)",
                "Sum of spend movement versus baseline.",
                format="currency",
                higher_is="bad",
            ),
            Measure(
                "current_total",
                "SUM(current_amount)",
                format="currency",
            ),
            Measure(
                "baseline_total",
                "SUM(baseline_median)",
                format="currency",
            ),
            Measure(
                "max_percent_change",
                "MAX(percent_change)",
                format="percent",
                higher_is="bad",
            ),
        ),
    ),
    SemanticModel(
        name="governance",
        display_name="Policy governance",
        description="Azure Policy compliance posture by assignment.",
        sql=(
            "SELECT observed_at, subscription_id, subscription_name,\n"
            "       assignment_id, assignment_name, evaluated_count,\n"
            "       compliant_count, non_compliant_count, exempt_count,\n"
            "       unknown_count, resource_count, definition_count\n"
            "FROM policy_posture_current"
        ),
        grain="One policy assignment posture row",
        requires=("policy_posture_current",),
        time_column=None,
        dimensions=(
            Dimension("subscription_name", "subscription_name"),
            Dimension("assignment_name", "assignment_name"),
        ),
        measures=(
            Measure("evaluated", "SUM(evaluated_count)"),
            Measure("compliant", "SUM(compliant_count)", higher_is="good"),
            Measure(
                "non_compliant", "SUM(non_compliant_count)", higher_is="bad"
            ),
            Measure("exempt", "SUM(exempt_count)"),
            Measure("unknown", "SUM(unknown_count)"),
            Measure(
                "compliance_percent",
                "100.0 * SUM(compliant_count) / "
                "NULLIF(SUM(evaluated_count), 0)",
                "Compliant share of evaluated resources.",
                format="percent",
                higher_is="good",
            ),
        ),
    ),
    SemanticModel(
        name="workload_optimization",
        display_name="Workload optimization",
        description=(
            "Governed opportunity valuations joined with confidence and "
            "current resource identity."
        ),
        sql=(
            "SELECT valuation.computed_at, valuation.resource_id,\n"
            "       resource.name AS resource_name, resource.resource_type,\n"
            "       resource.subscription_id, resource.subscription_name,\n"
            "       resource.resource_group, resource.region,\n"
            "       valuation.opportunity_type, valuation.source,\n"
            "       valuation.valuation_status, valuation.monthly_gross,\n"
            "       valuation.monthly_risk_adjusted, valuation.currency,\n"
            "       confidence.confidence_label,\n"
            "       date_diff('day', confidence.first_seen,\n"
            "                 confidence.last_seen) AS age_days\n"
            "FROM opportunity_valuation_current AS valuation\n"
            "LEFT JOIN opportunity_confidence_current AS confidence\n"
            "  ON confidence.resource_id = valuation.resource_id\n"
            " AND confidence.opportunity_type = valuation.opportunity_type\n"
            "LEFT JOIN resources_current AS resource\n"
            "  ON resource.resource_id = valuation.resource_id"
        ),
        grain="One valued opportunity per resource and type",
        requires=(
            "opportunity_valuation_current",
            "opportunity_confidence_current",
            "resources_current",
        ),
        time_column=None,
        dimensions=(
            Dimension("opportunity_type", "opportunity_type"),
            Dimension("valuation_status", "valuation_status"),
            Dimension("source", "source"),
            Dimension("subscription_name", "subscription_name"),
            Dimension("resource_group", "resource_group"),
            Dimension("resource_type", "resource_type"),
            Dimension("region", "region"),
            Dimension("confidence_label", "confidence_label"),
        ),
        measures=(
            Measure("opportunity_count", "COUNT(*)"),
            Measure(
                "monthly_gross",
                "SUM(monthly_gross)",
                "Gross monthly savings if every opportunity were actioned.",
                format="currency",
                higher_is="good",
            ),
            Measure(
                "monthly_risk_adjusted",
                "SUM(monthly_risk_adjusted)",
                "Savings weighted by confidence and risk.",
                format="currency",
                higher_is="good",
            ),
            Measure(
                "resource_count", "COUNT(DISTINCT NULLIF(resource_id, ''))"
            ),
            Measure(
                "average_age_days",
                "AVG(age_days)",
                "How long opportunities have been open.",
                higher_is="bad",
            ),
        ),
    ),
    SemanticModel(
        name="inventory",
        display_name="Inventory",
        description=(
            "Current Azure estate: every governed resource with cost, "
            "utilization, and tagging posture."
        ),
        sql=(
            "SELECT resource_id, name, resource_type, subscription_id,\n"
            "       subscription_name, resource_group, region, sku,\n"
            "       cost_source, opportunity_kind, tags_json,\n"
            "       estimated_monthly_cost, estimated_monthly_savings,\n"
            "       utilization_percent\n"
            "FROM resources_current"
        ),
        grain="One current resource",
        requires=("resources_current",),
        time_column=None,
        dimensions=(
            Dimension("resource_type", "resource_type"),
            Dimension("subscription_name", "subscription_name"),
            Dimension("resource_group", "resource_group"),
            Dimension("region", "region"),
            Dimension("sku", "sku"),
            Dimension("opportunity_kind", "opportunity_kind"),
        ),
        measures=(
            Measure("resource_count", "COUNT(*)"),
            Measure(
                "estimated_monthly_cost",
                "SUM(coalesce(estimated_monthly_cost, 0))",
                "Directional monthly cost from the enrichment pipeline.",
                format="currency",
                higher_is="bad",
            ),
            Measure(
                "estimated_monthly_savings",
                "SUM(coalesce(estimated_monthly_savings, 0))",
                "Directional savings attached to detected opportunities.",
                format="currency",
                higher_is="good",
            ),
            Measure(
                "average_utilization",
                "AVG(utilization_percent)",
                "Mean utilization across resources with telemetry.",
                format="percent",
            ),
            Measure(
                "tagged_percent",
                "100.0 * COUNT(CASE WHEN"
                " coalesce(CAST(tags_json AS VARCHAR), '') NOT IN"
                " ('', '{}', 'null') THEN 1 END) / NULLIF(COUNT(*), 0)",
                "Share of resources carrying at least one tag.",
                format="percent",
                higher_is="good",
            ),
        ),
    ),
    SemanticModel(
        name="vm_utilization",
        display_name="VM utilization",
        description=(
            "Governed CPU telemetry summaries joined with resource "
            "identity: the evidence base for right-sizing."
        ),
        sql=(
            "SELECT summary.resource_id, summary.source,\n"
            "       summary.aggregation_method, summary.coverage_percent,\n"
            "       summary.average, summary.p95, summary.maximum,\n"
            "       resource.name AS resource_name,\n"
            "       resource.subscription_name, resource.resource_group,\n"
            "       resource.region, resource.sku\n"
            "FROM telemetry_metric_summaries_current AS summary\n"
            "LEFT JOIN resources_current AS resource\n"
            "  ON lower(resource.resource_id) = lower(summary.resource_id)\n"
            "WHERE lower(summary.metric) = 'percentage cpu'"
        ),
        grain="One CPU summary per resource and telemetry source",
        requires=(
            "telemetry_metric_summaries_current",
            "resources_current",
        ),
        time_column=None,
        dimensions=(
            Dimension("source", "source", "azure_monitor or logicmonitor"),
            Dimension("subscription_name", "subscription_name"),
            Dimension("resource_group", "resource_group"),
            Dimension("region", "region"),
            Dimension("sku", "sku"),
        ),
        measures=(
            Measure(
                "vm_count", "COUNT(DISTINCT lower(resource_id))",
                "VMs with a CPU summary from this source.",
            ),
            Measure(
                "average_cpu",
                "AVG(average)",
                "Mean of per-VM average CPU.",
                format="percent",
            ),
            Measure(
                "average_p95_cpu",
                "AVG(p95)",
                "Mean of per-VM p95 CPU -- the right-sizing signal.",
                format="percent",
            ),
            Measure("max_cpu", "MAX(maximum)", format="percent"),
            Measure(
                "average_coverage",
                "AVG(coverage_percent)",
                "How complete the observation window is.",
                format="percent",
                higher_is="good",
            ),
            Measure(
                "idle_vms",
                "COUNT(DISTINCT CASE WHEN p95 < 10 THEN"
                " lower(resource_id) END)",
                "VMs whose p95 CPU stays under 10%.",
                higher_is="bad",
            ),
        ),
    ),
    SemanticModel(
        name="commitments",
        display_name="Commitments",
        description=(
            "Purchased reservations with Azure's own 1/7/30-day "
            "utilization: whether what the plan bought is being used."
        ),
        sql=(
            "SELECT reservation_id, order_id, display_name, sku,"
            " resource_type, region, quantity, term, scope_type, state,"
            " expiry_date, utilization_1d, utilization_7d, utilization_30d"
            " FROM reservation_inventory_current"
        ),
        grain="One purchased reservation",
        requires=("reservation_inventory_current",),
        time_column=None,
        dimensions=(
            Dimension("sku", "sku"),
            Dimension("resource_type", "resource_type"),
            Dimension("region", "region"),
            Dimension("term", "term", "P1Y or P3Y"),
            Dimension("scope_type", "scope_type"),
            Dimension("state", "state"),
        ),
        measures=(
            Measure("reservation_count", "COUNT(*)"),
            Measure("total_quantity", "SUM(quantity)"),
            Measure(
                "average_utilization_7d",
                "AVG(utilization_7d)",
                "Azure-reported 7-day utilization of purchased capacity.",
                format="percent",
                higher_is="good",
            ),
            Measure(
                "average_utilization_30d",
                "AVG(utilization_30d)",
                format="percent",
                higher_is="good",
            ),
            Measure(
                "underused_reservations",
                "COUNT(CASE WHEN utilization_30d IS NOT NULL"
                " AND utilization_30d < 80 THEN 1 END)",
                "Reservations under 80% utilization over 30 days -- money "
                "already spent that workloads are not consuming.",
                higher_is="bad",
            ),
            Measure(
                "expiring_within_90d",
                "COUNT(CASE WHEN expiry_date IS NOT NULL AND expiry_date"
                " <= current_date + INTERVAL 90 DAY THEN 1 END)",
                "Reservations that need a renew-or-release decision soon.",
                higher_is="neutral",
            ),
        ),
    ),
    SemanticModel(
        name="commitment_recommendations",
        display_name="Commitment recommendations",
        description=(
            "Azure's reservation purchase recommendations per "
            "subscription, by SKU, term, and look-back window."
        ),
        sql=(
            "SELECT subscription_id, subscription_name, scope,"
            " resource_type, sku, region, term, look_back,"
            " recommended_quantity, cost_without_commitment,"
            " cost_with_commitment, net_savings"
            " FROM reservation_recommendations_current"
        ),
        grain="One recommendation per subscription, SKU, and term",
        requires=("reservation_recommendations_current",),
        time_column=None,
        dimensions=(
            Dimension("subscription_name", "subscription_name"),
            Dimension("scope", "scope", "Single or Shared"),
            Dimension("resource_type", "resource_type"),
            Dimension("sku", "sku"),
            Dimension("region", "region"),
            Dimension("term", "term"),
            Dimension("look_back", "look_back"),
        ),
        measures=(
            Measure("recommendation_count", "COUNT(*)"),
            Measure(
                "recommended_quantity", "SUM(recommended_quantity)"
            ),
            Measure(
                "net_savings",
                "SUM(net_savings)",
                "Azure's estimated savings over the recommendation term.",
                format="currency",
                higher_is="good",
            ),
            Measure(
                "cost_without_commitment",
                "SUM(cost_without_commitment)",
                "Projected on-demand cost over the term if nothing is "
                "purchased.",
                format="currency",
            ),
            Measure(
                "cost_with_commitment",
                "SUM(cost_with_commitment)",
                format="currency",
            ),
        ),
    ),
    SemanticModel(
        name="price_sheet",
        display_name="Price sheet",
        description=(
            "Negotiated unit prices from the Cost Management price sheet "
            "export: the audit-grade basis for savings versus market "
            "rates."
        ),
        sql=(
            "SELECT meter_id, meter_name, service_family, product, sku_id,"
            " unit_of_measure, price_type, unit_price, base_price,"
            " market_price, currency"
            " FROM price_sheet_current"
        ),
        grain="One priced meter",
        requires=("price_sheet_current",),
        time_column=None,
        dimensions=(
            Dimension("service_family", "service_family"),
            Dimension("price_type", "price_type"),
            Dimension("product", "product"),
            Dimension("unit_of_measure", "unit_of_measure"),
            Dimension("currency", "currency"),
        ),
        measures=(
            Measure(
                "meter_count", "COUNT(DISTINCT NULLIF(meter_id, ''))"
            ),
            Measure(
                "discounted_meters",
                "COUNT(CASE WHEN unit_price IS NOT NULL AND market_price"
                " IS NOT NULL AND unit_price < market_price THEN 1 END)",
                "Meters priced below market rate by the agreement.",
                higher_is="good",
            ),
            Measure(
                "average_discount_percent",
                "AVG(CASE WHEN market_price > 0 AND unit_price IS NOT NULL"
                " THEN 100.0 * (market_price - unit_price) / market_price"
                " END)",
                "Unweighted mean discount versus market price across "
                "priced meters -- directional, not spend-weighted.",
                format="percent",
                higher_is="good",
            ),
        ),
    ),
)


def find_model(name: str) -> SemanticModel | None:
    return next((m for m in SEMANTIC_MODELS if m.name == name), None)


def create_semantic_views(connection: Any) -> list[str]:
    """Create every semantic view whose source tables exist.

    Idempotent (CREATE OR REPLACE) and safe on older databases: a model
    whose sources are missing is skipped rather than failing the caller,
    because snapshot files predating a source table must still publish.
    """
    available = {
        str(row[0])
        for row in connection.execute(
            "SELECT table_name FROM information_schema.tables"
        ).fetchall()
    }
    created: list[str] = []
    for model in SEMANTIC_MODELS:
        if any(required not in available for required in model.requires):
            continue
        connection.execute(
            f"CREATE OR REPLACE VIEW {model.view_name} AS\n{model.sql}"
        )
        created.append(model.view_name)
    return created


def semantic_catalog() -> dict[str, Any]:
    """The registry as governed metadata for the UI, API, and AI."""
    return {
        "contract": (
            "Queries may reference only the models, dimensions, and "
            "measures listed here; arbitrary SQL is not accepted."
        ),
        "models": [
            {
                "name": model.name,
                "displayName": model.display_name,
                "description": model.description,
                "grain": model.grain,
                "timeColumn": model.time_column,
                "completenessLagDays": model.completeness_lag_days,
                "defaultFilters": {
                    name: list(values)
                    for name, values in model.default_filters
                },
                "dimensions": [
                    {"name": d.name, "description": d.description}
                    for d in model.dimensions
                ],
                "measures": [
                    {
                        "name": m.name,
                        "description": m.description,
                        "format": m.format,
                        "higherIs": m.higher_is,
                    }
                    for m in model.measures
                ],
            }
            for model in SEMANTIC_MODELS
        ],
    }


class SemanticQueryError(ValueError):
    """A request referenced names outside the registry or invalid bounds."""


_GRAINS = {"day": "day", "week": "week", "month": "month"}

MAX_MEASURES = 8
MAX_DIMENSIONS = 3
MAX_FILTER_VALUES = 50
MAX_LIMIT = 5000


@dataclass(frozen=True)
class SemanticQuery:
    model: str
    measures: tuple[str, ...]
    dimensions: tuple[str, ...] = ()
    filters: dict[str, tuple[str, ...]] = field(default_factory=dict)
    grain: str | None = None
    start: date | None = None
    end: date | None = None
    limit: int = 1000


def build_semantic_query(
    query: SemanticQuery,
) -> tuple[str, list[Any], list[dict[str, str]], dict[str, list[str]]]:
    """Compile a request to (sql, parameters, column descriptors).

    Every identifier in the emitted SQL comes from the registry; caller
    input only selects registry entries or travels as bind parameters.
    """
    model = find_model(query.model)
    if model is None:
        raise SemanticQueryError(f"Unknown semantic model: {query.model!r}")
    if not query.measures:
        raise SemanticQueryError("At least one measure is required.")
    if len(query.measures) > MAX_MEASURES:
        raise SemanticQueryError(f"At most {MAX_MEASURES} measures per query.")
    if len(query.dimensions) > MAX_DIMENSIONS:
        raise SemanticQueryError(
            f"At most {MAX_DIMENSIONS} dimensions per query."
        )
    if not 1 <= query.limit <= MAX_LIMIT:
        raise SemanticQueryError(f"limit must be between 1 and {MAX_LIMIT}.")

    columns: list[dict[str, str]] = []
    select: list[str] = []
    group: list[str] = []
    parameters: list[Any] = []

    if query.grain is not None:
        if model.time_column is None:
            raise SemanticQueryError(
                f"Model {model.name!r} has no time column; omit grain."
            )
        if query.grain not in _GRAINS:
            raise SemanticQueryError(
                f"grain must be one of {sorted(_GRAINS)}."
            )
        select.append(
            f"date_trunc('{_GRAINS[query.grain]}', {model.time_column})"
            " AS period"
        )
        group.append("1")
        columns.append({"name": "period", "kind": "time", "format": "date"})

    for name in query.dimensions:
        dimension = model.dimension(name)
        if dimension is None:
            raise SemanticQueryError(
                f"Unknown dimension {name!r} on model {model.name!r}."
            )
        select.append(f"{dimension.column} AS {dimension.name}")
        group.append(str(len(select)))
        columns.append(
            {"name": dimension.name, "kind": "dimension", "format": "text"}
        )

    for name in query.measures:
        measure = model.measure(name)
        if measure is None:
            raise SemanticQueryError(
                f"Unknown measure {name!r} on model {model.name!r}."
            )
        select.append(f"{measure.expression} AS {measure.name}")
        columns.append(
            {"name": measure.name, "kind": "measure", "format": measure.format}
        )

    where: list[str] = []
    if query.start is not None or query.end is not None:
        if model.time_column is None:
            raise SemanticQueryError(
                f"Model {model.name!r} has no time column; omit start/end."
            )
        if query.start is not None:
            where.append(f"CAST({model.time_column} AS DATE) >= ?")
            parameters.append(query.start)
        if query.end is not None:
            where.append(f"CAST({model.time_column} AS DATE) <= ?")
            parameters.append(query.end)
    if (
        query.end is None
        and model.time_column is not None
        and model.completeness_lag_days > 0
    ):
        # The source is still filling its most recent days; excluding them
        # by default keeps partial data from charting as a collapse. An
        # explicit end date overrides deliberately.
        where.append(
            f"CAST({model.time_column} AS DATE) <= current_date"
            f" - INTERVAL {int(model.completeness_lag_days)} DAY"
        )
    for name, values in query.filters.items():
        dimension = model.dimension(name)
        if dimension is None:
            raise SemanticQueryError(
                f"Unknown filter dimension {name!r} on model {model.name!r}."
            )
        if not values:
            continue
        if len(values) > MAX_FILTER_VALUES:
            raise SemanticQueryError(
                f"At most {MAX_FILTER_VALUES} values per filter."
            )
        placeholders = ", ".join("?" for _ in values)
        where.append(f"{dimension.column} IN ({placeholders})")
        parameters.extend(values)

    applied_defaults: dict[str, list[str]] = {}
    for name, values in model.default_filters:
        # A caller who filters or groups by the dimension has addressed it
        # deliberately; only an untouched dimension gets the safe default.
        if name in query.filters or name in query.dimensions:
            continue
        dimension = model.dimension(name)
        if dimension is None or not values:
            continue
        placeholders = ", ".join("?" for _ in values)
        where.append(f"{dimension.column} IN ({placeholders})")
        parameters.extend(values)
        applied_defaults[name] = list(values)

    sql = f"SELECT {', '.join(select)}\nFROM {model.view_name}"
    if where:
        sql += "\nWHERE " + " AND ".join(where)
    if group:
        sql += "\nGROUP BY " + ", ".join(group)
    order: list[str] = []
    if query.grain is not None:
        order.append("period ASC")
    if query.dimensions or query.grain is None:
        order.append(f"{query.measures[0]} DESC")
    sql += "\nORDER BY " + ", ".join(order)
    sql += f"\nLIMIT {int(query.limit)}"
    return sql, parameters, columns, applied_defaults
