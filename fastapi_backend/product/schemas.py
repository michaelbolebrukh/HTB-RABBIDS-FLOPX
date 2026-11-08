"""FLOPX wire and file schemas.

Every model takes and emits camelCase (architecture §2/§4 fix the wire shape
as `observedAt`, `generatedAt`, ...) while Python stays snake_case: one base
class sets the alias generator, accepts both spellings on input, and
serialises by alias. `from_attributes=True` lets the dataclasses in
product.flopx.domain serialise straight through a response model.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator, model_validator
from pydantic.alias_generators import to_camel

from product.validators import is_series_id, normalise_ticker


class FlopxModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        serialize_by_alias=True,
        from_attributes=True,
        extra="ignore",
    )


# ============================================================================
# PROVENANCE AND FRESHNESS (§2, §4)
# ============================================================================

class Provenance(FlopxModel):
    kind: Literal["live", "archived", "sample", "derived"]
    source: str
    observed_at: datetime
    source_url: Optional[str] = None
    fetched_at: Optional[datetime] = None
    licence: Optional[str] = None
    note: Optional[str] = None
    input_kinds: list[str] = Field(default_factory=list)


class Unavailable(FlopxModel):
    """An adapter that could not answer: named source and a reason token, never sample in its place."""
    source: str
    reason: str


class SourceFreshness(FlopxModel):
    """One live or archived source a response used, with its newest observation time."""
    source: str
    kind: Literal["live", "archived"]
    observed_at: datetime
    licence: Optional[str] = None


class Freshness(FlopxModel):
    generated_at: datetime
    oldest_observed_at: Optional[datetime] = None
    sources: list[str] = Field(default_factory=list)
    source_details: list[SourceFreshness] = Field(default_factory=list)
    unavailable: list[Unavailable] = Field(default_factory=list)


# ============================================================================
# DOMAIN ENTITIES (§3), also the validators for the sample files
# ============================================================================

class GpuModel(FlopxModel):
    id: str
    vendor: str
    family: str
    memory_gb: int = Field(gt=0)
    interconnect: str
    fp16_tflops: float = Field(gt=0)
    release_year: int = Field(ge=2015, le=2030)


class Provider(FlopxModel):
    id: str
    name: str
    kind: Literal["marketplace", "neocloud", "hyperscaler"]
    website: Optional[str] = None


class Region(FlopxModel):
    id: str
    name: str
    country_code: str = Field(min_length=2, max_length=2)
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    grid_zone: Optional[str] = None


class GpuOffer(FlopxModel):
    id: str
    provider_id: str
    region_id: str
    gpu_model_id: str
    gpu_count: int = Field(gt=0)
    price_per_gpu_hour_usd: float = Field(gt=0)
    availability: Literal["available", "limited", "unavailable"]
    interruptible: bool
    contract: Literal["on-demand", "reserved", "spot"]
    provenance: Provenance
    reliability_score: Optional[float] = Field(default=None, ge=0, le=1)
    bandwidth_gbps: Optional[float] = Field(default=None, gt=0)
    min_duration_hours: Optional[int] = Field(default=None, ge=0)


class PriceObservation(FlopxModel):
    series_id: str
    t: datetime
    value: float
    unit: str
    provenance: Provenance


class SeriesFile(FlopxModel):
    """The compact on-disk shape: points as [iso, value] pairs."""
    id: str
    unit: str
    points: list[tuple[datetime, float]]

    @field_validator("id")
    @classmethod
    def _series_id(cls, value: str) -> str:
        if not is_series_id(value):
            raise ValueError(f"malformed series id {value!r}")
        return value


class SeriesBreak(FlopxModel):
    """Where a series stops being comparable with its earlier points (R1-4): for an index, the basket of
    sources behind the median changed. Momentum, change and forecast reads never span it."""
    t: datetime
    reason: Literal["basket-changed"]
    before: list[str] = Field(default_factory=list)
    after: list[str] = Field(default_factory=list)


class FcpiCoverage(FlopxModel):
    providers: int
    regions: int
    offer_count: int


class FcpiIndex(FlopxModel):
    id: str
    gpu_model_id: str
    methodology: str
    coverage: FcpiCoverage
    normalisation: list[str]
    last_updated: datetime
    values: list[PriceObservation]
    limitations: list[str]
    provenance: Provenance
    current: Optional[float] = None
    # The sources behind `current` (empty for a sample index) and each basket change inside `values`.
    basket: list[str] = Field(default_factory=list)
    breaks: list[SeriesBreak] = Field(default_factory=list)
    # Set on the single-index read (`/gpu/index/{id}`); the list read carries one block for all.
    freshness: Optional[Freshness] = None


class FacilityTimeline(FlopxModel):
    announced: Optional[str] = None
    construction_start: Optional[str] = None
    expected_online: Optional[str] = None


class SourceLink(FlopxModel):
    url: str
    date: str
    label: Optional[str] = None


class Facility(FlopxModel):
    id: str
    name: str
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    region_id: str
    status: Literal["operating", "under-construction", "announced", "planned", "reported"]
    provenance: Provenance
    operator: Optional[str] = None
    owner_company_ids: list[str] = Field(default_factory=list)
    capacity_mw: Optional[float] = Field(default=None, ge=0)
    gpu_count_estimate: Optional[int] = Field(default=None, ge=0)
    timeline: FacilityTimeline = Field(default_factory=FacilityTimeline)
    sources: list[SourceLink] = Field(default_factory=list)


class ComputeExposure(FlopxModel):
    text: str
    tags: list[str] = Field(default_factory=list)


class Fundamental(FlopxModel):
    ticker: str
    concept: str
    value: float
    unit: str
    period_end: str
    filed_at: str
    provenance: Provenance


class Company(FlopxModel):
    ticker: str
    name: str
    exchange: str
    segments: list[str]
    compute_exposure: ComputeExposure
    related_facility_ids: list[str] = Field(default_factory=list)
    peers: list[str] = Field(default_factory=list)
    provenance: Provenance
    cik: Optional[str] = None
    fundamentals: list[Fundamental] = Field(default_factory=list)
    sample_last_close_usd: Optional[float] = Field(default=None, gt=0)

    @field_validator("ticker")
    @classmethod
    def _ticker(cls, value: str) -> str:
        return normalise_ticker(value)


class EquityBar(FlopxModel):
    ticker: str
    t: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    provenance: Provenance


class OutcomeQuote(FlopxModel):
    label: str
    price: float = Field(ge=0, le=1)
    implied_probability: float = Field(ge=0, le=1)


class LinkedEntities(FlopxModel):
    tickers: list[str] = Field(default_factory=list)
    region_ids: list[str] = Field(default_factory=list)
    facility_ids: list[str] = Field(default_factory=list)
    gpu_model_ids: list[str] = Field(default_factory=list)


class PredictionMarketFile(FlopxModel):
    id: str
    venue: str
    question: str
    outcomes: list[OutcomeQuote] = Field(min_length=1)
    last_updated: datetime
    provenance: Provenance
    resolution_rules: Optional[str] = None
    liquidity: Optional[float] = None
    volume_24h: Optional[float] = None
    history: list[tuple[datetime, float]] = Field(default_factory=list)
    linked_entities: LinkedEntities = Field(default_factory=LinkedEntities)


class PredictionMarket(PredictionMarketFile):
    history: list[PriceObservation] = Field(default_factory=list)  # type: ignore[assignment]


class Evidence(FlopxModel):
    label: str
    href: str
    kind: str


class Geography(FlopxModel):
    region_ids: list[str] = Field(default_factory=list)


class IntelEntities(FlopxModel):
    tickers: list[str] = Field(default_factory=list)
    facility_ids: list[str] = Field(default_factory=list)
    gpu_model_ids: list[str] = Field(default_factory=list)
    market_ids: list[str] = Field(default_factory=list)


class IntelItem(FlopxModel):
    id: str
    kind: Literal["announcement", "price-move", "supply-anomaly", "earnings", "power-event",
                  "prediction-reprice", "policy", "launch", "divergence"]
    headline: str
    why_it_matters: str
    published_at: datetime
    observed_at: datetime
    severity: int = Field(ge=1, le=5)
    classification: Literal["verified", "interpretation", "prediction"]
    provenance: Provenance
    geography: Geography = Field(default_factory=Geography)
    entities: IntelEntities = Field(default_factory=IntelEntities)
    evidence: list[Evidence] = Field(default_factory=list)
    scenario_seed: Optional[dict[str, Any]] = None


class Workload(FlopxModel):
    gpu_model_ids: list[str] = Field(min_length=1)
    duration_hours: float = Field(gt=0)
    interruption_tolerance: Literal["none", "low", "high"] = "none"
    gpu_count: Optional[int] = Field(default=None, gt=0)
    gpu_hours: Optional[float] = Field(default=None, gt=0)
    deadline: Optional[datetime] = None
    region_ids: list[str] = Field(default_factory=list)
    budget_usd: Optional[float] = Field(default=None, gt=0)
    performance_needs: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _count_or_hours(self):
        if (self.gpu_count is None) == (self.gpu_hours is None):
            raise ValueError("exactly one of gpuCount or gpuHours is required")
        return self


# ============================================================================
# SAMPLE FILE ENVELOPES
# ============================================================================

class SampleFileProvenance(Provenance):
    kind: Literal["sample"]  # type: ignore[assignment]
    authored_at: str
    basis: str


class SampleFile(FlopxModel):
    provenance: SampleFileProvenance
    items: list[Any]


# ============================================================================
# API PAYLOADS (§4)
# ============================================================================

class AdjustmentStep(FlopxModel):
    name: str
    factor: float
    note: str


class Adjustment(FlopxModel):
    quoted: float
    adjusted: float
    steps: list[AdjustmentStep]
    reference: dict[str, Any]


class AdjustedOffer(GpuOffer):
    adjustment: Adjustment


class OffersResponse(FlopxModel):
    offers: list[AdjustedOffer]
    freshness: Freshness


class HeatmapCell(FlopxModel):
    gpu_model_id: str
    region_id: Optional[str] = None
    provider_id: Optional[str] = None
    value: Optional[float]
    n: int
    # The one provenance class the cell was computed over (live | sample), None for an empty cell:
    kind: Optional[Literal["live", "sample"]] = None


class HeatmapResponse(FlopxModel):
    measure: str
    measure_definition: str
    axis: Literal["region", "provider"]
    cells: list[HeatmapCell]
    legend: dict[str, Any]
    freshness: Freshness


class SeriesResponse(FlopxModel):
    series_id: str
    unit: str
    points: list[PriceObservation]
    freshness: Freshness
    # Basket changes inside `points` (an FCPI index whose sources changed); empty for every other series.
    breaks: list[SeriesBreak] = Field(default_factory=list)


class IndexListResponse(FlopxModel):
    indices: list[FcpiIndex]
    freshness: Freshness


class OfferChange(FlopxModel):
    kind: Literal["price", "availability", "new", "removed"]
    offer_id: str
    gpu_model_id: str
    provider_id: str
    region_id: str
    before: Optional[dict[str, Any]] = None
    after: Optional[dict[str, Any]] = None
    pct_change: Optional[float] = None
    evidence: list[Evidence] = Field(default_factory=list)
    # The snapshot the change's values come from (the after offer; the before offer for a removal).
    provenance: Optional[Provenance] = None


class ChangesResponse(FlopxModel):
    since: datetime
    until: datetime
    changes: list[OfferChange]
    freshness: Freshness


class FacilitiesResponse(FlopxModel):
    facilities: list[Facility]
    freshness: Freshness


class RegionRollup(FlopxModel):
    region: Region
    offers: int
    median_raw_price: Optional[float]
    median_adjusted_price: Optional[float]
    facilities_by_status: dict[str, int]
    energy_indicator: Optional[PriceObservation] = None


class RegionsResponse(FlopxModel):
    regions: list[RegionRollup]
    freshness: Freshness


class LatestFundamental(FlopxModel):
    """The newest value a company has for one concept, and which source it came from."""
    concept: str
    value: float
    unit: str
    period_end: str
    filed_at: str
    period_class: str
    source: str
    kind: str


class FundamentalFigure(FlopxModel):
    """A public figure derived from observed fundamentals (revenue growth year on year, capex intensity); `sources`
    names the raw sources behind it and `compared_to` the prior period end for a growth figure."""
    concept: str
    value: float
    unit: str
    period_end: str
    compared_to: Optional[str] = None
    sources: list[str]
    provenance: Provenance


class FundamentalsSummary(FlopxModel):
    latest: list[LatestFundamental]
    figures: list[FundamentalFigure]
    sources: list[str]
    note: str


class CompanySummary(FlopxModel):
    company: Company
    latest_close: Optional[PriceObservation] = None
    related_item_count: int = 0
    fundamentals_summary: Optional[FundamentalsSummary] = None


class CompaniesResponse(FlopxModel):
    companies: list[CompanySummary]
    freshness: Freshness


class CompanyDetail(FlopxModel):
    company: Company
    latest_bar: Optional[EquityBar] = None
    latest_close: Optional[PriceObservation] = None
    fundamentals: list[Fundamental]
    fundamentals_summary: Optional[FundamentalsSummary] = None
    related_items: list[IntelItem]
    related_facilities: list[Facility]
    unavailable: Optional[Unavailable] = None
    freshness: Freshness


class BarsResponse(FlopxModel):
    ticker: str
    interval: str
    range: str
    bars: list[EquityBar]
    unavailable: Optional[Unavailable] = None
    sample_series_id: Optional[str] = None
    freshness: Freshness


class MarketsResponse(FlopxModel):
    markets: list[PredictionMarket]
    freshness: Freshness


class FeedResponse(FlopxModel):
    items: list[IntelItem]
    freshness: Freshness


class AlignedSeries(FlopxModel):
    series_id: str
    unit: str
    points: list[PriceObservation]
    normalised: list[float]


class CorrelationRow(FlopxModel):
    overlay_id: str
    lag_days: int
    pearson: Optional[float]
    n: int


class EventAnnotation(FlopxModel):
    t: datetime
    item_id: str
    kind: str
    headline: str
    severity: int


class XrayResponse(FlopxModel):
    target: AlignedSeries
    overlays: list[AlignedSeries]
    events: list[EventAnnotation]
    correlations: list[CorrelationRow]
    caveats: list[str]
    freshness: Freshness


class ForecastRequest(FlopxModel):
    series_id: str
    horizon: int = Field(ge=1, le=90)
    level: float = Field(default=0.8, gt=0, lt=1)


class ForecastPoint(FlopxModel):
    step: int
    t: datetime
    mean: float
    lower: float
    upper: float


class BaselineForecast(FlopxModel):
    method: str
    points: list[ForecastPoint]
    residual_std: float
    parameters: dict[str, Any] = Field(default_factory=dict)


class BacktestScore(FlopxModel):
    method: str
    mae: float
    mape: float
    mae_vs_naive: float
    origins: int


class ForecastResponse(FlopxModel):
    series_id: str
    horizon: int
    level: float
    history_points: int
    baselines: list[BaselineForecast]
    backtest: list[BacktestScore]
    chosen: str
    caveats: list[str]
    # True when the history is thin for the horizon, the backtest could not run, or
    # every input is sample; the reasons name which. Screens key the EXPLORATORY chip on it.
    exploratory: bool = False
    exploratory_reasons: list[str] = []
    # Set when the series changed basket: the forecast reads only the points since this break.
    history_break: Optional[SeriesBreak] = None
    provenance: Provenance
    freshness: Freshness


class ScenarioRequest(FlopxModel):
    target: str
    assumptions: dict[str, float]
    horizon: int = Field(default=30, ge=1, le=180)


class ScenarioPoint(FlopxModel):
    step: int
    t: datetime
    baseline: float
    projected: float
    lower: float
    upper: float


class ScenarioResponse(FlopxModel):
    target: str
    label: Literal["demonstrative"]
    assumptions: dict[str, float]
    elasticities_used: dict[str, float]
    ignored_assumptions: list[str]
    total_effect_pct: float
    path: list[ScenarioPoint]
    caveats: list[str]
    provenance: Provenance
    freshness: Freshness


class Factor(FlopxModel):
    id: str
    name: str
    definition: str
    inputs: list[str]
    unit: str


class FactorsResponse(FlopxModel):
    factors: list[Factor]
    freshness: Freshness


class Decision(FlopxModel):
    t: datetime
    action: Literal["buy", "sell", "hold"]
    ticker: Optional[str]
    rationale: dict[str, Any]
    known_data_snapshot: dict[str, Any]
    mode: Literal["backtest", "forward"]
    acted: bool = False


class DecisionLedgerOut(Decision):
    """A decision with its ledger fields (architecture §10.7); every field beyond `Decision` is None for a backtest row."""
    id: Optional[int] = None
    strategy_id: Optional[str] = None
    status: Literal["proposed", "submitted", "filled", "rejected", "skipped"] = "proposed"
    signal_id: Optional[str] = None
    signal_kind: Optional[str] = None
    order_ids: list[str] = Field(default_factory=list)
    skipped_reason: Optional[str] = None
    horizon_days: Optional[int] = None
    proposed_weight: Optional[float] = None
    # The sample date a replayed decision replays; None for a forward step. A replay is hindsight
    # and never counts in forward results (falsifier R2-06).
    replay_of: Optional[str] = None
    statement_class: Literal["decision"] = "decision"


class BacktestSummary(FlopxModel):
    mode: Literal["backtest"] = "backtest"
    start: datetime
    end: datetime
    trades: int
    total_return_pct: float
    benchmark_return_pct: float
    max_drawdown_pct: float
    hit_rate: Optional[float]
    turnover: int
    decisions: list[Decision]
    provenance: Provenance
    round_trips: int = 0                  # closed buy-then-sell pairs: the hit rate's denominator
    benchmark_ticker: str = ""            # the instrument whose buy-and-hold benchmark_return_pct is
    variant: str = "compute"
    cost_bps: float = 0.0                 # charged per fill; the strategy view runs at the evaluation's 5 bps
    costs_usd: float = 0.0
    turnover_ratio: float = 0.0


class ForwardSummary(FlopxModel):
    mode: Literal["forward"] = "forward"
    decisions: list[DecisionLedgerOut]
    orders: int                           # forward decisions that placed an order; replays excluded
    proposed: int = 0                     # forward decisions still proposed; replays excluded
    replays: int = 0                      # replayed decisions listed in `decisions`, counted apart


class StrategyView(FlopxModel):
    id: str
    name: str
    objective: str
    rule: str
    parameters: dict[str, Any]
    universe: list[str]
    backtest: Optional[BacktestSummary] = None
    forward: Optional[ForwardSummary] = None


class StrategiesResponse(FlopxModel):
    strategies: list[StrategyView]
    freshness: Freshness


class StrategyStepRequest(FlopxModel):
    """Optional body of a step. `as_of` replays the rule at a past sample date (a replay, labelled
    so on the decision); omitted, the rule decides on the data known now."""
    as_of: Optional[datetime] = None


class StrategyStepResponse(FlopxModel):
    """A step PROPOSES: `order` is always None here; `POST .../decisions/{id}/accept` is what submits."""
    strategy_id: str
    decision: DecisionLedgerOut
    order: Optional[PaperOrderOut] = None
    replay: bool = False
    as_of: Optional[datetime] = None
    paper: Literal[True] = True


# -- paper desk ---------------------------------------------------------------

class PaperOrderRequest(FlopxModel):
    ticker: str
    side: Literal["buy", "sell"]
    order_type: Literal["market", "limit"] = "market"
    quantity: Optional[float] = Field(default=None, gt=0)
    notional_usd: Optional[float] = Field(default=None, gt=0)
    limit_price: Optional[float] = Field(default=None, gt=0)

    @field_validator("ticker")
    @classmethod
    def _ticker(cls, value: str) -> str:
        return normalise_ticker(value)

    @model_validator(mode="after")
    def _shape(self):
        if (self.quantity is None) == (self.notional_usd is None):
            raise ValueError("exactly one of quantity or notionalUsd is required")
        if self.order_type == "limit" and self.limit_price is None:
            raise ValueError("limitPrice is required for a limit order")
        return self


class PaperOrderOut(FlopxModel):
    id: str
    ticker: str
    side: str
    order_type: str
    quantity: float
    notional_usd: Optional[float]
    limit_price: Optional[float]
    status: str
    origin: str
    reason: Optional[str]
    fill_price: Optional[float]
    fill_provenance: Optional[Provenance]
    filled_at: Optional[datetime]
    created_at: Optional[datetime]
    paper: Literal[True] = True


class PaperPositionOut(FlopxModel):
    ticker: str
    quantity: float
    avg_cost: float
    last_price: Optional[float]
    market_value_usd: Optional[float]
    unrealised_pnl_usd: Optional[float]
    weight: Optional[float]
    price_provenance: Optional[Provenance]


class PaperAccountOut(FlopxModel):
    paper: Literal[True] = True
    cash_usd: float
    equity_usd: float
    buying_power_usd: float
    realised_pnl_usd: float
    unrealised_pnl_usd: float
    max_drawdown_pct: float
    positions: list[PaperPositionOut]
    exposure_by_theme: dict[str, float]
    open_orders: int
    freshness: Freshness


class PaperOrdersResponse(FlopxModel):
    orders: list[PaperOrderOut]
    freshness: Freshness


class CloseAllRequest(FlopxModel):
    # Strict: only the JSON literal `true` confirms a destructive close-all. Lax coercion read
    # "true", "yes", "on" and 1 as consent (falsifier V3 F7).
    confirm: StrictBool = False


class CloseAllResponse(FlopxModel):
    closed: list[PaperOrderOut]
    account: PaperAccountOut


# -- procurement --------------------------------------------------------------

class ProcurePlanRequest(FlopxModel):
    workload: Workload


class PlanOption(FlopxModel):
    rank: int
    offer_id: str
    provider_id: str
    region_id: str
    gpu_model_id: str
    contract: str
    gpu_hours: float
    unit_price_usd: float
    expected_cost_usd: float
    cost_low_usd: float
    cost_high_usd: float
    expected_duration_hours: float
    performance_score: float
    trade_offs: list[str]
    within_budget: Optional[bool]
    meets_deadline: Optional[bool]


class ProcurePlanResponse(FlopxModel):
    workload: Workload
    plans: list[PlanOption]
    excluded: dict[str, int]
    caveats: list[str]
    provenance: Provenance
    freshness: Freshness


# -- ask ----------------------------------------------------------------------

class AskRequest(FlopxModel):
    query: str = Field(min_length=1, max_length=400)


class AskAction(FlopxModel):
    kind: str
    route: str
    requires_confirmation: bool = False


class AskResponse(FlopxModel):
    query: str
    intent: str
    route: str
    filters: dict[str, Any]
    explanation: dict[str, Any]
    actions: list[AskAction]
    engine: Literal["grammar", "llm"] = "grammar"
    # The composed assessment when the intent is `brief` (architecture §10.10), None otherwise:
    brief: Optional[BriefOut] = None


# -- overview -----------------------------------------------------------------

class Indicator(FlopxModel):
    id: str
    label_key: str
    value: Optional[float]
    unit: str
    change_pct: Optional[float]
    change_window_days: int
    provenance: Provenance
    route: str


class GpuSnapshotRow(FlopxModel):
    gpu_model_id: str
    offers: int
    median_price: Optional[float]
    median_adjusted_price: Optional[float]
    min_price: Optional[float]
    available: int
    limited: int
    unavailable: int
    regions: int
    provenance: Provenance


class WatchlistRow(FlopxModel):
    ticker: str
    name: str
    last_close: Optional[PriceObservation]
    change_pct: Optional[float]


class PortfolioSummary(FlopxModel):
    paper: Literal[True] = True
    equity_usd: float
    cash_usd: float
    unrealised_pnl_usd: float
    realised_pnl_usd: float
    positions: int


class OverviewResponse(FlopxModel):
    indicators: list[Indicator]
    gpu_snapshot: list[GpuSnapshotRow]
    watchlist: list[WatchlistRow]
    markets: list[PredictionMarket]
    feed: list[IntelItem]
    portfolio_summary: Optional[PortfolioSummary] = None
    freshness: Freshness


StrategyStepResponse.model_rebuild()


# ============================================================================
# INFERRED STATE, EVALUATION, SIGNALS, FUND, INFRA (architecture §10)
# ============================================================================

StatementClass = Literal["observation", "inference", "forecast", "decision"]


class ClusterSpec(FlopxModel):
    """One row of clusters.json: an authored editorial grouping of tickers."""
    id: str
    name: str
    tickers: list[str] = Field(min_length=1)
    note: Optional[str] = None


class FactorOut(FlopxModel):
    factor: str
    raw: Optional[float]
    unit: str
    normalised: Optional[float]
    weight: float
    contribution: Optional[float]
    direction: Literal["up", "down", "flat", "none"]
    evidence: list[Provenance]
    observed_at: Optional[datetime]
    note: str
    scope: Optional[str] = None


class UncertaintyOut(FlopxModel):
    coverage: int
    factors_with_data: int
    factors_total: int
    sample_share: float
    oldest_input: Optional[datetime]
    label: Literal["exploratory", "indicative"]


class HistoryPointOut(FlopxModel):
    t: datetime
    score: float
    band: str
    coverage: int
    factors_with_data: int
    label: str
    input_kinds: list[str] = Field(default_factory=list)


class RentalReadOut(FlopxModel):
    """The cell's rental economics (N-XRAY): regional asks, or the labelled global fallback when the cell has none."""
    basis: Literal["regional-asks", "global-fallback", "none"]
    median_usd_per_gpu_hour: Optional[float]
    premium_pct: Optional[float]
    premium_label: str
    ask_count: int
    global_median_usd_per_gpu_hour: Optional[float]
    global_ask_count: int
    weekly_median_usd_per_gpu_hour: Optional[float]
    weekly_week_ending: Optional[datetime]
    source: str
    note: str
    evidence: list[Provenance]
    observed_at: Optional[datetime]


class PressureCellOut(FlopxModel):
    region_id: str
    gpu_model_id: str
    as_of: datetime
    score: float
    band: Literal["slack", "balanced", "tight", "acute"]
    factors: list[FactorOut]
    contradictions: list[FactorOut]
    uncertainty: UncertaintyOut
    method: str
    method_text: str
    input_kinds: list[str]
    provenance: Provenance
    history: list[HistoryPointOut] = Field(default_factory=list)
    history_basis: str = ""
    statement_class: Literal["inference"] = "inference"
    rental: Optional[RentalReadOut] = None


class PressureCellResponse(PressureCellOut):
    freshness: Freshness


class PressureGridResponse(FlopxModel):
    as_of: datetime
    method: str
    method_text: str
    bands: dict[str, str]
    cells: list[PressureCellOut]
    statement_class: Literal["inference"] = "inference"
    freshness: Freshness


class AssumptionOut(FlopxModel):
    name: str
    value: float
    range: list[float]
    note: str


class FacilitySupplyOut(FlopxModel):
    facility_id: str
    name: str
    status: str
    capacity_mw: Optional[float]
    gpu_count_estimate: Optional[int]
    expected_online: Optional[str]
    months_late: Optional[float]
    completion_probability: Optional[float]
    contributes_mw: float


class EffectiveSupplyOut(FlopxModel):
    region_id: str
    as_of: datetime
    operating_mw: float
    under_construction_mw: float
    announced_mw: float
    reported_mw: float
    effective_mw: float
    effective_gpu_equivalents: Optional[int]
    gpu_equivalents_coverage: str
    facilities: list[FacilitySupplyOut]
    assumptions: list[AssumptionOut]
    method: str
    input_kinds: list[str]
    provenance: Provenance
    statement_class: Literal["inference"] = "inference"


class EffectiveSupplyResponse(FlopxModel):
    regions: list[EffectiveSupplyOut]
    freshness: Freshness


class ExposurePositionOut(FlopxModel):
    quantity: float
    side: Literal["long", "short"]
    avg_cost_usd: float
    market_value_usd: Optional[float]
    weight: Optional[float]


class ExposureOut(FlopxModel):
    ticker: str
    basis: list[str]
    documented: bool
    sensitivity: float
    sensitivity_basis: str
    facility_ids: list[str]
    provenance: Provenance
    statement_class: Literal["inference"] = "inference"
    # N-XRAY: the tie in words. `tie_kind` is documented (observed facility record or SEC filing) or authored
    # (company profile or authored sample facility; `tie_note` then says "not a filing").
    role: str = ""
    tie_kind: Literal["documented", "authored"] = "authored"
    tie_basis: str = ""
    tie_note: Optional[str] = None
    # The signed-in account's paper position, when there is one; null for anonymous readers.
    position: Optional[ExposurePositionOut] = None


class ExposureResponse(FlopxModel):
    region_id: str
    gpu_model_id: Optional[str]
    exposures: list[ExposureOut]
    statement_class: Literal["inference"] = "inference"
    freshness: Freshness


class AnomalyOut(FlopxModel):
    id: str
    type: str
    region_id: str
    gpu_model_id: Optional[str]
    severity: int = Field(ge=1, le=5)
    headline: str
    rule: str
    measures: dict[str, Any]
    supporting: list[FactorOut]
    contradicting: list[FactorOut]
    uncertainty: UncertaintyOut
    first_seen: datetime
    first_seen_basis: str = ""
    status: Literal["open", "resolved"] = "open"
    last_seen: Optional[datetime] = None
    history: list[HistoryPointOut]
    linked_companies: list[ExposureOut]
    scenario_seeds: list[str]
    provenance: Provenance
    severity_basis: str
    statement_class: Literal["inference"] = "inference"


class AnomaliesResponse(FlopxModel):
    anomalies: list[AnomalyOut]
    cells_scanned: int
    rules: dict[str, str]
    freshness: Freshness


class DecisionRefOut(FlopxModel):
    decision_id: Optional[int]
    strategy_id: str
    t: datetime
    action: str
    ticker: Optional[str]
    status: str
    rationale: dict[str, Any]
    proposed_weight: Optional[float]
    horizon_days: Optional[int]
    order_ids: list[str]
    skipped_reason: Optional[str]
    mode: str
    statement_class: Literal["decision"] = "decision"


class OrderRefOut(FlopxModel):
    id: str
    status: str
    side: str
    quantity: float
    fill_price: Optional[float]
    filled_at: Optional[datetime]
    origin: str


class ForecastRefOut(FlopxModel):
    model: str
    ticker: str
    at: datetime
    horizon_days: int
    expected_log_return: float
    lower: float
    upper: float
    level: float
    window_days: int
    known_closes: int
    basis: str
    exploratory: Literal[True] = True
    statement_class: Literal["forecast"] = "forecast"


class OutcomeOut(FlopxModel):
    ticker: str
    horizon_days: int
    realised_return: float
    evaluated_at: datetime
    from_close: float
    to_close: float
    input_kinds: list[str]
    forecast_error: Optional[float] = None
    within_band: Optional[bool] = None


class ConnectedOut(FlopxModel):
    instrument: str
    basis: str


class SkippedOut(FlopxModel):
    reason: Literal["no-documented-exposure", "insufficient-sample", "risk-limit", "strategy-not-followed", "no-actionable-signal"]
    note: Optional[str] = None


class SignalOut(FlopxModel):
    id: str
    at: datetime
    kind: str
    headline: str
    source_series_id: Optional[str]
    source_id: Optional[str]
    magnitude: Optional[float]
    evidence: list[Provenance]
    connected: list[ConnectedOut]
    forecast_ref: Optional[ForecastRefOut]
    decision_refs: list[DecisionRefOut]
    orders: list[OrderRefOut]
    outcome: Optional[OutcomeOut]
    skipped: Optional[SkippedOut]
    provenance: Provenance
    statement_class: Literal["inference"] = "inference"


class AnomalyDetailResponse(AnomalyOut):
    signal: Optional[SignalOut] = None
    cell: Optional[PressureCellOut] = None
    effective_supply: Optional[EffectiveSupplyOut] = None
    freshness: Freshness


class SignalsResponse(FlopxModel):
    signals: list[SignalOut]
    freshness: Freshness


class SignalDetailResponse(SignalOut):
    anomaly: Optional[AnomalyOut] = None
    freshness: Freshness


class InfraAssumption(FlopxModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, serialize_by_alias=True, extra="allow")
    kind: Literal["delay-facility", "complete-facility", "energy-shock", "demand-shock", "utilisation"]
    facility_id: Optional[str] = None
    months: Optional[int] = Field(default=None, ge=0, le=120)
    at_date: Optional[str] = None
    pct: Optional[float] = Field(default=None, ge=-100, le=1000)
    value: Optional[float] = None


class InfraScenarioRequest(FlopxModel):
    region_id: str
    gpu_model_id: Optional[str] = None
    horizon_days: int = Field(default=90, ge=1, le=365)
    assumptions: list[InfraAssumption] = Field(default_factory=list)


class InfraPointOut(FlopxModel):
    step: int
    t: datetime
    baseline: float
    baseline_lower: float
    baseline_upper: float
    conditional: float
    lower: float
    upper: float


class ExposedMoveOut(FlopxModel):
    ticker: str
    documented: bool
    basis: list[str]
    sensitivity: float
    sensitivity_basis: str
    estimated_move_pct: float


class InfraScenarioResponse(FlopxModel):
    region_id: str
    gpu_model_id: str
    horizon_days: int
    assumptions: list[dict[str, Any]]
    applied: list[str]
    ignored: list[dict[str, Any]]
    baseline_supply: EffectiveSupplyOut
    scenario_supply: EffectiveSupplyOut
    baseline_score: float
    scenario_score: float
    pressure_delta: float
    factor_deltas: dict[str, float]
    price_effect_pct: float
    price_series_id: str
    path: list[InfraPointOut]
    exposure: list[ExposedMoveOut]
    elasticities_used: dict[str, float]
    caveats: list[str]
    statement_class: Literal["forecast"] = "forecast"
    conditional: Literal[True] = True
    label: Literal["demonstrative"] = "demonstrative"
    provenance: Provenance
    freshness: Freshness


class ModelResultOut(FlopxModel):
    model: str
    n: int
    mae: Optional[float]
    rmse: Optional[float]
    hit_rate: Optional[float]
    coverage80: Optional[float]
    skill_vs_baseline: Optional[float]
    verdict: Literal["improves", "inconclusive", "worsens", "insufficient-sample", "reference"]
    realised_vs_predicted: list[dict[str, Any]] = Field(default_factory=list)
    features_used: list[str] = Field(default_factory=list)
    baseline_rmse_matched: Optional[float] = None                  # the baseline RMSE over exactly this model's origins
    realised_vs_predicted_truncated_from: Optional[int] = None     # the full count when only the last 60 rows are shown
    note: Optional[str] = None


class HorizonResultOut(FlopxModel):
    ticker: str
    horizon_days: int
    origins: int
    models: list[ModelResultOut]
    verdict: Literal["improves", "inconclusive", "worsens", "insufficient-sample"]
    notes: list[str]


class TickerEvaluationOut(FlopxModel):
    ticker: str
    horizons: list[HorizonResultOut]
    features_available: list[str]
    features_dropped: dict[str, str]
    feature_sources: dict[str, str]
    input_kinds: list[str]
    provenance: Provenance
    method: str
    reliability_note: str
    statement_class: Literal["inference"] = "inference"


class CrossSectionOut(FlopxModel):
    """Mean Spearman IC across tickers per origin date, one row per (horizon, model); research, never a verdict."""
    horizon_days: int
    model: str
    n_dates: int
    mean_ic: Optional[float]
    t_stat: Optional[float]
    share_dates_five_plus: Optional[float]
    tickers: int
    provenance: Literal["research"] = "research"
    statement_class: Literal["inference"] = "inference"
    note: str


class TradableOut(FlopxModel):
    """Sharpe of the sign-of-forecast trade pooled over tickers, one row per (horizon, model); no costs, research."""
    horizon_days: int
    model: str
    n_trades: int
    sharpe: Optional[float]
    mean_excess_log_return: Optional[float]
    hit_rate: Optional[float]
    tickers: int
    provenance: Literal["research"] = "research"
    statement_class: Literal["inference"] = "inference"
    note: str


class HistoryCoverageOut(FlopxModel):
    ticker: str
    first_close: Optional[datetime]
    last_close: Optional[datetime]
    trading_days: int
    span_years: Optional[float]
    origins_scored: int
    origins_by_horizon: dict[int, int]
    input_kinds: list[str]


class EvaluationResponse(FlopxModel):
    target: str
    horizons: list[int]
    evaluations: list[TickerEvaluationOut]
    method_text: str
    cross_section: list[CrossSectionOut] = Field(default_factory=list)
    tradable: list[TradableOut] = Field(default_factory=list)
    history_coverage: list[HistoryCoverageOut] = Field(default_factory=list)
    freshness: Freshness


class PortfolioBacktestOut(FlopxModel):
    strategy_id: str
    variant: str
    rule: str
    mode: Literal["backtest"] = "backtest"
    start: datetime
    end: datetime
    trades: int
    total_return_pct: float
    benchmark_return_pct: float
    max_drawdown_pct: float
    hit_rate: Optional[float]
    round_trips: int
    benchmark_ticker: str
    turnover: int
    turnover_ratio: float
    cost_bps: float
    costs_usd: float
    provenance: Provenance
    statement_class: Literal["decision"] = "decision"


class PortfolioBenchmarkOut(FlopxModel):
    tickers: list[str]
    start: datetime
    end: datetime
    total_return_pct: float
    per_ticker_pct: dict[str, float]
    rule: str


class PortfolioEvaluationResponse(FlopxModel):
    strategies: list[PortfolioBacktestOut]
    benchmark: PortfolioBenchmarkOut
    cost_bps: float
    input_kinds: list[str]
    notes: list[str]
    freshness: Freshness


class AcceptDecisionResponse(FlopxModel):
    strategy_id: str
    decision: DecisionLedgerOut
    order: Optional[PaperOrderOut] = None
    paper: Literal[True] = True


class FundAccountOut(FlopxModel):
    cash_usd: float
    equity_usd: float
    buying_power_usd: float
    realised_pnl_usd: float
    unrealised_pnl_usd: float
    max_drawdown_pct: float
    positions: list[PaperPositionOut]
    exposure_by_theme: dict[str, float]
    open_orders: int


class EquityPointOut(FlopxModel):
    t: datetime
    value_usd: float
    cash_usd: Optional[float] = None
    input_kinds: list[str] = Field(default_factory=list)


class DrawdownPointOut(FlopxModel):
    t: datetime
    drawdown_pct: float


class FundBenchmarkOut(FlopxModel):
    tickers: list[str]
    curve: list[EquityPointOut]
    total_return_pct: Optional[float]
    rule: str


class AttributionOut(FlopxModel):
    """One group's P&L by the origin that opened each lot, FIFO (fund.attribution)."""
    fills: int
    bought_usd: float
    sold_usd: float
    open_value_usd: float
    realised_pnl_usd: float = 0.0
    unrealised_pnl_usd: float = 0.0
    pnl_usd: float
    unpriced: list[str] = Field(default_factory=list)


class TargetRiskOut(FlopxModel):
    """A proposed position's stated risk: one standard deviation of its value over the horizon."""
    measure: Literal["one-sigma-move-over-horizon"]
    daily_volatility_pct: float
    horizon_trading_days: int
    one_sigma_pct: float
    one_sigma_usd: float
    notional_usd: float
    closes: int
    rule: str


class TargetPositionOut(FlopxModel):
    decision_id: int
    strategy_id: str
    t: datetime
    action: str
    ticker: Optional[str]
    proposed_weight: Optional[float]
    horizon_days: Optional[int]
    rationale: dict[str, Any]
    status: Literal["proposed"]
    signal_id: Optional[str]
    uncertainty: dict[str, str]
    # When the proposal lapses (t + horizon); past it the proposal is expired and cannot be accepted.
    expires_at: Optional[datetime] = None
    provenance: Provenance
    risk: Optional[TargetRiskOut] = None
    statement_class: Literal["decision"] = "decision"


class FundSummaryResponse(FlopxModel):
    paper: Literal[True] = True
    account: FundAccountOut
    # The provenance of the account VALUE (cash plus positions at their marks); null with no positions.
    account_provenance: Optional[Provenance] = None
    exposure_by_cluster: dict[str, float]
    clusters: list[ClusterSpec]
    clusters_provenance: Provenance
    inception: Optional[datetime]
    performance_since_inception_pct: float
    equity_curve: list[EquityPointOut]
    equity_curve_provenance: Optional[Provenance]
    drawdown_series: list[DrawdownPointOut]
    max_drawdown_pct: float
    turnover_usd: float
    turnover_ratio: float
    benchmark: FundBenchmarkOut
    attribution_by_origin: dict[str, AttributionOut]
    attribution_by_signal_kind: dict[str, AttributionOut]
    target_positions: list[TargetPositionOut]
    fills: int
    freshness: Freshness


class EnergyZoneOut(FlopxModel):
    zone: str
    series_id: str
    unit: str
    latest: float
    latest_at: datetime
    median_30d: float
    change_pct_30d: Optional[float]
    points: list[PriceObservation]
    regions: list[str]
    provenance: Provenance
    statement_class: Literal["observation"] = "observation"


class InfraEnergyResponse(FlopxModel):
    zones: list[EnergyZoneOut]
    missing: list[dict[str, Any]]
    freshness: Freshness


class ProjectOut(FlopxModel):
    facility: Facility
    stage: str
    months_late: Optional[float]
    documented_owners: list[dict[str, Any]]
    source_count: int
    statement_class: Literal["observation"] = "observation"


class InfraProjectsResponse(FlopxModel):
    projects: list[ProjectOut]
    freshness: Freshness


class GridRegionOut(FlopxModel):
    region: Region
    series_id: Optional[str]
    latest_price: Optional[float]
    unit: Optional[str]
    power_stress_raw: Optional[float]
    power_stress_normalised: Optional[float]
    constrained: bool
    reason: str
    provenance: Optional[Provenance]
    statement_class: Literal["observation"] = "observation"
    flag_class: Literal["inference"] = "inference"


class InfraGridResponse(FlopxModel):
    regions: list[GridRegionOut]
    constraint_rule: str
    freshness: Freshness


class SentenceOut(FlopxModel):
    text: str
    statement_class: StatementClass
    evidence_ids: list[str]


class EvidenceRefOut(FlopxModel):
    id: str
    kind: str
    href: str
    statement_class: StatementClass
    observed_at: Optional[datetime] = None
    provenance_kind: Optional[str] = None
    provenance: Optional[Provenance] = None
    input_kinds: list[str] = Field(default_factory=list)


class BriefOut(FlopxModel):
    target_kind: str
    target_id: str
    headline: str
    sentences: list[SentenceOut]
    evidence: list[EvidenceRefOut]
    follow_through: list[dict[str, Any]]
    caveats: list[str]
    method: Literal["composed-from-evidence-v1"]
    statement_class: Literal["inference"] = "inference"
    not_language_model: Literal[True] = True
    engine: Literal["composed"] = "composed"
    language_model_note: str
    freshness: Optional[Freshness] = None


AskResponse.model_rebuild()
