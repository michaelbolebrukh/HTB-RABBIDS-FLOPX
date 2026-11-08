"""FLOPX domain entities (architecture §3) as plain dataclasses.

These are the in-process working types. `product.schemas` carries the pydantic
mirrors used for file validation and the wire (camelCase aliases); the field
names are identical so a dataclass serialises straight through a response model
with `from_attributes=True`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from product.flopx.provenance import Provenance


@dataclass(frozen=True)
class GpuModel:
    id: str                # slug: h100-sxm-80gb
    vendor: str
    family: str
    memory_gb: int
    interconnect: str
    fp16_tflops: float
    release_year: int


@dataclass(frozen=True)
class Provider:
    id: str
    name: str
    kind: str              # marketplace | neocloud | hyperscaler
    website: str | None = None


@dataclass(frozen=True)
class Region:
    id: str                # ISO country or us-tx
    name: str
    country_code: str
    lat: float
    lon: float
    grid_zone: str | None = None


@dataclass(frozen=True)
class GpuOffer:
    id: str
    provider_id: str
    region_id: str
    gpu_model_id: str
    gpu_count: int
    price_per_gpu_hour_usd: float
    availability: str      # available | limited | unavailable
    interruptible: bool
    contract: str          # on-demand | reserved | spot
    provenance: Provenance
    reliability_score: float | None = None
    bandwidth_gbps: float | None = None
    min_duration_hours: int | None = None


@dataclass(frozen=True)
class PriceObservation:
    series_id: str
    t: datetime
    value: float
    unit: str
    provenance: Provenance


@dataclass(frozen=True)
class Series:
    id: str
    unit: str
    points: list[PriceObservation]


@dataclass(frozen=True)
class FcpiCoverage:
    providers: int
    regions: int
    offer_count: int


@dataclass(frozen=True)
class SeriesBreak:
    """A point where a series stops being comparable with what came before it (R1-4): for an index, the
    set of sources behind the median changed. `t` is the first point of the new basket; readings that
    compare two points (momentum, change, forecast history) never span it."""
    t: datetime
    reason: str                       # "basket-changed"
    before: list[str] = field(default_factory=list)
    after: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class FcpiIndex:
    id: str
    gpu_model_id: str
    methodology: str
    coverage: FcpiCoverage
    normalisation: list[str]
    last_updated: datetime
    values: list[PriceObservation]
    limitations: list[str]
    provenance: Provenance
    current: float | None = None
    # The sources behind `current` (live indices; empty for a sample index) and every basket change
    # inside `values`, oldest first. Set by the request layer (terminal.py `_index_for`), not the engine.
    basket: list[str] = field(default_factory=list)
    breaks: list[SeriesBreak] = field(default_factory=list)


@dataclass(frozen=True)
class FacilityTimeline:
    announced: str | None = None
    construction_start: str | None = None
    expected_online: str | None = None


@dataclass(frozen=True)
class SourceLink:
    url: str
    date: str
    label: str | None = None


@dataclass(frozen=True)
class Facility:
    id: str
    name: str
    lat: float
    lon: float
    region_id: str
    status: str            # operating | under-construction | announced | planned | reported
    provenance: Provenance
    operator: str | None = None
    owner_company_ids: list[str] = field(default_factory=list)
    capacity_mw: float | None = None
    gpu_count_estimate: int | None = None
    timeline: FacilityTimeline = field(default_factory=FacilityTimeline)
    sources: list[SourceLink] = field(default_factory=list)


@dataclass(frozen=True)
class ComputeExposure:
    text: str
    tags: list[str]


@dataclass(frozen=True)
class Fundamental:
    ticker: str
    concept: str
    value: float
    unit: str
    period_end: str
    filed_at: str          # availability date: nothing is "known" before this
    provenance: Provenance


@dataclass(frozen=True)
class Company:
    ticker: str
    name: str
    exchange: str
    segments: list[str]
    compute_exposure: ComputeExposure
    related_facility_ids: list[str]
    peers: list[str]
    provenance: Provenance
    cik: str | None = None
    fundamentals: list[Fundamental] = field(default_factory=list)
    # Authored reference close used only when no live/archived close exists;
    # labelled sample wherever it is served:
    sample_last_close_usd: float | None = None


@dataclass(frozen=True)
class EquityBar:
    ticker: str
    t: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    provenance: Provenance


@dataclass(frozen=True)
class OutcomeQuote:
    label: str
    price: float
    implied_probability: float


@dataclass(frozen=True)
class LinkedEntities:
    tickers: list[str] = field(default_factory=list)
    region_ids: list[str] = field(default_factory=list)
    facility_ids: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class PredictionMarket:
    id: str
    venue: str
    question: str
    outcomes: list[OutcomeQuote]
    last_updated: datetime
    provenance: Provenance
    resolution_rules: str | None = None
    liquidity: float | None = None
    volume_24h: float | None = None
    history: list[PriceObservation] = field(default_factory=list)
    linked_entities: LinkedEntities = field(default_factory=LinkedEntities)


@dataclass(frozen=True)
class Evidence:
    label: str
    href: str
    kind: str              # offer | series | filing | market | article | facility


@dataclass(frozen=True)
class Geography:
    region_ids: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class IntelEntities:
    tickers: list[str] = field(default_factory=list)
    facility_ids: list[str] = field(default_factory=list)
    gpu_model_ids: list[str] = field(default_factory=list)
    market_ids: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class IntelItem:
    id: str
    kind: str
    headline: str
    why_it_matters: str
    published_at: datetime
    observed_at: datetime
    severity: int
    classification: str    # verified | interpretation | prediction
    provenance: Provenance
    geography: Geography = field(default_factory=Geography)
    entities: IntelEntities = field(default_factory=IntelEntities)
    evidence: list[Evidence] = field(default_factory=list)
    scenario_seed: dict | None = None


@dataclass(frozen=True)
class Workload:
    gpu_model_ids: list[str]
    duration_hours: float
    interruption_tolerance: str          # none | low | high
    gpu_count: int | None = None
    gpu_hours: float | None = None
    deadline: datetime | None = None
    region_ids: list[str] = field(default_factory=list)
    budget_usd: float | None = None
    performance_needs: list[str] = field(default_factory=list)   # tokens: high-memory | fast-interconnect | fp8


# ============================================================================
# Inferred state, evaluation, signals (architecture §10)
# ============================================================================

@dataclass(frozen=True)
class Factor:
    """One cpx-v1 factor with its evidence (§10.2); `normalised` is None when no data exists."""
    factor: str
    raw: float | None
    unit: str
    normalised: float | None
    weight: float
    contribution: float | None
    direction: str                       # up | down | flat | none
    evidence: list[Provenance]
    observed_at: datetime | None
    note: str
    scope: str | None = None             # "global" when a regional series was wanted and a global one used


@dataclass(frozen=True)
class Uncertainty:
    coverage: int
    factors_with_data: int
    factors_total: int
    sample_share: float
    oldest_input: datetime | None
    label: str                           # exploratory | indicative (never measured)


@dataclass(frozen=True)
class HistoryPoint:
    t: datetime
    score: float
    band: str
    coverage: int
    factors_with_data: int
    label: str
    input_kinds: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class RentalRead:
    """The cell's rental economics as the X-Ray states them (N-XRAY). `basis` is `regional-asks` when the
    cell has advertised asks of its own (median and premium read from them), `global-fallback` when it has
    none and the model's global median of live asks and GetDeploying's archived weekly median stand in
    (premium `None`, `premium_label` says why), or `none` when neither exists."""
    basis: str                                   # regional-asks | global-fallback | none
    median_usd_per_gpu_hour: float | None
    premium_pct: float | None
    premium_label: str                           # e.g. "+12.3%" or "n/a (no regional asks)"
    ask_count: int
    global_median_usd_per_gpu_hour: float | None
    global_ask_count: int
    weekly_median_usd_per_gpu_hour: float | None  # GetDeploying weekly median (archived), latest week
    weekly_week_ending: datetime | None
    source: str
    note: str
    evidence: list[Provenance]
    observed_at: datetime | None


@dataclass(frozen=True)
class PressureCell:
    region_id: str
    gpu_model_id: str
    as_of: datetime
    score: float
    band: str
    factors: list[Factor]
    contradictions: list[Factor]
    uncertainty: Uncertainty
    method: str
    method_text: str
    input_kinds: list[str]
    provenance: Provenance
    history: list[HistoryPoint] = field(default_factory=list)
    history_basis: str = ""
    statement_class: str = "inference"
    rental: RentalRead | None = None


@dataclass(frozen=True)
class Assumption:
    name: str
    value: float
    range: list[float]
    note: str


@dataclass(frozen=True)
class FacilitySupply:
    facility_id: str
    name: str
    status: str
    capacity_mw: float | None
    gpu_count_estimate: int | None
    expected_online: str | None
    months_late: float | None
    completion_probability: float | None
    contributes_mw: float


@dataclass(frozen=True)
class EffectiveSupply:
    region_id: str
    as_of: datetime
    operating_mw: float
    under_construction_mw: float
    announced_mw: float                  # announced + planned
    reported_mw: float                   # listed separately, never counted
    effective_mw: float
    effective_gpu_equivalents: int | None
    gpu_equivalents_coverage: str
    facilities: list[FacilitySupply]
    assumptions: list[Assumption]
    method: str
    input_kinds: list[str]
    provenance: Provenance
    statement_class: str = "inference"


@dataclass(frozen=True)
class ExposurePosition:
    """The signed-in account's paper position in an exposed company, overlaid per request (never cached)."""
    quantity: float
    side: str                            # long | short
    avg_cost_usd: float
    market_value_usd: float | None
    weight: float | None


@dataclass(frozen=True)
class Exposure:
    ticker: str
    basis: list[str]                     # facility-owner | facility-operator | filing-mention | sample-profile
    documented: bool
    sensitivity: float
    sensitivity_basis: str
    facility_ids: list[str]
    provenance: Provenance
    statement_class: str = "inference"
    # N-XRAY: the row says what it rests on in words. `tie_kind` is `documented` (an observed facility record
    # or an SEC filing) or `authored` (the company profile or an authored sample facility; not a filing).
    role: str = ""
    tie_kind: str = "authored"
    tie_basis: str = ""
    tie_note: str | None = None
    position: ExposurePosition | None = None


@dataclass(frozen=True)
class Anomaly:
    id: str
    type: str
    region_id: str
    gpu_model_id: str | None
    severity: int
    headline: str
    rule: str
    measures: dict
    supporting: list[Factor]
    contradicting: list[Factor]
    uncertainty: Uncertainty
    first_seen: datetime
    history: list[HistoryPoint]
    linked_companies: list[Exposure]
    scenario_seeds: list[str]
    provenance: Provenance
    severity_basis: str
    statement_class: str = "inference"
    first_seen_basis: str = ""           # which of history / first detection / this snapshot first_seen is
    status: str = "open"                 # open | resolved (the rule no longer holds; served from the last time it did)
    last_seen: datetime | None = None    # resolved only: the snapshot at which the rule last held
