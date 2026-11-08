"""Loads and validates the SAMPLE datasets, once per process (TTL-only cache).

Every file is validated against product.schemas at load time, then cross-
referenced (an offer's provider, region and GPU model must exist; a company's
related facilities must exist; ...). A broken file raises
SampleDataInvalidError naming the file and the problem, which the startup task
turns into a loud boot-time log line rather than a 500 on the first request.

The loaded object is held once per process and shared read-only (see
`load_sample`); a one-hour TTL bounds a stale read after a redeploy.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import ValidationError

from product import schemas
from product.exceptions import SampleDataInvalidError
from product.flopx import domain
from product.flopx.provenance import Provenance

SAMPLE_DIR = Path(__file__).resolve().parent / "sample"
CACHE_KEY = "flopx:sample-data:v1"
CACHE_TTL_SECONDS = 3600

FILES = {
    "gpu_models": ("gpu_models.json", schemas.GpuModel),
    "providers": ("providers.json", schemas.Provider),
    "regions": ("regions.json", schemas.Region),
    "offers": ("gpu_offers.json", schemas.GpuOffer),
    "offers_previous": ("gpu_offers_previous.json", schemas.GpuOffer),
    "facilities": ("facilities.json", schemas.Facility),
    "companies": ("companies.json", schemas.Company),
    "markets": ("markets.json", schemas.PredictionMarketFile),
    "feed": ("feed.json", schemas.IntelItem),
    "series": ("series.json", schemas.SeriesFile),
}


@dataclass
class SampleData:
    file_provenance: dict[str, Provenance]
    gpu_models: dict[str, domain.GpuModel]
    providers: dict[str, domain.Provider]
    regions: dict[str, domain.Region]
    offers: list[domain.GpuOffer]
    offers_previous: list[domain.GpuOffer]
    facilities: list[domain.Facility]
    companies: dict[str, domain.Company]
    markets: list[domain.PredictionMarket]
    feed: list[domain.IntelItem]
    series: dict[str, domain.Series]
    problems: list[str] = field(default_factory=list)

    @property
    def facilities_by_id(self) -> dict[str, domain.Facility]:
        return {f.id: f for f in self.facilities}

    @property
    def markets_by_id(self) -> dict[str, domain.PredictionMarket]:
        return {m.id: m for m in self.markets}

    @property
    def feed_by_id(self) -> dict[str, domain.IntelItem]:
        return {i.id: i for i in self.feed}


# -- reading ---------------------------------------------------------------------

def _read(sample_dir: Path, name: str, model) -> tuple[Provenance, list]:
    path = sample_dir / name
    if not path.exists():
        raise SampleDataInvalidError(file=name, problem="missing")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        envelope = schemas.SampleFile.model_validate(raw)
        items = [model.model_validate(item) for item in envelope.items]
    except (ValueError, ValidationError) as exc:
        raise SampleDataInvalidError(file=name, problem=str(exc)[:500]) from exc
    prov = Provenance.from_dict(envelope.provenance.model_dump(by_alias=False))
    return prov, items


def _prov(model_prov: schemas.Provenance) -> Provenance:
    return Provenance.from_dict(model_prov.model_dump(by_alias=False))


def _to_domain(cls, model, **overrides):
    data = model.model_dump(by_alias=False)
    data.update(overrides)
    return cls(**data)


def _offer(m: schemas.GpuOffer) -> domain.GpuOffer:
    return _to_domain(domain.GpuOffer, m, provenance=_prov(m.provenance))


def _facility(m: schemas.Facility) -> domain.Facility:
    return _to_domain(
        domain.Facility, m,
        provenance=_prov(m.provenance),
        timeline=domain.FacilityTimeline(**m.timeline.model_dump(by_alias=False)),
        sources=[domain.SourceLink(**s.model_dump(by_alias=False)) for s in m.sources],
    )


def _company(m: schemas.Company) -> domain.Company:
    return _to_domain(
        domain.Company, m,
        provenance=_prov(m.provenance),
        compute_exposure=domain.ComputeExposure(**m.compute_exposure.model_dump(by_alias=False)),
        fundamentals=[_to_domain(domain.Fundamental, f, provenance=_prov(f.provenance)) for f in m.fundamentals],
    )


def _market(m: schemas.PredictionMarketFile) -> domain.PredictionMarket:
    prov = _prov(m.provenance)
    history = [
        domain.PriceObservation(series_id=f"market:{m.id}:yes", t=t, value=v, unit="probability", provenance=prov)
        for t, v in m.history
    ]
    return _to_domain(
        domain.PredictionMarket, m,
        provenance=prov,
        outcomes=[domain.OutcomeQuote(**o.model_dump(by_alias=False)) for o in m.outcomes],
        history=history,
        linked_entities=domain.LinkedEntities(**{k: v for k, v in m.linked_entities.model_dump(by_alias=False).items() if k != "gpu_model_ids"}),
    )


def _intel(m: schemas.IntelItem) -> domain.IntelItem:
    return _to_domain(
        domain.IntelItem, m,
        provenance=_prov(m.provenance),
        geography=domain.Geography(**m.geography.model_dump(by_alias=False)),
        entities=domain.IntelEntities(**m.entities.model_dump(by_alias=False)),
        evidence=[domain.Evidence(**e.model_dump(by_alias=False)) for e in m.evidence],
    )


def _series(m: schemas.SeriesFile, file_prov: Provenance) -> domain.Series:
    points = [
        domain.PriceObservation(
            series_id=m.id, t=t, value=v, unit=m.unit,
            provenance=Provenance(kind="sample", source=file_prov.source, observed_at=t, note=file_prov.note),
        )
        for t, v in sorted(m.points, key=lambda p: p[0])
    ]
    return domain.Series(id=m.id, unit=m.unit, points=points)


def load_sample_uncached(sample_dir: Path = SAMPLE_DIR) -> SampleData:
    provs: dict[str, Provenance] = {}
    raw: dict[str, list] = {}
    for key, (name, model) in FILES.items():
        provs[key], raw[key] = _read(sample_dir, name, model)

    gpu_models = {m.id: _to_domain(domain.GpuModel, m) for m in raw["gpu_models"]}
    providers = {m.id: _to_domain(domain.Provider, m) for m in raw["providers"]}
    regions = {m.id: _to_domain(domain.Region, m) for m in raw["regions"]}
    data = SampleData(
        file_provenance=provs,
        gpu_models=gpu_models,
        providers=providers,
        regions=regions,
        offers=[_offer(m) for m in raw["offers"]],
        offers_previous=[_offer(m) for m in raw["offers_previous"]],
        facilities=[_facility(m) for m in raw["facilities"]],
        companies={m.ticker: _company(m) for m in raw["companies"]},
        markets=[_market(m) for m in raw["markets"]],
        feed=[_intel(m) for m in raw["feed"]],
        series={m.id: _series(m, provs["series"]) for m in raw["series"]},
    )
    problems = validate_cross_references(data)
    if problems:
        raise SampleDataInvalidError(file="(cross-references)", problem="; ".join(problems[:20]), count=len(problems))
    return data


def validate_cross_references(data: SampleData) -> list[str]:
    """Every id a record points at must resolve; returns the list of violations."""
    problems: list[str] = []
    facility_ids = {f.id for f in data.facilities}
    market_ids = {m.id for m in data.markets}
    for name, offers in (("offers", data.offers), ("offers_previous", data.offers_previous)):
        seen: set[str] = set()
        for o in offers:
            if o.id in seen:
                problems.append(f"{name}: duplicate offer id {o.id}")
            seen.add(o.id)
            if o.provider_id not in data.providers:
                problems.append(f"{name}: {o.id} unknown provider {o.provider_id}")
            if o.region_id not in data.regions:
                problems.append(f"{name}: {o.id} unknown region {o.region_id}")
            if o.gpu_model_id not in data.gpu_models:
                problems.append(f"{name}: {o.id} unknown gpu model {o.gpu_model_id}")
            if o.provenance.kind != "sample":
                problems.append(f"{name}: {o.id} is not labelled sample")
    for f in data.facilities:
        if f.region_id not in data.regions:
            problems.append(f"facilities: {f.id} unknown region {f.region_id}")
        for ticker in f.owner_company_ids:
            if ticker not in data.companies:
                problems.append(f"facilities: {f.id} unknown owner {ticker}")
    for c in data.companies.values():
        for fid in c.related_facility_ids:
            if fid not in facility_ids:
                problems.append(f"companies: {c.ticker} unknown facility {fid}")
        for peer in c.peers:
            if peer not in data.companies:
                problems.append(f"companies: {c.ticker} unknown peer {peer}")
        if f"equity:{c.ticker}:close" not in data.series:
            problems.append(f"companies: {c.ticker} has no sample close series")
    for item in data.feed:
        for rid in item.geography.region_ids:
            if rid not in data.regions:
                problems.append(f"feed: {item.id} unknown region {rid}")
        for fid in item.entities.facility_ids:
            if fid not in facility_ids:
                problems.append(f"feed: {item.id} unknown facility {fid}")
        for mid in item.entities.market_ids:
            if mid not in market_ids:
                problems.append(f"feed: {item.id} unknown market {mid}")
        for gid in item.entities.gpu_model_ids:
            if gid not in data.gpu_models:
                problems.append(f"feed: {item.id} unknown gpu model {gid}")
        for ticker in item.entities.tickers:
            if ticker not in data.companies:
                problems.append(f"feed: {item.id} unknown ticker {ticker}")
    for gid in data.gpu_models:
        for suffix in ("", ":monthly"):
            if f"gpu:{gid}:on-demand:global{suffix}" not in data.series:
                problems.append(f"series: gpu model {gid} missing on-demand series{suffix}")
    for s in data.series.values():
        if len(s.points) < 2:
            problems.append(f"series: {s.id} has fewer than 2 points")
    return problems


_shared: tuple[float, SampleData] | None = None
_shared_lock = threading.Lock()


def load_sample() -> SampleData:
    """The entry point every reader uses: ONE shared, read-only object per process, reloaded after
    `CACHE_TTL_SECONDS` (B-PERF). It used to come through the platform cache, whose memory backend
    deep-copies on every read: about 87 ms per call locally, several times that on the 0.5-CPU
    Starter instance, paid by every request (some call it twice). The data is files, identical in
    every worker, so nothing is lost by holding it in the process; readers must never mutate it in
    place (they build new lists; `test_perf_snapshots` pins that the shared object is unchanged
    after every composed read)."""
    global _shared
    now = time.monotonic()
    held = _shared
    if held is not None and now - held[0] < CACHE_TTL_SECONDS:
        return held[1]
    with _shared_lock:
        held = _shared
        if held is None or time.monotonic() - held[0] >= CACHE_TTL_SECONDS:
            _shared = held = (time.monotonic(), load_sample_uncached())
        return held[1]


def reset_shared_sample() -> None:
    """Drops the process copy so the next read reloads the files (tests that swap the sample dir)."""
    global _shared
    with _shared_lock:
        _shared = None
