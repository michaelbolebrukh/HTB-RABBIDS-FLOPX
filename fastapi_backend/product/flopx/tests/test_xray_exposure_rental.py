"""N-XRAY: the Compute X-Ray's Financial exposure and Rental economics answers for every region.

Exposure: a covered region never answers with an empty list; documented ties (observed facility record or SEC
filing) sort first and say so, every other row is an `authored` tie that names its basis and carries the
"not a filing" note, and a signed-in account's paper position is overlaid per row without touching the shared
composition. Rental: a cell with regional asks reads its median and premium from them; a cell without reads the
model's global median and GetDeploying's archived weekly median, labelled as the fallback, with the premium
"n/a (no regional asks)" rather than nothing.
"""

import statistics
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import platform.models  # noqa: F401
import product.models  # noqa: F401
from infrastructure.database import Base
from platform.models import User
from product.flopx import domain, exposure, pressure, terminal
from product.flopx.adjust import adjust_offer
from product.flopx.data.loader import load_sample_uncached
from product.flopx.paper import broker
from product.flopx.provenance import Provenance, live, sample
from product.models import PaperAccount, PaperPosition

T0 = datetime(2026, 11, 6, 21, tzinfo=timezone.utc)
H100 = domain.GpuModel(id="h100-sxm-80gb", vendor="NVIDIA", family="Hopper", memory_gb=80, interconnect="NVLink", fp16_tflops=989.0, release_year=2022)
REGIONS = {"us-tx": domain.Region(id="us-tx", name="US Central (Dallas hub)", country_code="US", lat=0, lon=0, grid_zone="ERCOT"),
           "ap-jp": domain.Region(id="ap-jp", name="Japan (Tokyo hub)", country_code="JP", lat=0, lon=0)}
WEEKLY_ID = f"gpu:{H100.id}:getdeploying:weekly-median"


def company(ticker, tags=(), name=None):
    return domain.Company(ticker=ticker, name=name or ticker, exchange="X", segments=[], compute_exposure=domain.ComputeExposure(text="", tags=list(tags)),
                          related_facility_ids=[], peers=[], provenance=sample(T0))


def facility(fid, region, owners=(), operator=None, prov=None, status="operating"):
    return domain.Facility(id=fid, name=f"{fid} campus", lat=0, lon=0, region_id=region, status=status, owner_company_ids=list(owners), operator=operator,
                           capacity_mw=100.0, provenance=prov or sample(T0))


def filing(ticker, region_id, headline="10-K names Texas"):
    return domain.IntelItem(id=f"f-{ticker}", kind="filing", headline=headline, why_it_matters="", published_at=T0, observed_at=T0, severity=1,
                            classification="verified", entities=domain.IntelEntities(tickers=[ticker]), geography=domain.Geography(region_ids=[region_id]),
                            provenance=Provenance(kind="live", source="sec-edgar", observed_at=T0))


def offer(i, price, region="us-tx"):
    return domain.GpuOffer(id=f"o{i}", provider_id="p", region_id=region, gpu_model_id=H100.id, gpu_count=8, price_per_gpu_hour_usd=price,
                           availability="available", interruptible=False, contract="on-demand", provenance=sample(T0))


def weekly(values, last_week_end=datetime(2026, 11, 2, tzinfo=timezone.utc)):
    points = [domain.PriceObservation(series_id=WEEKLY_ID, t=last_week_end - timedelta(days=7 * (len(values) - 1 - i)), value=v, unit="USD/GPU-hour",
                                      provenance=Provenance(kind="archived", source="getdeploying", observed_at=last_week_end, licence="CC BY 4.0"))
              for i, v in enumerate(values)]
    return {WEEKLY_ID: domain.Series(id=WEEKLY_ID, unit="USD/GPU-hour", points=points)}


def comparable_median(offers):
    """An independent restatement through the adjust engine: the median comparable (regionally adjusted) ask."""
    return statistics.median(adjust_offer(o, H100, REGIONS[o.region_id]).adjusted for o in offers)


def cell(region, offers, series=None):
    return pressure.compute_cell(region, H100.id, offers=offers, gpu_models={H100.id: H100}, regions=REGIONS, series=series or {}, facilities=[],
                                 markets=[], feed=[], as_of=T0)


# -- exposure -------------------------------------------------------------------------------------------------

def test_every_sample_region_lists_every_company_with_a_role_and_a_worded_tie():
    data = load_sample_uncached()
    for rid in data.regions:
        rows = exposure.trace(rid, None, companies=data.companies, facilities=data.facilities, regions=data.regions)
        assert {e.ticker for e in rows} == set(data.companies), rid
        for e in rows:
            assert e.tie_kind == "authored" and e.tie_note == exposure.AUTHORED_TIE_NOTE and not e.documented
            assert e.tie_basis and e.role in {*exposure.ROLE_SENSITIVITY, exposure.ROLE_NONE}
            assert e.provenance.note == exposure.AUTHORED_TIE_NOTE
        # An authored sample facility is named as such in the words, never as a document.
        nbis = next(e for e in rows if e.ticker == "NBIS")
        if nbis.facility_ids:
            assert "authored sample record" in nbis.tie_basis and "owner of record" in nbis.tie_basis


def test_documented_ties_sort_first_and_say_what_documents_them():
    companies = {"CRWV": company("CRWV", ["gpu-cloud"], name="CoreWeave"), "NVDA": company("NVDA", ["accelerator-vendor"]),
                 "VRT": company("VRT", ["picks-and-shovels"]), "ZZZ": company("ZZZ", ["unrelated"])}
    facilities = [facility("fac-live", "us-tx", owners=["VRT"], prov=live("wikidata", T0)), facility("fac-sample", "us-tx", operator="CoreWeave")]
    rows = exposure.trace("us-tx", None, companies=companies, facilities=facilities, regions=REGIONS, filings=[filing("NVDA", "us-tx")])
    assert [e.ticker for e in rows] == ["VRT", "NVDA", "CRWV"]          # documented (owner record, filing), then authored
    vrt, nvda, crwv = rows
    assert vrt.tie_kind == "documented" and vrt.tie_note is None and "owner of record of fac-live campus (fac-live, operating, observed record)" == vrt.tie_basis
    assert nvda.tie_kind == "documented" and nvda.tie_basis == "SEC filing names the region: 10-K names Texas" and nvda.role == "accelerator-vendor"
    assert crwv.tie_kind == "authored" and crwv.documented is False and crwv.tie_note == exposure.AUTHORED_TIE_NOTE
    assert crwv.tie_basis == "operator of fac-sample campus (fac-sample, operating, authored sample record); profile role gpu-cloud reaches every region"
    assert all(e.position is None for e in rows)
    assert "ZZZ" not in {e.ticker for e in rows}                        # no tie of any kind: not listed


def test_the_europe_tag_reaches_only_eu_regions_and_the_role_is_the_sensitivity_role():
    c = company("NBIS", ["neocloud", "gpu-cloud", "europe", "miner-pivot"])
    assert exposure.company_role(c) == "gpu-cloud"
    assert exposure.company_role(company("X", ["unrelated"])) == exposure.ROLE_NONE
    regions = {**REGIONS, "eu-de": domain.Region(id="eu-de", name="Western Europe", country_code="DE", lat=0, lon=0)}
    eu = exposure.trace("eu-de", None, companies={"NBIS": company("NBIS", ["europe"])}, facilities=[], regions=regions)
    assert eu[0].tie_basis == "profile tag europe reaches Western Europe"
    assert exposure.trace("ap-jp", None, companies={"NBIS": company("NBIS", ["europe"])}, facilities=[], regions=regions) == []


@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(autocommit=False, autoflush=False, bind=engine)()
    try:
        yield session
    finally:
        session.close()


def test_a_signed_in_accounts_paper_position_is_overlaid_without_mutating_the_shared_answer(db, monkeypatch):
    user = User(name="Fund", email="fund@flopx.test", is_verified=True)
    db.add(user)
    db.commit()
    data = load_sample_uncached()
    rows = exposure.trace("us-tx", None, companies=data.companies, facilities=data.facilities, regions=data.regions)
    shared = {"region_id": "us-tx", "gpu_model_id": None, "exposures": rows, "freshness": None}
    monkeypatch.setattr(terminal, "exposure_view", lambda db_, region, gpu: shared)
    monkeypatch.setattr(terminal, "_data", lambda: data)
    # Anonymous, and a user with no desk yet: the shared answer, untouched, and no account row created by a read.
    assert terminal.exposure_view_for(db, None, "us-tx", None) is shared
    assert terminal.exposure_view_for(db, user, "us-tx", None) is shared
    assert db.query(PaperAccount).count() == 0
    account = broker.get_or_create_account(db, user.id)
    db.add(PaperPosition(account_id=account.id, ticker="CRWV", quantity=10, avg_cost=100.0))
    db.add(PaperPosition(account_id=account.id, ticker="NVDA", quantity=-2, avg_cost=150.0))
    db.commit()
    out = terminal.exposure_view_for(db, user, "us-tx", None)
    assert out is not shared and all(e.position is None for e in shared["exposures"])
    by = {e.ticker: e for e in out["exposures"]}
    crwv, nvda, vrt = by["CRWV"].position, by["NVDA"].position, by["VRT"].position
    assert crwv.side == "long" and crwv.quantity == 10 and crwv.avg_cost_usd == 100.0 and crwv.market_value_usd is not None and crwv.weight is not None
    assert nvda.side == "short" and nvda.quantity == -2
    assert vrt is None
    assert [e.ticker for e in out["exposures"]] == [e.ticker for e in shared["exposures"]]


# -- rental ----------------------------------------------------------------------------------------------------

def test_regional_asks_give_the_cells_median_and_premium_and_match_the_factor():
    offers = [offer(1, 2.0), offer(2, 2.4), offer(3, 3.0, region="ap-jp"), offer(4, 4.0, region="ap-jp")]
    c = cell("us-tx", offers)
    r = c.rental
    assert r.basis == "regional-asks" and r.ask_count == 2 and r.global_ask_count == 4
    pp = next(f for f in c.factors if f.factor == "price-premium")
    assert r.premium_pct == pytest.approx(pp.raw * 100, abs=0.051) and r.premium_label.endswith("%") and r.premium_pct < 0
    # Comparable asks (the adjust engine's regional and spec adjustments), the same numbers the factor reads.
    assert r.median_usd_per_gpu_hour == pytest.approx(comparable_median(offers[:2]), abs=1e-4)
    assert r.global_median_usd_per_gpu_hour == pytest.approx(comparable_median(offers), abs=1e-4)
    assert r.source == "flopx-sample-v1" and len(r.evidence) == 2 and "asks, not transactions" in r.note


def test_no_regional_asks_falls_back_to_the_global_median_and_the_archived_weekly_median_and_says_so():
    offers = [offer(3, 3.0, region="ap-jp"), offer(4, 4.0, region="ap-jp")]
    c = cell("us-tx", offers, series=weekly([2.9, 2.98]))
    r = c.rental
    assert r.basis == "global-fallback" and r.ask_count == 0 and r.premium_pct is None and r.premium_label == pressure.NO_REGIONAL_ASKS
    assert r.median_usd_per_gpu_hour == pytest.approx(comparable_median(offers), abs=1e-4) and r.median_usd_per_gpu_hour == r.global_median_usd_per_gpu_hour
    assert r.global_ask_count == 2
    assert r.weekly_median_usd_per_gpu_hour == 2.98 and r.weekly_week_ending == datetime(2026, 11, 2, tzinfo=timezone.utc)
    assert r.note.startswith("fallback: no live asks for h100-sxm-80gb in US Central (Dallas hub)")
    assert "GetDeploying's archived weekly median 2.9800 (week ending 2026-11-02)" in r.note and "no regional premium can be read" in r.note
    assert "getdeploying weekly median (CC BY 4.0, archived)" in r.source
    assert {p.kind for p in r.evidence} == {"sample", "archived"}
    # The price-premium factor is still "no data" for that cell: the fallback is a reading for the tab, never a factor input.
    assert next(f for f in c.factors if f.factor == "price-premium").normalised is None


def test_weekly_median_alone_stands_in_and_nothing_at_all_is_an_honest_none():
    only_weekly = cell("us-tx", [], series=weekly([3.1])).rental
    assert only_weekly.basis == "global-fallback" and only_weekly.median_usd_per_gpu_hour == 3.1 and only_weekly.global_median_usd_per_gpu_hour is None
    assert only_weekly.premium_label == pressure.NO_REGIONAL_ASKS and only_weekly.global_ask_count == 0
    nothing = cell("us-tx", []).rental
    assert nothing.basis == "none" and nothing.median_usd_per_gpu_hour is None and nothing.evidence == [] and nothing.observed_at is None
    assert nothing.premium_label == pressure.NO_REGIONAL_ASKS


def test_the_weekly_fallback_reads_only_the_weeks_known_at_the_cells_date():
    series = weekly([2.5, 2.98])     # weeks ending 2026-10-26 and 2026-11-02
    earlier = pressure.compute_cell("us-tx", H100.id, offers=[], gpu_models={H100.id: H100}, regions=REGIONS, series=series, facilities=[], markets=[], feed=[],
                                    as_of=datetime(2026, 10, 28, tzinfo=timezone.utc))
    assert earlier.rental.weekly_median_usd_per_gpu_hour == 2.5 and earlier.rental.weekly_week_ending == datetime(2026, 10, 26, tzinfo=timezone.utc)
