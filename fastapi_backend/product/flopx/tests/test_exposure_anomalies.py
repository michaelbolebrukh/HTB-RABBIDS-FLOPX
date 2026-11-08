"""Exposure tracing and anomaly rules (architecture §10.3, §10.5) on constructed cells."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from product.flopx import anomalies, domain, exposure, pressure, signals
from product.flopx.provenance import Provenance, live, sample

T0 = datetime(2026, 11, 6, 21, tzinfo=timezone.utc)
H100 = domain.GpuModel(id="h100-sxm-80gb", vendor="NVIDIA", family="Hopper", memory_gb=80, interconnect="NVLink", fp16_tflops=989.0, release_year=2022)
REGIONS = {"us-tx": domain.Region(id="us-tx", name="Texas", country_code="US", lat=0, lon=0, grid_zone="ERCOT"),
           "us-va": domain.Region(id="us-va", name="Virginia", country_code="US", lat=0, lon=0, grid_zone="PJM")}


def company(ticker, tags=(), name=None):
    return domain.Company(ticker=ticker, name=name or ticker, exchange="X", segments=[], compute_exposure=domain.ComputeExposure(text="", tags=list(tags)),
                          related_facility_ids=[], peers=[], provenance=sample(T0))


def facility(fid, region, status="operating", owners=(), operator=None, mw=100.0):
    return domain.Facility(id=fid, name=fid, lat=0, lon=0, region_id=region, status=status, owner_company_ids=list(owners), operator=operator, capacity_mw=mw, provenance=sample(T0))


def offer(i, price, region="us-tx", availability="available"):
    return domain.GpuOffer(id=f"o{i}", provider_id="p", region_id=region, gpu_model_id=H100.id, gpu_count=8, price_per_gpu_hour_usd=price,
                           availability=availability, interruptible=False, contract="on-demand", provenance=sample(T0))


def cell(offers, facilities=(), series=None):
    return pressure.compute_cell("us-tx", H100.id, offers=offers, gpu_models={H100.id: H100}, regions=REGIONS, series=series or {}, facilities=list(facilities),
                                 markets=[], feed=[], as_of=T0)


def detect(cells, offers, exposures=None, facilities=(), series=None):
    return anomalies.detect(cells, offers=offers, gpu_models={H100.id: H100}, regions=REGIONS, facilities=list(facilities), series=series or {},
                            exposures_by_region=exposures or {})


# -- exposure -------------------------------------------------------------------------

def test_documented_only_for_data_backed_links():
    companies = {"CRWV": company("CRWV", ["gpu-cloud"]), "VRT": company("VRT", [], name="Vertiv"), "NBIS": company("NBIS", ["europe"]), "XYZ": company("XYZ", [])}
    observed = live("wikidata", T0, licence="CC0")
    facilities = [replace(facility("f1", "us-tx", owners=["CRWV"]), provenance=observed), replace(facility("f2", "us-tx", operator="vertiv"), provenance=observed)]
    rows = {e.ticker: e for e in exposure.trace("us-tx", None, companies=companies, facilities=facilities, regions=REGIONS)}
    assert rows["CRWV"].documented and rows["CRWV"].basis == ["facility-owner", "sample-profile"] and rows["CRWV"].facility_ids == ["f1"]
    assert rows["VRT"].documented and rows["VRT"].basis == ["facility-operator"]
    assert "NBIS" not in rows and "XYZ" not in rows                       # europe tag does not reach Texas; no tags, no link
    europe = {e.ticker: e for e in exposure.trace("eu-de", None, companies=companies, facilities=[], regions={"eu-de": domain.Region(id="eu-de", name="Germany", country_code="DE", lat=0, lon=0)})}
    assert europe["NBIS"].documented is False and europe["NBIS"].basis == ["sample-profile"]
    assert all("not a measured beta" in e.sensitivity_basis for e in rows.values())
    assert exposure.documented_tickers(list(rows.values())) == ["CRWV", "VRT"]


def test_a_link_from_an_authored_sample_facility_is_authored_not_documented():
    # V1 F7: a sample "Ashburn campus" naming CRWV as owner is an authored record, not a document.
    companies = {"CRWV": company("CRWV", []), "VRT": company("VRT", [], name="Vertiv")}
    facilities = [facility("f1", "us-tx", owners=["CRWV"]), facility("f2", "us-tx", operator="vertiv")]   # provenance: sample
    rows = {e.ticker: e for e in exposure.trace("us-tx", None, companies=companies, facilities=facilities, regions=REGIONS)}
    assert rows["CRWV"].basis == ["facility-owner"] and rows["CRWV"].documented is False and rows["CRWV"].facility_ids == ["f1"]
    assert rows["VRT"].basis == ["facility-operator"] and rows["VRT"].documented is False
    assert exposure.documented_tickers(list(rows.values())) == []


def test_filing_mention_documents_only_from_sec():
    companies = {"SMCI": company("SMCI")}
    sec_item = domain.IntelItem(id="sec:1", kind="announcement", headline="10-Q notes new Texas assembly site", why_it_matters="", published_at=T0, observed_at=T0, severity=2,
                                classification="verified", provenance=live("sec-edgar", T0), entities=domain.IntelEntities(tickers=["SMCI"]))
    hn_item = domain.IntelItem(**{**sec_item.__dict__, "id": "hn:1", "provenance": live("hacker-news", T0)})
    assert exposure.trace("us-tx", None, companies=companies, facilities=[], regions=REGIONS, filings=[sec_item])[0].documented is True
    assert exposure.trace("us-tx", None, companies=companies, facilities=[], regions=REGIONS, filings=[hn_item]) == []


# -- anomalies -------------------------------------------------------------------------

def test_bottleneck_fires_on_an_acute_scarce_cell_with_contradiction_free_severity():
    # Two unavailable cell asks around 4.1 against three Virginia asks at 2.0: global median 2.0, premium raw 1.05 -> +1,
    # availability raw 1 -> +1; two factors with data -> score 100
    offers = [offer(1, 4.2, availability="unavailable"), offer(2, 4.0, availability="unavailable"), offer(3, 2.0, "us-va"), offer(4, 2.0, "us-va"), offer(5, 2.0, "us-va")]
    c = cell(offers)
    assert c.score == 100.0 and c.band == "acute" and c.contradictions == []
    found = detect([c], offers)
    bottleneck = next(a for a in found if a.type == "bottleneck")
    assert bottleneck.id == "anomaly:bottleneck:us-tx:h100-sxm-80gb" and bottleneck.severity == 4
    assert bottleneck.severity_basis == "1 + band distance 2 + 1 (no factor argues against the score)"
    assert bottleneck.statement_class == "inference" and bottleneck.rule == anomalies.RULES["bottleneck"]
    assert [f.factor for f in bottleneck.supporting] == ["availability", "price-premium"]


def test_price_dispersion_needs_four_asks_and_measures_p90_p10_over_median():
    spread = [offer(i, p) for i, p in enumerate((1.0, 1.2, 3.0, 4.0, 5.0), start=1)]
    found = detect([cell(spread)], spread)
    dispersion = next(a for a in found if a.type == "price-dispersion")
    assert dispersion.measures["median"] == 3.0 and dispersion.measures["p10"] == pytest.approx(1.08) and dispersion.measures["p90"] == pytest.approx(4.6)
    assert dispersion.measures["spread"] == pytest.approx(1.1733, abs=1e-4) and dispersion.severity == 3
    three = spread[:3]
    assert not [a for a in detect([cell(three)], three) if a.type == "price-dispersion"]


def test_overhang_and_announcement_mismatch_use_the_pipeline():
    facilities = [facility("op", "us-tx", mw=100), facility("plan", "us-tx", status="planned", mw=300)]
    slack = [offer(1, 1.0), offer(2, 1.0), offer(3, 3.0, "us-va"), offer(4, 3.0, "us-va")]      # premium -1, availability -1, pipeline 3.0
    c = cell(slack, facilities)
    assert c.score <= 25
    assert any(a.type == "overhang" for a in detect([c], slack, facilities=facilities))
    big_ops = [facility("op", "us-tx", mw=250)]
    premium = [offer(1, 3.6), offer(2, 3.6), offer(3, 2.0, "us-va"), offer(4, 2.0, "us-va")]     # premium raw +0.29 over 200 MW operating
    mismatch = [a for a in detect([cell(premium, big_ops)], premium, facilities=big_ops) if a.type == "announcement-mismatch"]
    assert mismatch and mismatch[0].measures["operatingMw"] == 250 and mismatch[0].measures["premiumRaw"] >= 0.2


def _closes(ticker, first, last):
    points = [domain.PriceObservation(series_id=f"equity:{ticker}:close", t=T0 - timedelta(days=40), value=first, unit="USD", provenance=sample(T0)),
              domain.PriceObservation(series_id=f"equity:{ticker}:close", t=T0, value=last, unit="USD", provenance=sample(T0))]
    return domain.Series(id=f"equity:{ticker}:close", unit="USD", points=points)


def test_divergence_prefers_a_documented_basket_and_labels_an_authored_one():
    """G11-b: with no documented link the divergence may rest on the authored link, saying so in its headline
    and carrying the link's sample provenance in its inputs; a documented basket keeps the documented wording."""
    index = domain.Series(id="gpu:h100-sxm-80gb:on-demand:global", unit="USD/GPU-hour",
                          points=[domain.PriceObservation(series_id="x", t=T0 - timedelta(days=40), value=2.0, unit="u", provenance=sample(T0)),
                                  domain.PriceObservation(series_id="x", t=T0, value=2.4, unit="u", provenance=sample(T0))])
    offers = [offer(1, 3.0), offer(2, 3.0, "us-va")]
    c = cell(offers, series={index.id: index})
    series = {index.id: index, **{s.id: s for s in [_closes("CRWV", 100.0, 85.0)]}}
    undocumented = {"us-tx": [domain.Exposure("CRWV", ["sample-profile"], False, 0.5, "", [], sample(T0))]}
    documented = {"us-tx": [domain.Exposure("CRWV", ["facility-owner"], True, 0.5, "", ["f1"], sample(T0))]}
    authored = [a for a in detect([c], offers, undocumented, series=series) if a.type == "divergence"]
    assert len(authored) == 1
    assert "the basket linked by an authored sample profile, not a filing (CRWV)" in authored[0].headline
    assert authored[0].measures["basketBasis"] == "authored"
    assert "sample" in authored[0].provenance.input_kinds
    fired = [a for a in detect([c], offers, documented, series=series) if a.type == "divergence"]
    assert fired and fired[0].measures["indexChange30d"] == pytest.approx(0.2) and fired[0].measures["basketReturn30d"] == pytest.approx(-0.15)
    assert "the documented basket (CRWV)" in fired[0].headline and fired[0].measures["basketBasis"] == "documented"
    # A documented link outranks an authored one in the same region: the basket is the documented companies only.
    both = {"us-tx": [*documented["us-tx"], domain.Exposure("NBIS", ["sample-profile"], False, 0.5, "", [], sample(T0))]}
    series_both = {**series, **{s.id: s for s in [_closes("NBIS", 100.0, 130.0)]}}
    mixed = [a for a in detect([c], offers, both, series=series_both) if a.type == "divergence"]
    assert mixed and mixed[0].measures["basket"] == ["CRWV"]


def test_empty_cells_are_skipped_and_ids_are_stable():
    empty = pressure.compute_cell("us-va", H100.id, offers=[offer(1, 3.0)], gpu_models={H100.id: H100}, regions=REGIONS, series={}, facilities=[], markets=[], feed=[], as_of=T0)
    assert detect([empty], [offer(1, 3.0)]) == []
    offers = [offer(1, 4.2, availability="unavailable"), offer(2, 4.0, availability="unavailable"), offer(3, 2.0, "us-va")]
    first = detect([cell(offers)], offers)
    second = detect([cell(offers)], offers)
    assert [a.id for a in first] == [a.id for a in second]


# -- anomaly signals -------------------------------------------------------------------------

def test_an_anomaly_without_documented_exposure_yields_a_skipped_signal():
    offers = [offer(1, 4.2, availability="unavailable"), offer(2, 4.0, availability="unavailable"), offer(3, 2.0, "us-va"), offer(4, 2.0, "us-va"), offer(5, 2.0, "us-va")]
    profile_only = {"us-tx": [domain.Exposure("NVDA", ["sample-profile"], False, 0.5, "", [], sample(T0))]}
    anomaly = next(a for a in detect([cell(offers)], offers, profile_only) if a.type == "bottleneck")
    signal = signals.from_anomaly(anomaly, {})
    assert signal.id == f"signal:anomaly:{anomaly.id}" and signal.kind == "anomaly"
    # R1-8: the reason names the basis the links rest on.
    assert signal.skipped == {"reason": "no-documented-exposure", "note": "no company is linked to the cell by an observed facility record or an SEC filing; "
                                                                          "the listed company (NVDA) is linked by an authored sample profile"}
    assert signal.decision_refs == [] and signal.orders == [] and signal.connected == [] and signal.forecast_ref is None and signal.outcome is None


def test_a_documented_anomaly_signal_is_not_followed_and_gains_an_outcome_once_the_horizon_passed():
    offers = [offer(1, 4.2, availability="unavailable"), offer(2, 4.0, availability="unavailable"), offer(3, 2.0, "us-va"), offer(4, 2.0, "us-va"), offer(5, 2.0, "us-va")]
    documented = {"us-tx": [domain.Exposure("CRWV", ["facility-owner"], True, 0.5, "", ["f1"], sample(T0))]}
    anomaly = next(a for a in detect([cell(offers)], offers, documented) if a.type == "bottleneck")
    start = T0 - timedelta(days=90)
    points = [domain.PriceObservation(series_id="equity:CRWV:close", t=start + timedelta(days=i), value=100.0 + i * 0.5, unit="USD", provenance=sample(T0)) for i in range(120)]
    series = {"equity:CRWV:close": domain.Series(id="equity:CRWV:close", unit="USD", points=points)}
    thin = signals.from_anomaly(anomaly, series)
    # Two asks and two factors with data: documented or not, the sample behind the cell is too thin to act on.
    assert thin.skipped["reason"] == "insufficient-sample" and thin.connected == [{"instrument": "CRWV", "basis": "facility-owner"}]
    strong = replace(anomaly, uncertainty=replace(anomaly.uncertainty, coverage=5, factors_with_data=5))
    signal = signals.from_anomaly(strong, series)
    assert signal.skipped["reason"] == "strategy-not-followed" and "no strategy consumes anomaly signals" in signal.skipped["note"]
    assert signal.forecast_ref["horizon_days"] == 21 and signal.forecast_ref["statement_class"] == "forecast" and signal.forecast_ref["exploratory"] is True
    assert signal.outcome["horizon_days"] == 21 and signal.outcome["realised_return"] > 0 and "forecast_error" in signal.outcome
    # No close 21 days after the anomaly yet: no outcome, and no pretence of one.
    before = {"equity:CRWV:close": domain.Series(id="equity:CRWV:close", unit="USD", points=[p for p in points if p.t <= T0])}
    assert signals.from_anomaly(strong, before).outcome is None


# -- firstSeen is never the time of the read (V1 F4) ---------------------------------------------------

def _history_cell(base, points):
    hist = [domain.HistoryPoint(t=t, score=s, band=pressure.band_for(s), coverage=4, factors_with_data=4, label="indicative", input_kinds=["live"])
            for t, s in points]
    return pressure.with_history(base, hist, "test")


def test_first_seen_is_the_earliest_history_snapshot_where_the_rule_held():
    offers = [offer(i, 1.0, availability="unavailable") for i in range(4)]
    base = replace(cell(offers), score=80.0, band="acute")   # all asks unavailable: availability normalised 1.0
    day1, day2 = T0 - timedelta(days=2), T0 - timedelta(days=1)
    c = _history_cell(base, [(day1, 50.0), (day2, 80.0), (T0, 80.0)])
    found = [a for a in detect([c], offers) if a.type == "bottleneck"]
    assert len(found) == 1
    assert found[0].first_seen == day2 and "score history" in found[0].first_seen_basis


def test_a_recorded_first_detection_beats_the_current_snapshot_and_is_stable():
    offers = [offer(i, p) for i, p in enumerate([1.0, 1.1, 3.0, 4.0])]   # wide dispersion: a rule the score history cannot answer
    c = _history_cell(cell(offers), [(T0, 50.0)])
    first = [a for a in detect([c], offers) if a.type == "price-dispersion"]
    assert len(first) == 1
    # Nothing on record: the snapshot it was found in, never a read time the engine invents.
    assert first[0].first_seen == T0 and "snapshot the rule was found in" in first[0].first_seen_basis
    # A later read (a newer snapshot) with the earlier detection on record keeps the earlier time.
    later = T0 + timedelta(minutes=5)
    c2 = _history_cell(replace(cell(offers), as_of=later), [(later, 50.0)])
    again = [a for a in anomalies.detect([c2], offers=offers, gpu_models={H100.id: H100}, regions=REGIONS, facilities=[], series={},
                                         exposures_by_region={}, first_detected={first[0].id: T0}) if a.type == "price-dispersion"]
    assert again[0].first_seen == T0 and "first detected" in again[0].first_seen_basis
    # And the signal derived from it carries the same time.
    assert signals.from_anomaly(again[0], {}).at == T0


# -- a pipeline against no operating capacity is never a ratio over a floor (V4 F8) -----------------

def test_pipeline_with_nothing_operating_is_absolute_mw_and_never_a_ratio():
    rows = [facility("p", "us-tx", status="announced", mw=50.0)]
    factor = pressure.capacity_pipeline(rows, "us-tx")
    assert factor.unit == "MW" and factor.raw == 50.0
    assert factor.normalised == pytest.approx(-0.1)                       # 50 of the 500 MW full-relief scale
    assert "no operating capacity in the data" in factor.note
    with_ops = pressure.capacity_pipeline([*rows, facility("o", "us-tx", mw=100.0)], "us-tx")
    assert with_ops.unit == "ratio" and with_ops.raw == pytest.approx(0.5) and with_ops.normalised == pytest.approx(-0.25)


def test_overhang_on_a_region_with_nothing_operating_needs_250_mw_and_says_so():
    offers = [offer(1, 1.0)]
    small = replace(cell(offers, facilities=[facility("p", "us-tx", status="announced", mw=50.0)]), score=18.0, band="slack")
    assert [a for a in detect([small], offers) if a.type == "overhang"] == []      # 50 MW was "50.0x" over a 1 MW floor
    big = replace(cell(offers, facilities=[facility("p", "us-tx", status="announced", mw=300.0)]), score=18.0, band="slack")
    found = [a for a in detect([big], offers) if a.type == "overhang"]
    assert len(found) == 1
    assert "300 MW, with no operating capacity in the data" in found[0].headline and "x " not in found[0].headline
    assert found[0].measures["pipelineUnit"] == "MW"
