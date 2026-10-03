/**
 * explore/semanticTypes.ts — typed contracts for explore/* derived from
 * api/semantic_layer.py SEMANTIC_MODELS and src/types.ts:SemanticCatalog.
 *
 * Every model/measures/dimensions literal in explore must come from these
 * unions so the compiler rejects typos and the catalog is the single source
 * of truth. No `any` / `as any` escapes.
 */
import type { SemanticQueryRequest, SemanticQueryResult } from "../types";

// ---------------------------------------------------------------------------
// Registry — mirrors api/semantic_layer.py SEMANTIC_MODELS names exactly.
// ---------------------------------------------------------------------------
export const SEMANTIC_MODELS = [
  "daily_cost",
  "focus_cost",
  "cost_anomalies",
  "governance",
  "workload_optimization",
  "inventory",
  "vm_utilization",
  "commitments",
  "commitment_recommendations",
  "price_sheet",
  "commitment_esr",
  "daily_cost_anomalies",
] as const;

export type SemanticModelName = (typeof SEMANTIC_MODELS)[number];

export type SemanticGrain = NonNullable<SemanticQueryRequest["grain"]>;

// ---------------------------------------------------------------------------
// Per-model allowed measures / dimensions — exhaustively listed from
// api/semantic_layer.py. `as const` so typos are compile errors.
// ---------------------------------------------------------------------------
type ModelMeasures = {
  daily_cost: "total_cost" | "average_daily_cost" | "distinct_resources" | "cost_rows";
  focus_cost:
    | "billed_cost"
    | "effective_cost"
    | "contracted_cost"
    | "list_cost"
    | "billed_vs_effective"
    | "negotiated_discount"
    | "commitment_discount_savings"
    | "total_savings"
    | "priced_effective_cost"
    | "effective_savings_rate"
    | "commitment_covered_cost"
    | "on_demand_cost"
    | "commitment_coverage_percent"
    | "charge_count"
    | "resource_count";
  cost_anomalies:
    | "anomaly_count"
    | "total_absolute_change"
    | "current_total"
    | "baseline_total"
    | "max_percent_change";
  governance: "evaluated" | "compliant" | "non_compliant" | "exempt" | "unknown" | "compliance_percent" | "compliance_rate";
  workload_optimization:
    | "opportunity_count"
    | "monthly_gross"
    | "monthly_risk_adjusted"
    | "resource_count"
    | "average_age_days"
    | "estimated_monthly_savings"
    | "opportunity_type";
  inventory: "resource_count" | "estimated_monthly_cost" | "estimated_monthly_savings" | "average_utilization" | "tagged_percent";
  vm_utilization: "vm_count" | "average_cpu" | "avg_cpu" | "p95_cpu" | "average_p95_cpu" | "max_cpu" | "average_coverage" | "idle_vms";
  commitments:
    | "reservation_count"
    | "total_quantity"
    | "average_utilization_7d"
    | "average_utilization_30d"
    | "utilization_percent"
    | "underused_reservations"
    | "expiring_within_90d";
  commitment_recommendations: "recommendation_count" | "recommended_quantity" | "net_savings" | "cost_without_commitment" | "cost_with_commitment";
  price_sheet: "meter_count" | "discounted_meters" | "average_discount_percent" | "retail_price";
  commitment_esr: "esr_pct" | "esr_percent" | "savings_dollars" | "on_demand_equivalent" | "charged_hours";
  daily_cost_anomalies: "daily_total" | "z_score" | "drift_vs_median" | "sigma_14d";
};

type ModelDimensions = {
  daily_cost: "cost_type" | "subscription_name" | "subscription_id" | "service_name" | "resource_id" | "currency" | "source";
  focus_cost:
    | "charge_category"
    | "pricing_category"
    | "commitment_discount_category"
    | "commitment_discount_type"
    | "service_category"
    | "service_name"
    | "subscription_name"
    | "resource_group"
    | "resource_type"
    | "region_name"
    | "billing_currency";
  cost_anomalies: "severity" | "cost_type" | "scope_type" | "service_name" | "subscription_name" | "subscription_id" | "resource_group";
  governance: "subscription_name" | "assignment_name";
  workload_optimization:
    | "opportunity_type"
    | "valuation_status"
    | "source"
    | "subscription_name"
    | "resource_group"
    | "resource_type"
    | "region"
    | "confidence_label";
  inventory: "resource_type" | "subscription_name" | "resource_group" | "region" | "sku" | "opportunity_kind";
  vm_utilization: "source" | "subscription_name" | "resource_group" | "region" | "sku" | "resource_type";
  commitments: "sku" | "resource_type" | "region" | "term" | "scope_type" | "state";
  commitment_recommendations: "subscription_name" | "scope" | "resource_type" | "sku" | "region" | "term" | "look_back";
  price_sheet: "service_family" | "price_type" | "product" | "unit_of_measure" | "currency" | "sku";
  commitment_esr:
    | "meter_name"
    | "sku_id"
    | "region_name"
    | "subscription_name"
    | "service_name"
    | "rate_source"
    | "image_type"
    | "commitment_discount_name"
    | "pricing_category";
  daily_cost_anomalies: "service_name" | "subscription_name";
};

// ---------------------------------------------------------------------------
// Typed request builder — enforces that model/measures/dimensions agree.
// ---------------------------------------------------------------------------
export type TypedSemanticRequest<M extends SemanticModelName = SemanticModelName> = Omit<SemanticQueryRequest, "model" | "measures" | "dimensions" | "grain"> & {
  model: M;
  measures: M extends keyof ModelMeasures ? ModelMeasures[M][] : string[];
  dimensions?: M extends keyof ModelDimensions ? ModelDimensions[M][] : string[];
  grain?: SemanticGrain | null;
};

export function typedRequest<M extends SemanticModelName>(
  req: TypedSemanticRequest<M>,
): SemanticQueryRequest {
  return req as unknown as SemanticQueryRequest;
}

// Convenience — narrow a string literal through the model union without ever
// writing `as any`.
export function modelLiteral<M extends SemanticModelName>(m: M): M {
  return m;
}

// ---------------------------------------------------------------------------
// Column helpers over SemanticQueryResult.
// ---------------------------------------------------------------------------
export type SemanticColumn = SemanticQueryResult["columns"][number];

export function columnIndexByKind(
  columns: readonly SemanticColumn[],
  kind: SemanticColumn["kind"],
): number {
  return columns.findIndex((c) => c.kind === kind);
}

export function columnIndexByName(
  columns: readonly SemanticColumn[],
  name: string,
): number {
  return columns.findIndex((c) => c.name === name);
}

/** First day of the window, inclusive, as the API's `YYYY-MM-DD`. */
export function startDateForRange(rangeDays: number, today: Date = new Date()): string {
  const anchor = new Date(
    Date.UTC(today.getUTCFullYear(), today.getUTCMonth(), today.getUTCDate()),
  );
  anchor.setUTCDate(anchor.getUTCDate() - (rangeDays - 1));
  return anchor.toISOString().slice(0, 10);
}
