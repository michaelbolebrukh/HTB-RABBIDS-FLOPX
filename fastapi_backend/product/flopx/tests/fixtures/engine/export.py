"""Engine drift fixtures: the Python engine's answers, frozen as JSON for a port to test against.

Eight files sit beside this script (adjust, fcpi, changes, forecast, scenario,
procure, ask, xray). Each is a JSON list of cases ``{"name", "input", "expected"}``
where ``input`` is JSON-native (camelCase keys, ISO 8601 datetimes, no NaN) and
``expected`` is what the Python engine actually returned for it, rounded to six
decimals where the engine itself does not round. The backend test
``product/flopx/tests/test_engine_fixtures.py`` re-runs every case through
``run_case`` below and asserts equality, so the files cannot drift from the
Python; the TypeScript port is tested against the same files.

Usage (from ``fastapi_backend/``)::

    .venv/bin/python product/flopx/tests/fixtures/engine/export.py          # rewrite the files
    .venv/bin/python product/flopx/tests/fixtures/engine/export.py --check  # exit 1 if they would change

Every case is deterministic: time-dependent fields (``fetchedAt``, ``now``) are
either fixed in the input or excluded from the expected output.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from dataclasses import asdict, is_dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
BACKEND_ROOT = HERE.parents[4]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

# The engine imports settings at import time; the same safe defaults the test
# conftest sets, so the script runs standalone without a developer .env:
os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("HASH_KEY", "fixture-export-only")
os.environ.setdefault("APP_DOMAIN", "test.local")
os.environ.setdefault("ENV", "development")

from product.exceptions import FlopxDomainError  # noqa: E402
from types import SimpleNamespace  # noqa: E402

from product.flopx import adjust, anomalies, ask, assistant, changes, domain, effective_supply, evaluation, exposure, forecast, pressure, procurement, scenario, signals, xray  # noqa: E402
from product.flopx.data.loader import load_sample_uncached  # noqa: E402
from product.flopx.indices import compute_index  # noqa: E402
from product.flopx.provenance import Provenance, derived  # noqa: E402

ENGINES = ("adjust", "fcpi", "changes", "forecast", "scenario", "procure", "ask", "xray",
           "pressure", "effective_supply", "exposure", "anomalies", "infra_scenario", "evaluation", "signals", "assistant", "evaluation_terminal")
# The inferred-state fixtures carry whole-grid inputs (every offer, facility, market and feed item the
# engine reads), so they are allowed more room than the per-engine 60 KB the first eight keep to. The infra
# scenario file sat at 199 KB of 200 when the R1-3 price-floor case (a minimal 5 KB input) was added, hence 210:
SIZE_LIMIT_KB = {"pressure": 200, "anomalies": 200, "effective_supply": 80, "exposure": 100, "infra_scenario": 210, "evaluation": 160,
                 "signals": 120, "assistant": 200, "evaluation_terminal": 200}
DEFAULT_SIZE_LIMIT_KB = 60
ROUND = 6
T0 = "2026-11-06T21:00:00Z"


# -- JSON-native helpers ------------------------------------------------------------

def _camel(key: str) -> str:
    if "_" not in key:
        return key
    head, *rest = key.split("_")
    return head + "".join(part[:1].upper() + part[1:] for part in rest)


def _snake(key: str) -> str:
    out = []
    for ch in key:
        if ch.isupper():
            out.append("_" + ch.lower())
        else:
            out.append(ch)
    return "".join(out)


def to_json(value):
    """Dataclasses, datetimes, NaN and snake_case keys into the fixture shape."""
    if is_dataclass(value) and not isinstance(value, type):
        return to_json(asdict(value))
    if isinstance(value, dict):
        return {_camel(str(k)): to_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_json(v) for v in value]
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        if value == int(value) and abs(value) < 1e15:
            return round(value, ROUND)
        return round(value, ROUND)
    if hasattr(value, "value") and not isinstance(value, (str, int)):
        return value.value
    return value


def _dt(text: str) -> datetime:
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _prov(data: dict | None) -> Provenance:
    data = data or {"kind": "sample", "source": "flopx-sample-v1", "observedAt": T0}
    return Provenance.from_dict(data)


def _offer(data: dict) -> domain.GpuOffer:
    fields = {_snake(k): v for k, v in data.items()}
    fields["provenance"] = _prov(data.get("provenance"))
    return domain.GpuOffer(**fields)


def _gpu_model(data: dict) -> domain.GpuModel:
    return domain.GpuModel(**{_snake(k): v for k, v in data.items()})


def _region(data: dict) -> domain.Region:
    return domain.Region(**{_snake(k): v for k, v in data.items()})


def _series(data: dict) -> domain.Series:
    kind = data.get("kind", "sample")
    points = [
        domain.PriceObservation(
            series_id=data["id"], t=_dt(t), value=v, unit=data["unit"],
            provenance=Provenance(kind=kind, source=data.get("source", "flopx-sample-v1"), observed_at=_dt(t)),
        )
        for t, v in data["points"]
    ]
    return domain.Series(id=data["id"], unit=data["unit"], points=points)


def _intel(data: dict) -> domain.IntelItem:
    return domain.IntelItem(
        id=data["id"], kind=data["kind"], headline=data["headline"], why_it_matters=data.get("whyItMatters", ""),
        published_at=_dt(data.get("publishedAt", data["observedAt"])), observed_at=_dt(data["observedAt"]),
        severity=data["severity"], classification=data.get("classification", "interpretation"),
        provenance=_prov(data.get("provenance")),
        entities=domain.IntelEntities(**{_snake(k): v for k, v in data.get("entities", {}).items()}),
        geography=domain.Geography(region_ids=data.get("geography", {}).get("regionIds", [])),
    )


def _facility(data: dict) -> domain.Facility:
    return domain.Facility(
        id=data["id"], name=data.get("name", data["id"]), lat=data.get("lat", 0.0), lon=data.get("lon", 0.0), region_id=data["regionId"],
        status=data["status"], provenance=_prov(data.get("provenance")), operator=data.get("operator"),
        owner_company_ids=data.get("ownerCompanyIds", []), capacity_mw=data.get("capacityMw"), gpu_count_estimate=data.get("gpuCountEstimate"),
        timeline=domain.FacilityTimeline(**{_snake(k): v for k, v in data.get("timeline", {}).items()}),
    )


def _market(data: dict) -> domain.PredictionMarket:
    prov = _prov(data.get("provenance"))
    return domain.PredictionMarket(
        id=data["id"], venue=data.get("venue", "fixture"), question=data["question"],
        outcomes=[domain.OutcomeQuote("Yes", data["yes"], data["yes"]), domain.OutcomeQuote("No", 1.0 - data["yes"], 1.0 - data["yes"])],
        last_updated=_dt(data["lastUpdated"]), provenance=prov,
        history=[domain.PriceObservation(series_id=f"market:{data['id']}:yes", t=_dt(t), value=v, unit="probability", provenance=prov)
                 for t, v in data.get("history", [])],
    )


def _company(data: dict) -> domain.Company:
    return domain.Company(
        ticker=data["ticker"], name=data.get("name", data["ticker"]), exchange=data.get("exchange", "X"), segments=[],
        compute_exposure=domain.ComputeExposure(text="", tags=data.get("tags", [])), related_facility_ids=[], peers=[],
        provenance=_prov(data.get("provenance")),
    )


def _series_map(data: dict) -> dict[str, domain.Series]:
    return {sid: _series({"id": sid, **body}) for sid, body in data.items()}


def _prov_out(p: Provenance) -> dict:
    return {"kind": p.kind, "source": p.source, "observedAt": to_json(p.observed_at)}


def _factor_out(f: domain.Factor) -> dict:
    return {"factor": f.factor, "raw": to_json(f.raw), "unit": f.unit, "normalised": to_json(f.normalised), "weight": f.weight,
            "contribution": to_json(f.contribution), "direction": f.direction, "scope": f.scope, "observedAt": to_json(f.observed_at),
            "evidence": [_prov_out(p) for p in f.evidence], "note": f.note}


def _cell_out(cell: domain.PressureCell, *, full: bool = True) -> dict:
    row = {"regionId": cell.region_id, "gpuModelId": cell.gpu_model_id, "asOf": to_json(cell.as_of), "score": cell.score, "band": cell.band,
           "statementClass": cell.statement_class, "method": cell.method, "inputKinds": cell.input_kinds, "uncertainty": to_json(cell.uncertainty),
           "contradictions": [f.factor for f in cell.contradictions], "history": to_json(cell.history), "historyBasis": cell.history_basis}
    if full:
        row["factors"] = [_factor_out(f) for f in cell.factors]
        row["provenance"] = {**_prov_out(cell.provenance), "inputKinds": list(cell.provenance.input_kinds)}
    return row


def _cell_in(cell: domain.PressureCell) -> dict:
    """A cell as anomaly-fixture INPUT: scores and factor numbers only, no evidence or prose."""
    return {"regionId": cell.region_id, "gpuModelId": cell.gpu_model_id, "asOf": to_json(cell.as_of), "score": cell.score, "band": cell.band,
            "factors": [{"factor": f.factor, "raw": to_json(f.raw), "unit": f.unit, "normalised": to_json(f.normalised), "weight": f.weight,
                         "contribution": to_json(f.contribution), "direction": f.direction} for f in cell.factors],
            "contradictions": [f.factor for f in cell.contradictions], "uncertainty": to_json(cell.uncertainty),
            "history": [{"t": to_json(h.t), "score": h.score, "band": h.band} for h in cell.history]}


def _cell_from(data: dict) -> domain.PressureCell:
    factors = [domain.Factor(factor=f["factor"], raw=f.get("raw"), unit=f.get("unit", ""), normalised=f.get("normalised"), weight=f["weight"],
                             contribution=f.get("contribution"), direction=f["direction"], evidence=[], observed_at=None, note="")
               for f in data["factors"]]
    by_name = {f.factor: f for f in factors}
    u = data["uncertainty"]
    uncertainty = domain.Uncertainty(coverage=u["coverage"], factors_with_data=u["factorsWithData"], factors_total=u.get("factorsTotal", 6),
                                     sample_share=u.get("sampleShare", 1.0), oldest_input=_dt(u["oldestInput"]) if u.get("oldestInput") else None,
                                     label=u["label"])
    as_of = _dt(data["asOf"])
    prov = Provenance(kind="derived", source=pressure.METHOD, observed_at=as_of, input_kinds=("sample",))
    history = [domain.HistoryPoint(t=_dt(h["t"]), score=h["score"], band=h["band"], coverage=0, factors_with_data=0, label="") for h in data.get("history", [])]
    return domain.PressureCell(region_id=data["regionId"], gpu_model_id=data["gpuModelId"], as_of=as_of, score=data["score"], band=data["band"],
                               factors=factors, contradictions=[by_name[n] for n in data.get("contradictions", []) if n in by_name],
                               uncertainty=uncertainty, method=pressure.METHOD, method_text="", input_kinds=["sample"], provenance=prov, history=history)


def _exposure_from(data: dict) -> domain.Exposure:
    return domain.Exposure(ticker=data["ticker"], basis=data.get("basis", []), documented=data["documented"], sensitivity=0.0, sensitivity_basis="",
                           facility_ids=data.get("facilityIds", []), provenance=Provenance(kind="derived", source="exposure-v1", observed_at=_dt(T0)))


def _anomaly_out(a: domain.Anomaly) -> dict:
    return {"id": a.id, "type": a.type, "regionId": a.region_id, "gpuModelId": a.gpu_model_id, "severity": a.severity, "severityBasis": a.severity_basis,
            "headline": a.headline, "rule": a.rule, "measures": to_json(a.measures), "statementClass": a.statement_class,
            "supporting": [f.factor for f in a.supporting], "contradicting": [f.factor for f in a.contradicting],
            "firstSeen": to_json(a.first_seen), "firstSeenBasis": a.first_seen_basis, "scenarioSeeds": a.scenario_seeds,
            "linkedCompanies": [{"ticker": e.ticker, "documented": e.documented} for e in a.linked_companies],
            "uncertainty": to_json(a.uncertainty)}


def _signal_out(s_: signals.Signal) -> dict:
    out = to_json(s_)
    out["evidence"] = [_prov_out(p) for p in s_.evidence]
    out["provenance"] = _prov_out(s_.provenance)
    if out.get("forecastRef"):
        out["forecastRef"] = {k: v for k, v in out["forecastRef"].items() if k in ("expectedLogReturn", "lower", "upper", "horizonDays", "knownCloses", "ticker")}
    return out


def _supply_out(e: domain.EffectiveSupply) -> dict:
    out = to_json(e)
    out["provenance"] = {**_prov_out(e.provenance), "inputKinds": list(e.provenance.input_kinds)}
    return out


def _exposure_out(e: domain.Exposure) -> dict:
    out = to_json(e)
    out["provenance"] = {**_prov_out(e.provenance), "inputKinds": list(e.provenance.input_kinds)}
    return out


def _grid_inputs(inp: dict) -> dict:
    return dict(
        offers=[_offer(o) for o in inp["offers"]], gpu_models={k: _gpu_model(v) for k, v in inp["gpuModels"].items()},
        regions={k: _region(v) for k, v in inp["regions"].items()}, series=_series_map(inp.get("series", {})),
        facilities=[_facility(f) for f in inp.get("facilities", [])], markets=[_market(m) for m in inp.get("markets", [])],
        feed=[_intel(i) for i in inp.get("feed", [])],
    )


def _index_out(ix: domain.FcpiIndex) -> dict:
    return {
        "id": ix.id, "gpuModelId": ix.gpu_model_id, "methodology": ix.methodology,
        "coverage": to_json(ix.coverage), "normalisation": ix.normalisation, "lastUpdated": to_json(ix.last_updated),
        "values": [[to_json(p.t), to_json(p.value)] for p in ix.values], "limitations": ix.limitations,
        "provenance": {"kind": ix.provenance.kind, "source": ix.provenance.source,
                       "inputKinds": list(ix.provenance.input_kinds), "observedAt": to_json(ix.provenance.observed_at)},
        "current": to_json(ix.current),
    }


def _aligned_out(a: xray.Aligned) -> dict:
    return {"seriesId": a.series_id, "unit": a.unit, "points": [[to_json(p.t), to_json(p.value)] for p in a.points],
            "normalised": to_json(a.normalised)}


# -- the one runner both the exporter and the test use ---------------------------------

def run_case(engine: str, inp: dict, data=None) -> dict | list:
    """Runs one fixture input through the named engine; returns the JSON-native output."""
    try:
        if engine == "adjust":
            result = adjust.adjust_offer(
                _offer(inp["offer"]), _gpu_model(inp["gpuModel"]),
                _region(inp["region"]) if inp.get("region") else None,
                reference_memory_gb=inp.get("referenceMemoryGb"),
            )
            return to_json(result)
        if engine == "fcpi":
            ix = compute_index(
                [_offer(o) for o in inp["offers"]], _gpu_model(inp["gpuModel"]),
                {k: _region(v) for k, v in inp["regions"].items()},
                history=_series(inp["history"]) if inp.get("history") else None,
                as_of=_dt(inp["asOf"]) if inp.get("asOf") else None,
            )
            return _index_out(ix)
        if engine == "changes":
            kwargs = {"price_threshold": inp["priceThreshold"]} if "priceThreshold" in inp else {}
            found = changes.detect_changes([_offer(o) for o in inp["before"]], [_offer(o) for o in inp["after"]], **kwargs)
            # Each change's snapshot provenance in the compact shape a port compares (kind, source, observedAt):
            return [{**to_json(replace(c, provenance=None)), "provenance": _prov_out(c.provenance) if c.provenance else None} for c in found]
        if engine == "forecast":
            result = forecast.forecast(inp["y"], inp["horizon"], level=inp.get("level", 0.8), season=inp.get("season", 7))
            return to_json(result)
        if engine == "scenario":
            result = scenario.project(
                inp["seriesId"], [(_dt(t), v) for t, v in inp["history"]], inp["assumptions"], inp["horizon"],
                step=timedelta(days=inp.get("stepDays", 1)),
            )
            return to_json(result)
        if engine == "procure":
            w = inp["workload"]
            workload = domain.Workload(
                gpu_model_ids=w["gpuModelIds"], duration_hours=w["durationHours"],
                interruption_tolerance=w.get("interruptionTolerance", "none"), gpu_count=w.get("gpuCount"),
                gpu_hours=w.get("gpuHours"), deadline=_dt(w["deadline"]) if w.get("deadline") else None,
                region_ids=w.get("regionIds", []), budget_usd=w.get("budgetUsd"), performance_needs=w.get("performanceNeeds", []),
            )
            result = procurement.plan(
                workload, [_offer(o) for o in inp["offers"]],
                {k: _gpu_model(v) for k, v in inp["gpuModels"].items()}, {k: _region(v) for k, v in inp["regions"].items()},
                now=_dt(inp["now"]), limit=inp.get("limit", 10),
            )
            return to_json(result)
        if engine == "ask":
            data = data or load_sample_uncached()
            parsed = ask.parse(inp["query"], data)
            out = to_json(parsed)
            out["explanation"] = parsed.explanation  # already camelCase keys and JSON-native
            return out
        if engine == "xray":
            result = xray.xray(_series(inp["target"]), [_series(o) for o in inp.get("overlays", [])], [_intel(i) for i in inp.get("feed", [])])
            return {
                "target": _aligned_out(result.target), "overlays": [_aligned_out(o) for o in result.overlays],
                "events": to_json(result.events), "correlations": to_json(result.correlations), "caveats": result.caveats,
            }
        if engine == "pressure":
            kwargs = _grid_inputs(inp)
            as_of = _dt(inp["asOf"])
            if inp.get("mode") == "grid":
                cells = pressure.compute_grid(as_of=as_of, **kwargs)
                return [_cell_out(c, full=False) for c in cells]
            offers = kwargs.pop("offers")
            cell = pressure.compute_cell(inp["regionId"], inp["gpuModelId"], offers=offers, as_of=as_of, **kwargs)
            if inp.get("snapshots"):
                snaps = [(_dt(t), [_offer(o) for o in rows]) for t, rows in inp["snapshots"]]
                history, basis = pressure.cell_history(inp["regionId"], inp["gpuModelId"], snaps, **kwargs)
                cell = pressure.with_history(cell, history, basis)
            return _cell_out(cell)
        if engine == "effective_supply":
            facilities = [_facility(f) for f in inp["facilities"]]
            overrides = {k: (v, "fixture") for k, v in inp.get("overrides", {}).items()}
            result = effective_supply.effective_supply(inp["regionId"], facilities, _dt(inp["asOf"]),
                                                       utilisation=inp.get("utilisation", effective_supply.DEFAULT_UTILISATION), overrides=overrides)
            return _supply_out(result)
        if engine == "exposure":
            rows = exposure.trace(inp["regionId"], inp.get("gpuModelId"), companies={c["ticker"]: _company(c) for c in inp["companies"]},
                                  facilities=[_facility(f) for f in inp["facilities"]], regions={k: _region(v) for k, v in inp["regions"].items()},
                                  filings=[_intel(i) for i in inp.get("filings", [])])
            return [_exposure_out(e) for e in rows]
        if engine == "anomalies":
            kwargs = _grid_inputs(inp)
            kwargs.pop("markets")
            kwargs.pop("feed")
            cells = [_cell_from(c) for c in inp["cells"]]
            exposures = {rid: [_exposure_from(e) for e in rows] for rid, rows in inp.get("exposuresByRegion", {}).items()}
            detected = {k: _dt(v) for k, v in inp.get("firstDetected", {}).items()}
            return [_anomaly_out(a) for a in anomalies.detect(cells, exposures_by_region=exposures, first_detected=detected, **kwargs)]
        if engine == "infra_scenario":
            kwargs = _grid_inputs(inp)
            result = scenario.project_infra(inp["regionId"], inp["gpuModelId"], inp["horizonDays"], inp["assumptions"],
                                            companies={c["ticker"]: _company(c) for c in inp["companies"]}, as_of=_dt(inp["asOf"]), **kwargs)
            out = to_json(result)
            out["baselineSupply"] = _supply_out(result.baseline_supply)
            out["scenarioSupply"] = _supply_out(result.scenario_supply)
            out["path"] = [to_json(result.path[0]), to_json(result.path[-1]), {"steps": len(result.path)}]
            return out
        if engine == "evaluation":
            closes = _series({"id": f"equity:{inp['ticker']}:close", **inp["closes"]})
            result = evaluation.evaluate_ticker(
                inp["ticker"], closes,
                fcpi=_series({"id": "fcpi", **inp["fcpi"]}) if inp.get("fcpi") else None,
                pressure_scores=_series({"id": "pressure", **inp["pressure"]}) if inp.get("pressure") else None,
                energy=_series({"id": "energy", **inp["energy"]}) if inp.get("energy") else None,
                feed=[_intel(i) for i in inp.get("feed", [])], horizons=tuple(inp.get("horizons", evaluation.HORIZONS)),
                price_feature=inp.get("priceFeature", evaluation.FCPI_FEATURE),
            )
            out = to_json(result)
            out.pop("provenance", None)
            for h in out["horizons"]:
                for m in h["models"]:
                    m["realisedVsPredicted"] = m["realisedVsPredicted"][-3:]
            return out
        if engine == "evaluation_terminal":
            return _evaluation_composition(inp["tickers"])
        if engine == "signals":
            series = _series_map(inp.get("series", {}))
            if "anomaly" in inp:
                kwargs = _grid_inputs(inp["anomaly"])
                kwargs.pop("markets")
                kwargs.pop("feed")
                cells = [_cell_from(c) for c in inp["anomaly"]["cells"]]
                exposures = {rid: [_exposure_from(e) for e in rows] for rid, rows in inp["anomaly"].get("exposuresByRegion", {}).items()}
                found = anomalies.detect(cells, exposures_by_region=exposures, **kwargs)
                return [_signal_out(signals.from_anomaly(a, series)) for a in found]
            rows = [SimpleNamespace(**{_snake(k): v for k, v in d.items()}) for d in inp["decisions"]]
            for r in rows:
                r.t = _dt(r.t)
            orders = {o["id"]: SimpleNamespace(**{_snake(k): v for k, v in o.items()}, filled_at=None) for o in inp.get("orders", [])}
            from product.flopx import strategies
            return [_signal_out(s_) for s_ in signals.from_decisions(rows, series, orders, strategies=strategies.STRATEGIES)]
        if engine == "assistant":
            kwargs = _grid_inputs(inp)
            as_of = _dt(inp["asOf"])
            rid = inp["regionId"]
            cells = pressure.compute_grid(as_of=as_of, region_ids=[rid], **kwargs)
            companies = {c["ticker"]: _company(c) for c in inp["companies"]}
            exposures = {rid: exposure.trace(rid, None, companies=companies, facilities=kwargs["facilities"], regions=kwargs["regions"])}
            found = anomalies.detect(cells, offers=kwargs["offers"], gpu_models=kwargs["gpu_models"], regions=kwargs["regions"], facilities=kwargs["facilities"],
                                     series=kwargs["series"], exposures_by_region=exposures)
            supply = effective_supply.effective_supply(rid, kwargs["facilities"], as_of)
            latest = [(label, kwargs["series"][sid].points[-1]) for label, sid in inp.get("latest", []) if sid in kwargs["series"] and kwargs["series"][sid].points]
            brief = assistant.compose(target_kind="region", target_id=rid, target_name=inp.get("targetName", rid), cells=cells, anomalies=found, supply=supply,
                                      feed=kwargs["feed"][: assistant.MAX_FEED], markets=kwargs["markets"], latest=latest, evaluation=inp.get("evaluation", []),
                                      exposures=exposures[rid])
            out = to_json(brief)
            for ref in out["evidence"]:
                ref.pop("observedAt", None)
                if ref.get("provenance"):
                    # The envelope's class and what it rests on; its times and optional fields are the inputs' own.
                    ref["provenance"] = {k: ref["provenance"][k] for k in ("kind", "source") if k in ref["provenance"]}
            return out
    except FlopxDomainError as exc:
        return {"error": exc.code, "details": to_json(exc.details)}
    except ValueError as exc:
        return {"error": "value-error", "message": str(exc)}
    raise KeyError(engine)


# -- case construction from the sample data -----------------------------------------------

def _evaluation_composition(tickers: list[str]) -> dict:
    """The route's evaluation composition (terminal.py `_evaluate_ticker_uncached`) over the sample bag with no
    live source answering: the closes, the feature series and their fallbacks, the sample feed. The route-handler
    tier (engine/terminal.ts `evaluation(sampleTerminalData())`) must produce this payload exactly, so a page shows
    one verdict per (ticker, horizon) whichever tier answers. Live adapters are switched off for the call, so a
    source cached earlier in the same process cannot leak in."""
    from product.flopx import terminal

    saved = terminal._gather
    terminal._gather = lambda calls: []
    try:
        rows = []
        for ticker in tickers:
            out = to_json(terminal._evaluate_ticker_uncached(None, ticker)["evaluation"])
            out.pop("provenance", None)
            rows.append(out)
    finally:
        terminal._gather = saved
    return {"evaluations": rows}


def _compact_offer(o: domain.GpuOffer) -> dict:
    """An offer as fixture input: no None fields, the provenance cut to what the engines read."""
    row = {k: v for k, v in to_json(o).items() if v is not None and k != "provenance"}
    row["provenance"] = {"kind": o.provenance.kind, "source": o.provenance.source, "observedAt": to_json(o.provenance.observed_at)}
    return row


def _sample_inputs(data):
    offers = [_compact_offer(o) for o in data.offers]
    previous = [_compact_offer(o) for o in data.offers_previous]
    models = {gid: to_json(m) for gid, m in data.gpu_models.items()}
    regions = {rid: to_json(r) for rid, r in data.regions.items()}
    return offers, previous, models, regions


def _series_json(s: domain.Series, kind: str = "sample", last: int | None = None) -> dict:
    points = s.points[-last:] if last else s.points
    return {"id": s.id, "unit": s.unit, "kind": kind, "points": [[to_json(p.t), to_json(p.value)] for p in points]}


def build_cases(data) -> dict[str, list[dict]]:
    offers, previous, models, regions = _sample_inputs(data)
    h100 = models["h100-sxm-80gb"]
    h200 = models["h200-sxm-141gb"]
    by_model = {gid: [o for o in offers if o["gpuModelId"] == gid] for gid in models}
    live = lambda src: {"kind": "live", "source": src, "observedAt": T0}  # noqa: E731

    def mk_offer(i, price, *, contract="on-demand", region="us-va", model="h100-sxm-80gb", prov=None, **extra):
        return {"id": f"o{i}", "providerId": extra.pop("provider", "p"), "regionId": region, "gpuModelId": model,
                "gpuCount": 8, "pricePerGpuHourUsd": price, "availability": "available",
                "interruptible": contract == "spot", "contract": contract, "provenance": prov, **extra}

    cases: dict[str, list[dict]] = {e: [] for e in ENGINES}

    # adjust: every sample offer, plus the hand-chosen edges
    seen: set[tuple] = set()
    for o in offers:
        key = (o["gpuModelId"], o["contract"], regions[o["regionId"]]["countryCode"])
        if key in seen:
            continue
        seen.add(key)
        cases["adjust"].append({"name": f"sample-{o['id']}", "input": {"offer": o, "gpuModel": models[o["gpuModelId"]], "region": regions[o["regionId"]]}})
    cases["adjust"] += [
        {"name": "edge-no-region-defaults-to-us", "input": {"offer": mk_offer(1, 2.5), "gpuModel": h100, "region": None}},
        {"name": "edge-spot-in-germany", "input": {"offer": mk_offer(2, 1.0, contract="spot", region="eu-de"), "gpuModel": h100, "region": regions["eu-de"]}},
        {"name": "edge-reserved-in-norway", "input": {"offer": mk_offer(3, 2.0, contract="reserved", region="eu-no"), "gpuModel": h100, "region": regions["eu-no"]}},
        {"name": "edge-memory-variant-to-80gb", "input": {"offer": mk_offer(4, 4.0, model="h200-sxm-141gb"), "gpuModel": h200, "region": regions["us-va"], "referenceMemoryGb": 80}},
        {"name": "edge-unknown-country-factor-one", "input": {"offer": mk_offer(5, 3.0), "gpuModel": h100, "region": {"id": "xx", "name": "Nowhere", "countryCode": "XX", "lat": 0, "lon": 0}}},
    ]

    # fcpi: each model over the sample offers (monthly history keeps the file small), plus edges
    for gid, m in models.items():
        hist = data.series[f"gpu:{gid}:on-demand:global:monthly"]
        cases["fcpi"].append({"name": f"sample-{gid}", "input": {"offers": by_model[gid], "gpuModel": m, "regions": regions, "history": _series_json(hist, last=12)}})
    cases["fcpi"] += [
        {"name": "edge-empty-offer-set", "input": {"offers": [], "gpuModel": h100, "regions": regions, "asOf": T0}},
        {"name": "edge-single-offer", "input": {"offers": [mk_offer(1, 3.1)], "gpuModel": h100, "regions": regions}},
        {"name": "edge-live-offers-stay-derived", "input": {"offers": [mk_offer(i, 3.0 + i / 10, prov=live("runpod"), provider=f"p{i}") for i in range(1, 7)], "gpuModel": h100, "regions": regions}},
        {"name": "edge-mixed-live-and-sample-refused", "input": {"offers": [mk_offer(1, 3.0), mk_offer(2, 3.2, prov=live("vast-ai"))], "gpuModel": h100, "regions": regions}},
        {"name": "edge-spot-grossed-up-before-median", "input": {"offers": [mk_offer(1, 3.0), mk_offer(2, 3.0), mk_offer(3, 3.0), mk_offer(4, 1.0, contract="spot")], "gpuModel": h100, "regions": regions}},
        {"name": "edge-flat-history-rescaled", "input": {"offers": [mk_offer(1, 2.0), mk_offer(2, 2.0), mk_offer(3, 2.0)], "gpuModel": h100, "regions": regions,
                                                        "history": {"id": "gpu:h100-sxm-80gb:on-demand:global", "unit": "USD/GPU-hour", "kind": "sample", "points": [[f"2026-10-0{d}T00:00:00Z", 4.0] for d in range(1, 6)]}}},
    ]

    # changes: the authored previous/current snapshots, plus edges
    cases["changes"] += [
        {"name": "sample-previous-to-current", "input": {"before": previous, "after": offers}},
        {"name": "edge-both-empty", "input": {"before": [], "after": []}},
        {"name": "edge-everything-new", "input": {"before": [], "after": offers[:3]}},
        {"name": "edge-everything-removed", "input": {"before": offers[:3], "after": []}},
        {"name": "edge-move-below-threshold-ignored", "input": {"before": [mk_offer(1, 3.00)], "after": [mk_offer(1, 3.03)]}},
        {"name": "edge-availability-flip-and-price-drop", "input": {"before": [mk_offer(1, 3.00, availability="available")], "after": [mk_offer(1, 2.40, availability="limited")]}},
        {"name": "edge-custom-threshold", "input": {"before": [mk_offer(1, 3.00)], "after": [mk_offer(1, 3.03)], "priceThreshold": 0.005}},
    ]

    # forecast: sample series at two horizons, plus edges
    daily = [p.value for p in data.series["gpu:h100-sxm-80gb:on-demand:global"].points]
    monthly = [p.value for p in data.series["gpu:h100-sxm-80gb:on-demand:global:monthly"].points]
    nvda = [p.value for p in data.series["equity:NVDA:close"].points]
    cases["forecast"] += [
        {"name": "sample-h100-daily-h14", "input": {"y": daily, "horizon": 14, "level": 0.8, "season": 7}},
        {"name": "sample-h100-daily-h30-level95", "input": {"y": daily[-120:], "horizon": 30, "level": 0.95, "season": 7}},
        {"name": "sample-h100-monthly-h6", "input": {"y": monthly, "horizon": 6, "level": 0.8, "season": 12}},
        {"name": "sample-nvda-h7", "input": {"y": nvda[-90:], "horizon": 7, "level": 0.8, "season": 5}},
        {"name": "edge-flat-series", "input": {"y": [3.0] * 40, "horizon": 5, "level": 0.8, "season": 7}},
        {"name": "edge-short-history-drops-seasonal", "input": {"y": [1.0, 1.1, 1.2, 1.1, 1.3, 1.2, 1.4, 1.3, 1.5, 1.4], "horizon": 2, "level": 0.8, "season": 7}},
        {"name": "edge-refusal-history-too-short", "input": {"y": monthly, "horizon": 14, "level": 0.8, "season": 12}},
        {"name": "edge-refusal-horizon-zero", "input": {"y": daily, "horizon": 0, "level": 0.8, "season": 7}},
        {"name": "edge-linear-trend", "input": {"y": [float(i) for i in range(1, 31)], "horizon": 5, "level": 0.8, "season": 7}},
    ]

    # scenario
    h100_hist = [[to_json(p.t), to_json(p.value)] for p in data.series["gpu:h100-sxm-80gb:on-demand:global"].points[-60:]]
    nvda_hist = [[to_json(p.t), to_json(p.value)] for p in data.series["equity:NVDA:close"].points[-60:]]
    ercot_hist = [[to_json(p.t), to_json(p.value)] for p in data.series["energy:ERCOT:day-ahead"].points[-60:]]
    cases["scenario"] += [
        {"name": "sample-h100-supply-shock", "input": {"seriesId": "gpu:h100-sxm-80gb:on-demand:global", "history": h100_hist, "assumptions": {"supplyGrowthPct": 20, "demandGrowthPct": 5}, "horizon": 30, "stepDays": 1}},
        {"name": "sample-nvda-earnings-surprise", "input": {"seriesId": "equity:NVDA:close", "history": nvda_hist, "assumptions": {"earningsSurprisePct": 10, "regulationTightnessPct": 15}, "horizon": 20, "stepDays": 1}},
        {"name": "sample-ercot-demand", "input": {"seriesId": "energy:ERCOT:day-ahead", "history": ercot_hist, "assumptions": {"demandGrowthPct": 10, "earningsSurprisePct": 5}, "horizon": 10, "stepDays": 1}},
        {"name": "edge-no-assumptions", "input": {"seriesId": "gpu:h100-sxm-80gb:on-demand:global", "history": h100_hist[-10:], "assumptions": {}, "horizon": 3, "stepDays": 1}},
        {"name": "edge-flat-history", "input": {"seriesId": "gpu:h100-sxm-80gb:on-demand:global", "history": [[f"2026-10-{d:02d}T00:00:00Z", 2.5] for d in range(1, 11)], "assumptions": {"demandGrowthPct": 10}, "horizon": 4, "stepDays": 1}},
        {"name": "edge-too-short-history-refused", "input": {"seriesId": "gpu:h100-sxm-80gb:on-demand:global", "history": h100_hist[-2:], "assumptions": {"demandGrowthPct": 10}, "horizon": 4, "stepDays": 1}},
        {"name": "edge-weekly-step", "input": {"seriesId": "gpu:h100-sxm-80gb:on-demand:global", "history": h100_hist[-30:], "assumptions": {"energyPriceChangePct": -20}, "horizon": 4, "stepDays": 7}},
    ]

    # procure
    now = "2026-11-07T12:00:00Z"
    base = {"offers": offers, "gpuModels": models, "regions": regions, "now": now}

    def for_models(*gids):
        return {**base, "offers": [o for o in offers if o["gpuModelId"] in gids]}
    cases["procure"] += [
        {"name": "sample-64-h100-48h-budget", "input": {**for_models("h100-sxm-80gb"), "workload": {"gpuModelIds": ["h100-sxm-80gb"], "durationHours": 48, "gpuCount": 64, "budgetUsd": 20000, "interruptionTolerance": "none"}}},
        {"name": "sample-gpu-hours-spot-ok", "input": {**for_models("h100-sxm-80gb", "h200-sxm-141gb"), "workload": {"gpuModelIds": ["h100-sxm-80gb", "h200-sxm-141gb"], "durationHours": 24, "gpuHours": 192, "interruptionTolerance": "high", "performanceNeeds": ["fast-interconnect", "fp8"]}}},
        {"name": "sample-eu-only-deadline", "input": {**for_models("a100-sxm-80gb", "l40s-48gb"), "workload": {"gpuModelIds": ["a100-sxm-80gb", "l40s-48gb"], "durationHours": 72, "gpuCount": 8, "regionIds": ["eu-ie", "eu-de", "eu-no"], "deadline": "2026-11-09T12:00:00Z", "interruptionTolerance": "low", "performanceNeeds": ["high-memory"]}}},
        {"name": "sample-b200-limit-3", "input": {**for_models("b200-sxm-192gb"), "limit": 3, "workload": {"gpuModelIds": ["b200-sxm-192gb"], "durationHours": 10, "gpuCount": 8, "interruptionTolerance": "none"}}},
        {"name": "edge-empty-offer-set", "input": {**base, "offers": [], "workload": {"gpuModelIds": ["h100-sxm-80gb"], "durationHours": 10, "gpuCount": 1, "interruptionTolerance": "none"}}},
        {"name": "edge-single-offer-too-small", "input": {**base, "offers": [mk_offer(1, 3.0, gpuCount=4)], "workload": {"gpuModelIds": ["h100-sxm-80gb"], "durationHours": 10, "gpuCount": 8, "interruptionTolerance": "none"}}},
        {"name": "edge-single-offer-fits", "input": {**base, "offers": [mk_offer(1, 3.0, reliabilityScore=0.9, availability="limited")], "workload": {"gpuModelIds": ["h100-sxm-80gb"], "durationHours": 10, "gpuCount": 8, "interruptionTolerance": "none"}}},
        {"name": "edge-reserved-minimum-exceeds-duration", "input": {**base, "offers": [mk_offer(1, 2.0, contract="reserved", minDurationHours=720)], "workload": {"gpuModelIds": ["h100-sxm-80gb"], "durationHours": 10, "gpuCount": 8, "interruptionTolerance": "none"}}},
        {"name": "edge-missed-deadline-ranked-last", "input": {**base, "offers": [mk_offer(1, 2.0, contract="spot", provider="cheap"), mk_offer(2, 3.0, provider="dear")], "workload": {"gpuModelIds": ["h100-sxm-80gb"], "durationHours": 10, "gpuCount": 8, "interruptionTolerance": "low", "deadline": "2026-11-07T23:00:00Z"}}},
    ]

    # ask
    queries = [
        "cheapest H100 in Texas right now", "compare NVDA against the H100 index over the last 90 days", "buy 50 CRWV",
        "forecast H200 prices next 30 days", "which data centres are under construction in Norway",
        "I need 64 H100s for 48 hours, budget 20000", "hello there", "show the latest news about CoreWeave",
        "sell 10 shares of nvidia", "close IREN", "what are the odds of a Texas moratorium", "H100 index ytd",
        "price changes this week", "show my portfolio", "revenue and capex for Nebius", "B200 supply in Europe",
        "what if energy prices rise 20% for the h100", "divergence strategy backtest", "", "???!!!", "nvda vs crwv vs iren",
        "rent 8 GPUs for 12h under $500",
    ]
    cases["ask"] += [{"name": f"q{i:02d}-{q[:40].strip() or 'empty'}", "input": {"query": q}} for i, q in enumerate(queries)]

    # xray
    feed = [to_json({"id": i.id, "kind": i.kind, "headline": i.headline, "severity": i.severity, "observedAt": i.observed_at,
                     "entities": i.entities}) for i in data.feed
            if {"NVDA", "CRWV"} & set(i.entities.tickers) or "h100-sxm-80gb" in i.entities.gpu_model_ids]
    h100_s = data.series["gpu:h100-sxm-80gb:on-demand:global"]
    nvda_s = data.series["equity:NVDA:close"]
    crwv_s = data.series["equity:CRWV:close"]
    ercot_s = data.series["energy:ERCOT:day-ahead"]
    cases["xray"] += [
        {"name": "sample-nvda-vs-h100-60d", "input": {"target": _series_json(nvda_s, last=60), "overlays": [_series_json(h100_s, last=60)], "feed": feed}},
        {"name": "sample-h100-vs-nvda-crwv-ercot-45d", "input": {"target": _series_json(h100_s, last=45), "overlays": [_series_json(nvda_s, last=45), _series_json(crwv_s, last=45), _series_json(ercot_s, last=45)], "feed": feed}},
        {"name": "edge-no-overlay", "input": {"target": _series_json(nvda_s, last=30), "overlays": [], "feed": feed}},
        {"name": "edge-no-overlap", "input": {"target": _series_json(nvda_s, last=10), "overlays": [{"id": "gpu:h100-sxm-80gb:on-demand:global", "unit": "USD/GPU-hour", "kind": "sample", "points": [["2020-01-01T00:00:00Z", 1.0], ["2020-01-02T00:00:00Z", 1.1], ["2020-01-03T00:00:00Z", 1.2]]}], "feed": []}},
        {"name": "edge-flat-overlay-no-correlation", "input": {"target": _series_json(nvda_s, last=20), "overlays": [{"id": "gpu:h100-sxm-80gb:on-demand:global", "unit": "USD/GPU-hour", "kind": "sample", "points": [[to_json(p.t), 3.0] for p in nvda_s.points[-20:]]}], "feed": []}},
        {"name": "edge-live-points-no-sample-caveat", "input": {"target": _series_json(nvda_s, kind="live", last=20), "overlays": [_series_json(crwv_s, kind="live", last=20)], "feed": []}},
        {"name": "edge-single-point", "input": {"target": _series_json(nvda_s, last=1), "overlays": [_series_json(crwv_s, last=1)], "feed": []}},
    ]
    cases.update(build_inferred_state_cases(data, offers, previous, models, regions, mk_offer))
    return cases


def _compact_facility(f: domain.Facility) -> dict:
    row = {"id": f.id, "name": f.name, "regionId": f.region_id, "status": f.status}
    for key, value in (("ownerCompanyIds", f.owner_company_ids), ("operator", f.operator), ("capacityMw", f.capacity_mw),
                       ("gpuCountEstimate", f.gpu_count_estimate)):
        if value not in (None, []):
            row[key] = value
    timeline = {k: v for k, v in to_json(f.timeline).items() if v}
    if timeline:
        row["timeline"] = timeline
    return row


def _compact_market(m: domain.PredictionMarket) -> dict:
    return {"id": m.id, "question": m.question, "yes": m.outcomes[0].implied_probability, "lastUpdated": to_json(m.last_updated),
            "history": [[to_json(p.t), to_json(p.value)] for p in m.history[-6:]]}


def _compact_intel(i: domain.IntelItem) -> dict:
    return {"id": i.id, "kind": i.kind, "headline": i.headline, "severity": i.severity, "observedAt": to_json(i.observed_at),
            "classification": i.classification, "geography": {"regionIds": i.geography.region_ids},
            "entities": {"tickers": i.entities.tickers, "gpuModelIds": i.entities.gpu_model_ids}}


def _series_body(s: domain.Series, last: int) -> dict:
    return {"unit": s.unit, "kind": "sample", "points": [[to_json(p.t), to_json(p.value)] for p in s.points[-last:]]}


def build_inferred_state_cases(data, offers, previous, models, regions, mk_offer) -> dict[str, list[dict]]:
    as_of = T0
    previous_at = "2026-10-30T21:00:00Z"
    cases: dict[str, list[dict]] = {"pressure": [], "effective_supply": [], "exposure": [], "anomalies": []}
    facilities = [_compact_facility(f) for f in data.facilities]
    markets = [_compact_market(m) for m in data.markets]
    feed = [_compact_intel(i) for i in data.feed]
    gpu_series = {f"gpu:{gid}:on-demand:global": _series_body(data.series[f"gpu:{gid}:on-demand:global"], 36) for gid in models}
    energy = {"energy:ERCOT:day-ahead": _series_body(data.series["energy:ERCOT:day-ahead"], 36)}
    closes = {f"equity:{t}:close": _series_body(data.series[f"equity:{t}:close"], 45) for t in data.companies}
    grid_input = {"mode": "grid", "asOf": as_of, "offers": offers, "gpuModels": models, "regions": regions,
                  "series": {**gpu_series, **energy}, "facilities": facilities, "markets": markets, "feed": feed}
    cases["pressure"].append({"name": "sample-grid-all-cells", "input": grid_input})

    def cell_case(name, rid, gid, *, snapshots=True, **extra):
        rows = [o for o in offers if o["gpuModelId"] == gid]
        prev_rows = [o for o in previous if o["gpuModelId"] == gid]
        inp = {"regionId": rid, "gpuModelId": gid, "asOf": as_of, "offers": rows, "gpuModels": {gid: models[gid]}, "regions": regions,
               "series": {f"gpu:{gid}:on-demand:global": gpu_series[f"gpu:{gid}:on-demand:global"], **energy},
               "facilities": [f for f in facilities if f["regionId"] == rid],
               "markets": [m for m in markets if any(w in m["question"].lower() for w in pressure.GPU_WORDS[gid])],
               "feed": [i for i in feed if rid in i["geography"]["regionIds"] or gid in i["entities"]["gpuModelIds"]]}
        if snapshots:
            inp["snapshots"] = [[previous_at, prev_rows], [as_of, rows]]
        inp.update(extra)
        return {"name": name, "input": inp}

    cases["pressure"] += [
        cell_case("sample-us-tx-h100-with-history", "us-tx", "h100-sxm-80gb"),
        cell_case("sample-us-va-h100-highest-cell", "us-va", "h100-sxm-80gb"),
        cell_case("sample-ap-sg-l40s-four-asks", "ap-sg", "l40s-48gb"),
        cell_case("sample-ap-jp-rtx4090-slack", "ap-jp", "rtx-4090-24gb"),
    ]
    h100 = models["h100-sxm-80gb"]
    base_regions = {k: regions[k] for k in ("us-tx", "us-va")}
    cases["pressure"] += [
        {"name": "edge-empty-cell-coverage-zero", "input": {"regionId": "us-va", "gpuModelId": "h100-sxm-80gb", "asOf": as_of,
                                                           "offers": [mk_offer(1, 3.0, region="us-tx")], "gpuModels": {"h100-sxm-80gb": h100},
                                                           "regions": base_regions, "series": {}, "facilities": [], "markets": [], "feed": []}},
        {"name": "edge-all-unavailable-premium-cell", "input": {"regionId": "us-tx", "gpuModelId": "h100-sxm-80gb", "asOf": as_of,
                                                               "offers": [mk_offer(1, 4.0, region="us-tx", availability="unavailable"), mk_offer(2, 4.2, region="us-tx", availability="unavailable"),
                                                                          mk_offer(3, 4.1, region="us-tx", availability="unavailable"), mk_offer(4, 2.0, region="us-va"), mk_offer(5, 2.2, region="us-va")],
                                                               "gpuModels": {"h100-sxm-80gb": h100}, "regions": base_regions, "series": {}, "facilities": [], "markets": [], "feed": []}},
        {"name": "edge-regional-series-preferred-over-global", "input": {"regionId": "us-tx", "gpuModelId": "h100-sxm-80gb", "asOf": as_of,
                                                                        "offers": [mk_offer(1, 3.0, region="us-tx")], "gpuModels": {"h100-sxm-80gb": h100}, "regions": base_regions,
                                                                        "series": {"gpu:h100-sxm-80gb:on-demand:us-tx": {"unit": "USD/GPU-hour", "kind": "sample", "points": [[f"2026-{m:02d}-01T00:00:00Z", v] for m, v in ((9, 2.0), (10, 2.5), (11, 3.0))]},
                                                                                   **gpu_series}, "facilities": [], "markets": [], "feed": []}},
        {"name": "edge-live-inputs-stay-derived", "input": {"regionId": "us-tx", "gpuModelId": "h100-sxm-80gb", "asOf": as_of,
                                                           "offers": [mk_offer(i, 2.5 + i / 10, region="us-tx", prov={"kind": "live", "source": "runpod", "observedAt": as_of}) for i in range(1, 4)]
                                                                     + [mk_offer(9, 2.9, region="us-va", prov={"kind": "live", "source": "vast-ai", "observedAt": as_of})],
                                                           "gpuModels": {"h100-sxm-80gb": h100}, "regions": base_regions,
                                                           "series": {"index:fcpi:h100-sxm-80gb": {"unit": "USD/GPU-hour", "kind": "archived", "source": "fcpi-v1:median-of-adjusted-asks",
                                                                                                   "points": [[f"2026-10-{d:02d}T00:00:00Z", 2.4 + d / 100] for d in range(1, 31)] + [[as_of, 2.9]]}},
                                                           "facilities": [], "markets": [], "feed": []}},
        {"name": "edge-pipeline-over-one-class", "input": {"regionId": "us-tx", "gpuModelId": "h100-sxm-80gb", "asOf": as_of,
                                                          "offers": [mk_offer(1, 3.0, region="us-tx")], "gpuModels": {"h100-sxm-80gb": h100}, "regions": base_regions, "series": {},
                                                          "facilities": [{"id": "s-op", "regionId": "us-tx", "status": "operating", "capacityMw": 200},
                                                                         {"id": "s-uc", "regionId": "us-tx", "status": "under-construction", "capacityMw": 100},
                                                                         {"id": "Q1", "regionId": "us-tx", "status": "announced", "capacityMw": 9000,
                                                                          "provenance": {"kind": "live", "source": "wikidata", "observedAt": as_of}}],
                                                          "markets": [], "feed": []}},
        {"name": "edge-price-down-market-inverts", "input": {"regionId": "us-tx", "gpuModelId": "h200-sxm-141gb", "asOf": as_of,
                                                            "offers": [mk_offer(1, 3.3, region="us-tx", model="h200-sxm-141gb")], "gpuModels": {"h200-sxm-141gb": models["h200-sxm-141gb"]},
                                                            "regions": base_regions, "series": {}, "facilities": [],
                                                            "markets": [{"id": "m-down", "question": "H200 on-demand median price below $3.50/GPU-hour at end of December?", "yes": 0.8, "lastUpdated": as_of},
                                                                        {"id": "m-unrelated", "question": "Texas announces a moratorium?", "yes": 0.3, "lastUpdated": as_of}],
                                                            "feed": []}},
    ]

    # effective supply: every region over the sample facilities, plus edges
    for rid in sorted(regions):
        cases["effective_supply"].append({"name": f"sample-{rid}", "input": {"regionId": rid, "asOf": as_of, "facilities": [f for f in facilities if f["regionId"] == rid]}})
    late = {"id": "late", "regionId": "us-tx", "status": "under-construction", "capacityMw": 100, "gpuCountEstimate": 80000, "timeline": {"expectedOnline": "2025-11-06"}}
    undated = {"id": "undated", "regionId": "us-tx", "status": "under-construction", "capacityMw": 100}
    op = {"id": "op", "regionId": "us-tx", "status": "operating", "capacityMw": 200}
    cases["effective_supply"] += [
        {"name": "edge-no-facilities", "input": {"regionId": "nowhere", "asOf": as_of, "facilities": []}},
        {"name": "edge-twelve-months-late-halves-probability", "input": {"regionId": "us-tx", "asOf": as_of, "facilities": [late]}},
        {"name": "edge-no-date-gets-quarter", "input": {"regionId": "us-tx", "asOf": as_of, "facilities": [undated, op]}},
        {"name": "edge-utilisation-at-range-top", "input": {"regionId": "us-tx", "asOf": as_of, "facilities": [op], "utilisation": 0.9}},
        {"name": "edge-utilisation-outside-range-refused", "input": {"regionId": "us-tx", "asOf": as_of, "facilities": [op], "utilisation": 0.95}},
        {"name": "edge-override-completion", "input": {"regionId": "us-tx", "asOf": as_of, "facilities": [late, op], "overrides": {"late": 1.0}}},
        {"name": "edge-announced-and-reported-never-counted", "input": {"regionId": "us-tx", "asOf": as_of, "facilities": [
            {"id": "a", "regionId": "us-tx", "status": "announced", "capacityMw": 1000}, {"id": "r", "regionId": "us-tx", "status": "reported", "capacityMw": 500},
            {"id": "p", "regionId": "us-tx", "status": "planned", "capacityMw": 300}]}},
    ]
    # One provenance class per region (falsifier V1 F1): sample wins where the region has any; live only elsewhere.
    wiki = {"kind": "live", "source": "wikidata", "observedAt": as_of}
    live_op = {"id": "Q1", "regionId": "us-tx", "status": "operating", "capacityMw": 5000, "provenance": wiki}
    live_ann = {"id": "Q2", "regionId": "us-tx", "status": "announced", "capacityMw": 7000, "provenance": wiki}
    cases["effective_supply"] += [
        {"name": "edge-mixed-classes-sample-wins", "input": {"regionId": "us-tx", "asOf": as_of, "facilities": [late, op, live_op, live_ann]}},
        {"name": "edge-live-only-region-uses-live", "input": {"regionId": "us-tx", "asOf": as_of, "facilities": [live_op, live_ann]}},
    ]

    # exposure: every region, plus edges
    companies = [{"ticker": c.ticker, "name": c.name, "tags": c.compute_exposure.tags} for c in data.companies.values()]
    for rid in sorted(regions):
        cases["exposure"].append({"name": f"sample-{rid}", "input": {"regionId": rid, "companies": companies, "facilities": [f for f in facilities if f["regionId"] == rid], "regions": regions}})
    cases["exposure"] += [
        {"name": "edge-unknown-region-profile-only", "input": {"regionId": "xx", "companies": companies, "facilities": [], "regions": regions}},
        {"name": "edge-operator-name-match", "input": {"regionId": "us-tx", "companies": [{"ticker": "VRT", "name": "Vertiv", "tags": []}],
                                                      "facilities": [{"id": "f1", "name": "Site", "regionId": "us-tx", "status": "operating", "operator": "vertiv"}], "regions": regions}},
        {"name": "edge-filing-mention-documents", "input": {"regionId": "us-tx", "companies": [{"ticker": "SMCI", "name": "Super Micro", "tags": []}], "facilities": [], "regions": regions,
                                                           "filings": [{"id": "sec:1", "kind": "announcement", "headline": "10-Q: new Texas assembly capacity", "severity": 2, "observedAt": as_of,
                                                                        "classification": "verified", "entities": {"tickers": ["SMCI"]}, "provenance": {"kind": "live", "source": "sec-edgar", "observedAt": as_of}}]}},
        {"name": "edge-non-sec-mention-does-not-document", "input": {"regionId": "us-tx", "companies": [{"ticker": "SMCI", "name": "Super Micro", "tags": []}], "facilities": [], "regions": regions,
                                                                    "filings": [{"id": "hn:1", "kind": "announcement", "headline": "Texas assembly capacity", "severity": 2, "observedAt": as_of,
                                                                                 "classification": "interpretation", "entities": {"tickers": ["SMCI"]}, "provenance": {"kind": "live", "source": "hacker-news", "observedAt": as_of}}]}},
    ]

    # anomalies: the whole sample grid (cells computed here, with the two-snapshot history) plus edges
    grid_kwargs = _grid_inputs(grid_input)
    grid_cells = pressure.compute_grid(as_of=_dt(as_of), **grid_kwargs)
    snaps = [(_dt(previous_at), [_offer(o) for o in previous]), (_dt(as_of), [_offer(o) for o in offers])]
    hist_kwargs = {k: v for k, v in grid_kwargs.items() if k != "offers"}
    with_hist = [pressure.with_history(c, *pressure.cell_history(c.region_id, c.gpu_model_id, snaps, **hist_kwargs)) for c in grid_cells]
    exposures = {rid: [{"ticker": e.ticker, "documented": e.documented, "basis": e.basis}
                       for e in exposure.trace(rid, None, companies=data.companies, facilities=data.facilities, regions=data.regions)] for rid in regions}
    cases["anomalies"].append({"name": "sample-grid", "input": {"cells": [_cell_in(c) for c in with_hist if c.uncertainty.coverage], "offers": offers, "gpuModels": models,
                                                                 "regions": regions, "facilities": facilities, "series": closes, "exposuresByRegion": exposures}})

    def synthetic_cell(score, *, availability=0.0, pipeline=0.0, premium=0.0, momentum=0.0, contradictions=(), coverage=4, history=()):
        factors = [("price-premium", premium, pressure.clamp(premium / 0.3), 0.3), ("availability", availability, 2 * availability - 1, 0.2),
                   ("price-momentum", momentum, pressure.clamp(momentum / 0.15), 0.15), ("power-stress", None, None, 0.15),
                   ("capacity-pipeline", pipeline, -pressure.clamp(pipeline / 2.0), 0.1), ("demand-signals", None, None, 0.1)]
        return {"regionId": "us-tx", "gpuModelId": "h100-sxm-80gb", "asOf": as_of, "score": score, "band": pressure.band_for(score),
                "factors": [{"factor": n, "raw": r, "normalised": z, "weight": w, "contribution": (w * z) if z is not None else None, "direction": pressure._direction(z)} for n, r, z, w in factors],
                "contradictions": list(contradictions), "uncertainty": {"coverage": coverage, "factorsWithData": 4, "factorsTotal": 6, "sampleShare": 1.0, "oldestInput": as_of, "label": "exploratory"},
                "history": [{"t": t, "score": sc, "band": pressure.band_for(sc)} for t, sc in history]}
    tx_facilities = [f for f in facilities if f["regionId"] == "us-tx"]
    bottleneck = synthetic_cell(82.0, availability=0.9, premium=0.4, momentum=0.1, history=((previous_at, 78.0), (as_of, 82.0)))
    spread_offers = [mk_offer(i, p, region="us-tx") for i, p in enumerate((1.0, 1.2, 3.0, 4.0, 5.0), start=1)]
    # With nothing operating the pipeline factor is absolute MW (V4 F8): 300 MW over the 500 MW full-relief scale.
    mw_overhang = synthetic_cell(18.0, availability=0.0, premium=-0.3)
    mw_overhang["factors"] = [{**f, "raw": 300.0, "unit": "MW", "normalised": -0.6, "contribution": -0.06, "direction": "down"} if f["factor"] == "capacity-pipeline" else f
                              for f in mw_overhang["factors"]]
    cases["anomalies"] += [
        {"name": "edge-bottleneck-contradiction-free-severity-3", "input": {"cells": [bottleneck], "offers": [mk_offer(1, 3.0, region="us-tx")], "gpuModels": {"h100-sxm-80gb": h100}, "regions": regions,
                                                                           "facilities": tx_facilities, "series": {}, "exposuresByRegion": {"us-tx": [{"ticker": "CRWV", "documented": True, "basis": ["facility-owner"]}]}}},
        {"name": "edge-bottleneck-contradicted-severity-2", "input": {"cells": [synthetic_cell(80.0, availability=0.8, premium=0.3, momentum=-0.05, contradictions=["price-momentum"])],
                                                                     "offers": [mk_offer(1, 3.0, region="us-tx")], "gpuModels": {"h100-sxm-80gb": h100}, "regions": regions, "facilities": [], "series": {}, "exposuresByRegion": {}}},
        {"name": "edge-overhang", "input": {"cells": [synthetic_cell(18.0, availability=0.0, premium=-0.3, pipeline=2.5)], "offers": [mk_offer(1, 3.0, region="us-tx")], "gpuModels": {"h100-sxm-80gb": h100},
                                           "regions": regions, "facilities": [], "series": {}, "exposuresByRegion": {}}},
        {"name": "edge-price-dispersion-needs-four-asks", "input": {"cells": [synthetic_cell(50.0, availability=0.0)], "offers": spread_offers, "gpuModels": {"h100-sxm-80gb": h100}, "regions": regions,
                                                                   "facilities": [], "series": {}, "exposuresByRegion": {}}},
        {"name": "edge-three-asks-no-dispersion", "input": {"cells": [synthetic_cell(50.0, availability=0.0, coverage=3)], "offers": spread_offers[:3], "gpuModels": {"h100-sxm-80gb": h100}, "regions": regions,
                                                           "facilities": [], "series": {}, "exposuresByRegion": {}}},
        {"name": "edge-divergence-needs-documented-basket", "input": {"cells": [synthetic_cell(50.0, availability=0.0, momentum=0.2)], "offers": [mk_offer(1, 3.0, region="us-tx")], "gpuModels": {"h100-sxm-80gb": h100},
                                                                     "regions": regions, "facilities": [], "series": {"equity:CRWV:close": {"unit": "USD", "kind": "sample", "points": [["2026-10-01T00:00:00Z", 100.0], ["2026-11-06T00:00:00Z", 85.0]]}},
                                                                     "exposuresByRegion": {"us-tx": [{"ticker": "CRWV", "documented": False, "basis": ["sample-profile"]}]}}},
        {"name": "edge-divergence-fires-with-documented-basket", "input": {"cells": [synthetic_cell(50.0, availability=0.0, momentum=0.2)], "offers": [mk_offer(1, 3.0, region="us-tx")], "gpuModels": {"h100-sxm-80gb": h100},
                                                                          "regions": regions, "facilities": [], "series": {"equity:CRWV:close": {"unit": "USD", "kind": "sample", "points": [["2026-10-01T00:00:00Z", 100.0], ["2026-11-06T00:00:00Z", 85.0]]}},
                                                                          "exposuresByRegion": {"us-tx": [{"ticker": "CRWV", "documented": True, "basis": ["facility-owner"]}]}}},
        {"name": "edge-overhang-absolute-mw-no-operating", "input": {"cells": [mw_overhang], "offers": [mk_offer(1, 3.0, region="us-tx")], "gpuModels": {"h100-sxm-80gb": h100},
                                                                    "regions": regions, "facilities": [], "series": {}, "exposuresByRegion": {}}},
        {"name": "edge-first-seen-from-recorded-detection", "input": {"cells": [synthetic_cell(50.0, availability=0.0, history=((as_of, 50.0),))], "offers": spread_offers,
                                                                     "gpuModels": {"h100-sxm-80gb": h100}, "regions": regions, "facilities": [], "series": {}, "exposuresByRegion": {},
                                                                     "firstDetected": {"anomaly:price-dispersion:us-tx:h100-sxm-80gb": previous_at}}},
        {"name": "edge-first-seen-history-beats-later-detection", "input": {"cells": [bottleneck], "offers": [mk_offer(1, 3.0, region="us-tx")], "gpuModels": {"h100-sxm-80gb": h100},
                                                                           "regions": regions, "facilities": [], "series": {}, "exposuresByRegion": {},
                                                                           "firstDetected": {"anomaly:bottleneck:us-tx:h100-sxm-80gb": "2026-11-02T00:00:00Z"}}},
        {"name": "edge-empty-cell-skipped", "input": {"cells": [synthetic_cell(90.0, availability=1.0, premium=0.5, coverage=0)], "offers": [], "gpuModels": {"h100-sxm-80gb": h100}, "regions": regions,
                                                     "facilities": [], "series": {}, "exposuresByRegion": {}}},
    ]
    cases.update(build_chain_cases(data, offers, previous, models, regions, mk_offer, cases))
    return cases


def build_chain_cases(data, offers, previous, models, regions, mk_offer, built) -> dict[str, list[dict]]:
    as_of = T0
    cases: dict[str, list[dict]] = {"infra_scenario": [], "evaluation": [], "signals": [], "assistant": [], "evaluation_terminal": []}
    tx_cell = next(c for c in built["pressure"] if c["name"] == "sample-us-tx-h100-with-history")["input"]
    companies = [{"ticker": c.ticker, "name": c.name, "tags": c.compute_exposure.tags} for c in data.companies.values()]
    base = {k: v for k, v in tx_cell.items() if k != "snapshots"}
    base.update({"companies": companies, "horizonDays": 30})
    cases["infra_scenario"] += [
        {"name": "sample-us-tx-h100-delay-fac-002-18m", "input": {**base, "assumptions": [{"kind": "delay-facility", "facilityId": "fac-002", "months": 18}]}},
        {"name": "sample-us-tx-h100-complete-fac-015-in-horizon", "input": {**base, "assumptions": [{"kind": "complete-facility", "facilityId": "fac-015", "atDate": "2026-11-20"}]}},
        {"name": "sample-us-tx-h100-energy-shock-plus-40", "input": {**base, "assumptions": [{"kind": "energy-shock", "pct": 40}]}},
        {"name": "sample-us-tx-h100-demand-shock-plus-25", "input": {**base, "assumptions": [{"kind": "demand-shock", "pct": 25}]}},
        {"name": "sample-us-tx-h100-utilisation-0.9", "input": {**base, "assumptions": [{"kind": "utilisation", "value": 0.9}]}},
        {"name": "sample-us-tx-h100-no-assumptions", "input": {**base, "assumptions": []}},
        {"name": "edge-unknown-kind-ignored-and-reported", "input": {**base, "assumptions": [{"kind": "moon-phase", "pct": 10}, {"kind": "energy-shock", "pct": 10}]}},
        {"name": "edge-facility-outside-region-ignored", "input": {**base, "assumptions": [{"kind": "delay-facility", "facilityId": "fac-024", "months": 6}]}},
        {"name": "edge-horizon-out-of-range-refused", "input": {**base, "horizonDays": 400, "assumptions": []}},
        {"name": "edge-utilisation-out-of-range-ignored", "input": {**base, "assumptions": [{"kind": "utilisation", "value": 0.99}]}},
        # R1-3: a year out the drift band would cross zero; a GPU price is floored there and the floor stated as a caveat.
        # The R1 shape: a short, swinging price history whose 90-day drift band reaches below zero.
        {"name": "edge-price-band-floored-at-zero", "input": {**base, "offers": [], "markets": [], "feed": [], "facilities": [], "companies": [], "horizonDays": 90, "assumptions": [{"kind": "demand-shock", "pct": -20}],
                                                             "series": {"gpu:h100-sxm-80gb:on-demand:global": {
                                                                 "unit": "USD/GPU-hour", "kind": "sample",
                                                                 "points": [["2026-10-31T21:00:00Z", 3.4], ["2026-11-01T21:00:00Z", 2.1], ["2026-11-02T21:00:00Z", 3.9],
                                                                            ["2026-11-03T21:00:00Z", 1.8], ["2026-11-04T21:00:00Z", 3.6], ["2026-11-05T21:00:00Z", 2.0],
                                                                            ["2026-11-06T21:00:00Z", 2.86]]}}}},
    ]

    # evaluation: a deterministic synthetic market where the compute feature really does lead returns, and one too short
    def synth(n, seed=7):
        closes, fcpi, pressure_s, energy = [], [], [], []
        price, level, score, power = 100.0, 2.0, 40.0, 40.0
        x = seed
        for i in range(n):
            x = (x * 1103515245 + 12345) % (2 ** 31)
            noise = (x / 2 ** 31 - 0.5) * 0.02
            level_change = 0.01 if (i // 20) % 2 == 0 else -0.01
            level = level * (1 + level_change + noise / 4)
            score = max(0.0, min(100.0, score + level_change * 50 + noise * 20))
            power = power * (1 + noise)
            # Returns follow the FCPI trend with a lag, plus noise:
            price = price * (1 + 0.4 * level_change + noise)
            t = f"2026-{1 + i // 28:02d}-{1 + i % 28:02d}T00:00:00Z"
            closes.append([t, round(price, 4)])
            fcpi.append([t, round(level, 5)])
            pressure_s.append([t, round(score, 2)])
            energy.append([t, round(power, 3)])
        return closes, fcpi, pressure_s, energy
    closes, fcpi, pressure_s, energy = synth(200)
    feed = [{"id": f"evt-{i}", "kind": "announcement", "headline": "synthetic", "severity": 3, "observedAt": closes[i][0], "classification": "verified",
             "entities": {"tickers": ["SYN"]}} for i in range(70, 200, 15)]
    full = {"ticker": "SYN", "closes": {"unit": "USD", "kind": "sample", "points": closes}, "fcpi": {"unit": "USD/GPU-hour", "kind": "sample", "points": fcpi},
            "pressure": {"unit": "score", "kind": "sample", "points": pressure_s}, "energy": {"unit": "USD/MWh", "kind": "sample", "points": energy}, "feed": feed}
    cases["evaluation"] += [
        {"name": "synthetic-200-days-all-features", "input": {**full, "horizons": [1, 5, 21]}},
        {"name": "synthetic-200-days-fcpi-only", "input": {**full, "pressure": None, "energy": None, "horizons": [5]}},
        {"name": "edge-no-features-augmented-equals-baseline", "input": {**full, "fcpi": None, "pressure": None, "energy": None, "feed": [], "horizons": [5]}},
        {"name": "edge-short-series-insufficient-sample", "input": {**{k: v for k, v in full.items()}, "closes": {"unit": "USD", "kind": "sample", "points": closes[:75]},
                                                                 "fcpi": {"unit": "USD/GPU-hour", "kind": "sample", "points": fcpi[:75]}, "pressure": None, "energy": None, "horizons": [1, 21]}},
        {"name": "edge-sixty-three-day-horizon-on-180-days", "input": {**full, "closes": {"unit": "USD", "kind": "sample", "points": closes[:180]}, "horizons": [63]}},
        {"name": "synthetic-200-days-raw-gpu-price-named-for-what-it-is", "input": {**full, "pressure": None, "energy": None, "priceFeature": "gpu-price-change-30d", "horizons": [5]}},
        {"name": "edge-flat-price-feature-dropped-no-intercept-verdict", "input": {**full, "fcpi": {"unit": "USD/GPU-hour", "kind": "sample", "points": [[t, 2.0] for t, _ in fcpi]},
                                                                        "pressure": None, "energy": None, "feed": [], "horizons": [5]}},
    ]
    # evaluation_terminal: the whole route composition over the sample bag, one documented-region ticker and one without
    cases["evaluation_terminal"] = [{"name": "sample-bag-crwv-and-nvda", "input": {"tickers": ["CRWV", "NVDA"]}}]

    # signals: anomaly signals through the detector, and strategy signals from ledger rows
    anomaly_edges = {c["name"]: c["input"] for c in built["anomalies"]}
    crwv = {"equity:CRWV:close": {"unit": "USD", "kind": "sample", "points": [[f"2026-{m:02d}-{d:02d}T21:00:00Z", 90.0 + m + d / 10] for m in (7, 8, 9, 10, 11) for d in range(1, 29) if not (m == 11 and d > 27)]}}
    undocumented = {**anomaly_edges["edge-bottleneck-contradiction-free-severity-3"], "exposuresByRegion": {"us-tx": [{"ticker": "CRWV", "documented": False, "basis": ["sample-profile"]}]}}
    documented_thin = anomaly_edges["edge-bottleneck-contradicted-severity-2"]
    documented_thin = {**documented_thin, "cells": [{**documented_thin["cells"][0], "uncertainty": {**documented_thin["cells"][0]["uncertainty"], "coverage": 2}}],
                       "exposuresByRegion": {"us-tx": [{"ticker": "CRWV", "documented": True, "basis": ["facility-owner"]}]}}
    documented_full = {**anomaly_edges["edge-bottleneck-contradiction-free-severity-3"], "cells": [{**anomaly_edges["edge-bottleneck-contradiction-free-severity-3"]["cells"][0],
                       "uncertainty": {"coverage": 5, "factorsWithData": 5, "factorsTotal": 6, "sampleShare": 1.0, "oldestInput": as_of, "label": "exploratory"},
                       "history": [{"t": "2026-10-01T21:00:00Z", "score": 80.0, "band": "acute"}, {"t": as_of, "score": 82.0, "band": "acute"}]}]}
    cases["signals"] += [
        {"name": "anomaly-without-documented-exposure-is-skipped", "input": {"anomaly": undocumented, "series": crwv}},
        {"name": "anomaly-thin-coverage-is-insufficient-sample", "input": {"anomaly": documented_thin, "series": crwv}},
        {"name": "anomaly-documented-not-followed-with-forecast-and-outcome", "input": {"anomaly": documented_full, "series": crwv}},
        {"name": "anomaly-outcome-absent-before-horizon", "input": {"anomaly": documented_full, "series": {"equity:CRWV:close": {"unit": "USD", "kind": "sample", "points": crwv["equity:CRWV:close"]["points"][:-20]}}}},
    ]
    decision = {"id": 1, "strategyId": "divergence", "t": "2026-10-06T21:00:00Z", "action": "buy", "ticker": "CRWV", "status": "proposed", "rationale": {"signal": "z-above-enter", "z": 1.8, "strategyId": "divergence"},
                "proposedWeight": 0.05, "horizonDays": 20, "orderIds": [], "skippedReason": None, "mode": "forward", "signalId": "signal:divergence:CRWV:2026-10-06", "signalKind": "price-change"}
    filled = {**decision, "id": 2, "status": "filled", "orderIds": ["ord-1"]}
    order = {"id": "ord-1", "status": "filled", "side": "buy", "quantity": 500.0, "fillPrice": 100.05, "origin": "strategy:divergence"}
    skipped = {**decision, "id": 3, "status": "skipped", "orderIds": [], "skippedReason": "risk-limit"}
    cases["signals"] += [
        {"name": "decision-proposed-no-orders", "input": {"decisions": [decision], "orders": [], "series": crwv}},
        {"name": "decision-filled-with-order-and-outcome", "input": {"decisions": [decision, filled], "orders": [order], "series": crwv}},
        {"name": "decision-skipped-risk-limit", "input": {"decisions": [skipped], "orders": [], "series": crwv}},
        {"name": "decision-without-signal-id-yields-nothing", "input": {"decisions": [{**decision, "signalId": None, "action": "hold"}], "orders": [], "series": crwv}},
    ]

    # assistant: a regional brief over the sample inputs for Texas
    grid_input = next(c for c in built["pressure"] if c["name"] == "sample-grid-all-cells")["input"]
    tx = {k: v for k, v in grid_input.items() if k != "mode"}
    tx["facilities"] = [f for f in grid_input["facilities"] if f["regionId"] == "us-tx"]
    tx["feed"] = [i for i in grid_input["feed"] if "us-tx" in i["geography"]["regionIds"]]
    tx["markets"] = [m for m in grid_input["markets"] if m["id"] in ("mkt-001", "mkt-005", "mkt-006")]
    tx["series"] = {**tx["series"], **{f"equity:{t}:close": {"unit": "USD", "kind": "sample", "points": [[to_json(p.t), to_json(p.value)] for p in data.series[f"equity:{t}:close"].points[-36:]]} for t in data.companies}}
    tx.update({"regionId": "us-tx", "targetName": "US Central (Dallas hub)", "companies": companies, "latest": [["ERCOT day-ahead power", "energy:ERCOT:day-ahead"]],
               "evaluation": [{"ticker": "CRWV", "horizonDays": 5, "verdict": "worsens", "n": 115, "skill": -0.2489, "inputKinds": ["sample"]}]})
    cases["assistant"] += [
        {"name": "sample-texas-brief", "input": tx},
        {"name": "edge-region-with-nothing", "input": {**tx, "regionId": "us-va", "targetName": "Virginia", "offers": [], "facilities": [], "feed": [], "markets": [], "series": {}, "latest": [], "evaluation": []}},
    ]
    return cases


def generate(data=None) -> dict[str, list[dict]]:
    data = data or load_sample_uncached()
    cases = build_cases(data)
    for engine, rows in cases.items():
        for case in rows:
            case["expected"] = run_case(engine, case["input"], data)
    return cases


def render(rows: list[dict]) -> str:
    return json.dumps(rows, indent=None, separators=(",", ":"), ensure_ascii=False, allow_nan=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--check", action="store_true", help="exit 1 if any fixture file would change")
    args = parser.parse_args(argv)
    cases = generate()
    changed = []
    for engine in ENGINES:
        path = HERE / f"{engine}.json"
        text = render(cases[engine])
        if args.check:
            if not path.exists() or path.read_text(encoding="utf-8") != text:
                changed.append(path.name)
        else:
            path.write_text(text, encoding="utf-8")
            limit = SIZE_LIMIT_KB.get(engine, DEFAULT_SIZE_LIMIT_KB)
            size_kb = len(text.encode()) // 1024
            print(f"wrote {path.name}: {len(cases[engine])} cases, {size_kb} KB (limit {limit} KB)")
    if changed:
        print("fixtures would change: " + ", ".join(changed), file=sys.stderr)
        return 1
    if args.check:
        print("fixtures up to date")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
