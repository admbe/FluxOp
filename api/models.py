from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic.alias_generators import to_camel


class ApiModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class SubscriptionScope(ApiModel):
    subscription_id: str = Field(min_length=36, max_length=36)
    label: str = Field(default="", max_length=100)

    @field_validator("subscription_id")
    @classmethod
    def normalize_subscription_id(cls, value: str) -> str:
        return value.strip().lower()


class AzureIntegrationUpdate(ApiModel):
    name: str = Field(default="Azure", min_length=1, max_length=80)
    tenant_id: str = Field(default="", max_length=36)
    enabled: bool = True
    auth_mode: Literal["local_powershell", "managed_identity"] = "local_powershell"
    subscriptions: list[SubscriptionScope] = Field(default_factory=list)


class OpportunityLifecycleUpdate(ApiModel):
    opportunity_id: str = Field(min_length=1, max_length=512)
    status: Literal["open", "accepted", "implemented", "dismissed"]
    note: str = Field(default="", max_length=1000)
    resource_id: str = Field(default="", max_length=2048)
    estimated_monthly_savings: float | None = None


class AllocationConfigUpdate(ApiModel):
    cost_center_tags: list[str] = Field(default_factory=list, max_length=8)
    shared_values: list[str] = Field(default_factory=list, max_length=16)
    unit_tag: str = Field(default="", max_length=128)
    unit_label: str = Field(default="", max_length=128)


class JobRunRequest(ApiModel):
    source: str = Field(min_length=1, max_length=40)


class RightsizingBoardCreate(ApiModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=500)


class RightsizingBoardUpdate(ApiModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=500)


class RightsizingBucketUpdate(ApiModel):
    board_id: str = Field(default="", max_length=64)
    region: str = Field(min_length=1, max_length=64)
    sku: str = Field(min_length=1, max_length=100)
    strategy: str = Field(default="", max_length=60)
    ref_quantity: int | None = Field(default=None, ge=0, le=100_000)
    ref_monthly_payg: float | None = None
    ref_monthly_ri_1y: float | None = None
    ref_ri_1y_upfront: float | None = None
    ref_monthly_sp_1y: float | None = None
    ref_monthly_savings: float | None = None
    ref_reservation_check: str = Field(default="", max_length=200)
    note: str = Field(default="", max_length=2000)


class RightsizingMove(ApiModel):
    vm_key: str = Field(min_length=1, max_length=1024)
    vm_name: str = Field(default="", max_length=260)
    subscription_name: str = Field(default="", max_length=120)
    bucket_key: str = Field(min_length=1, max_length=200)
    decision: str | None = Field(default=None, max_length=40)
    note: str | None = Field(default=None, max_length=2000)


class RightsizingAssignmentsUpdate(ApiModel):
    board_id: str = Field(default="", max_length=64)
    moves: list[RightsizingMove] = Field(min_length=1, max_length=500)


class RightsizingPlanImport(ApiModel):
    """The standalone board's decisions file plus its embedded VM seed."""

    board_id: str = Field(default="", max_length=64)
    new_board_name: str = Field(default="", max_length=120)
    dry_run: bool = False
    buckets: dict[str, dict[str, Any]] = Field(default_factory=dict)
    assignments: dict[str, str] = Field(default_factory=dict)
    vm_meta: dict[str, dict[str, Any]] = Field(default_factory=dict)
    log: list[dict[str, Any]] = Field(default_factory=list, max_length=10_000)
    vms: list[dict[str, Any]] = Field(default_factory=list, max_length=10_000)


class AiIntelligenceConfigUpdate(ApiModel):
    provider: Literal["deepseek", "openrouter", "foundry"] = "deepseek"
    fast_model: str = Field(default="", max_length=200)
    deep_model: str = Field(default="", max_length=200)


class BudgetTarget(ApiModel):
    scope_type: Literal["estate", "subscription"]
    scope_id: str = Field(default="", max_length=64)
    monthly_amount: float = Field(gt=0)
    currency: str = Field(default="USD", max_length=8)


class BudgetTargetsUpdate(ApiModel):
    targets: list[BudgetTarget] = Field(default_factory=list, max_length=64)


class CostAnomalyReviewUpdate(ApiModel):
    run_id: str = Field(min_length=1, max_length=80)
    cost_type: Literal["ActualCost", "AmortizedCost"]
    scope_type: Literal["subscription", "service", "resource"]
    scope_id: str = Field(min_length=1, max_length=2048)
    review_status: Literal["new", "investigating", "acknowledged", "resolved"]
    note: str = Field(default="", max_length=2000)


class InventoryQuery(BaseModel):
    search: str = ""
    resource_type: str = ""
    subscription_id: str = ""
    region: str = ""
    opportunity_only: bool = False
    limit: int = Field(default=250, ge=1, le=2000)
    offset: int = Field(default=0, ge=0)


class GovernedReportRequest(ApiModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="forbid",
    )
    report_id: str = Field(min_length=1, max_length=80)
    measures: list[str] = Field(default_factory=list, max_length=50)
    dimensions: list[str] = Field(default_factory=list, max_length=50)
    filters: dict[str, str | int | float | bool | None] = Field(
        default_factory=dict,
    )


class IntelligenceMessage(ApiModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=12000)


class IntelligenceContext(ApiModel):
    page: str = Field(default="overview", max_length=80)
    filters: dict[str, str] = Field(default_factory=dict)
    selected_resource_id: str = Field(default="", max_length=2048)


class IntelligenceChatRequest(ApiModel):
    messages: list[IntelligenceMessage] = Field(min_length=1, max_length=24)
    context: IntelligenceContext = Field(default_factory=IntelligenceContext)
    model_profile: Literal["fast", "benchmark"] = "fast"


class IntelligenceFeedback(ApiModel):
    request_id: str = Field(min_length=1, max_length=80)
    rating: Literal["helpful", "not_helpful"]
    reason: str = Field(default="", max_length=500)


class IntelligenceClientPerformance(ApiModel):
    request_id: str = Field(min_length=1, max_length=80)
    client_round_trip_ms: int = Field(ge=0, le=600000)
    client_render_ms: int = Field(ge=0, le=600000)
    client_end_to_end_ms: int = Field(ge=0, le=600000)


class ExpertExplorerTurn(ApiModel):
    question: str = Field(min_length=1, max_length=2000)
    sql: str = Field(default="", max_length=8000)


class ExpertExplorerRequest(ApiModel):
    question: str = Field(min_length=3, max_length=2000)
    history: list[ExpertExplorerTurn] = Field(
        default_factory=list, max_length=8
    )


class SemanticQueryRequest(ApiModel):
    model: str = Field(min_length=1, max_length=80)
    measures: list[str] = Field(min_length=1, max_length=8)
    dimensions: list[str] = Field(default_factory=list, max_length=3)
    filters: dict[str, list[str]] = Field(default_factory=dict)
    grain: Literal["day", "week", "month"] | None = None
    start: date | None = None
    end: date | None = None
    limit: int = Field(default=1000, ge=1, le=5000)

    @field_validator("filters")
    @classmethod
    def bounded_filters(
        cls, value: dict[str, list[str]]
    ) -> dict[str, list[str]]:
        if len(value) > 6:
            raise ValueError("At most 6 filters per query.")
        for values in value.values():
            if len(values) > 50:
                raise ValueError("At most 50 values per filter.")
        return value


class BudgetGroup(ApiModel):
    id: str = Field(default="", max_length=64)
    name: str = Field(min_length=1, max_length=80)
    annual_amount: float = Field(gt=0)
    currency: str = Field(default="USD", max_length=8)
    subscription_ids: list[str] = Field(default_factory=list, max_length=200)


class BudgetGroupsUpdate(ApiModel):
    groups: list[BudgetGroup] = Field(default_factory=list, max_length=24)


class FiscalOutlookConfigUpdate(ApiModel):
    fy_start_month: int = Field(default=7, ge=1, le=12)
    cost_type: Literal["ActualCost", "AmortizedCost"] = "AmortizedCost"
    growth_percent_monthly: float = Field(default=0.0, ge=-10, le=10)
    include_planned_savings: bool = False
    savings_ramp_months: int = Field(default=3, ge=0, le=12)
    notes: str = Field(default="", max_length=500)


class ClientErrorReport(ApiModel):
    area: str = Field(min_length=1, max_length=80)
    message: str = Field(min_length=1, max_length=2000)
    stack: str = Field(default="", max_length=8000)
    component_stack: str = Field(default="", max_length=8000)
    url: str = Field(default="", max_length=500)
