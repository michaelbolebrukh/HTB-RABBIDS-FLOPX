"""The second research-archive extract (decision G13): the founder's WRDS Compustat quarterly fundamentals, loaded
honestly, merged under SEC-first, derived figures public, raw rows for signed-in accounts only.

Every expectation is stated independently of the code: the quarterly capex figures are worked out by hand from the
fictional year-to-date values below, the growth and intensity ratios likewise, the row counts from the synthetic file.
No test reads the real extract (it is never in the repository); the values here are fictional, in Compustat's millions,
and include a fiscal-year rollover, a blank report date, a blank year-to-date cell and a first row that is quarter 4.
"""

import logging
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.testclient import TestClient

import infrastructure.cache as cache_module
import infrastructure.dependencies as deps
from infrastructure.cache import MemoryBackend
from infrastructure.database import Base
from infrastructure.rate_limiter import limiter
import platform.models  # noqa: F401
import product.models  # noqa: F401
from product import schemas
from product.flopx import archive, domain, fundamentals, terminal
from product.flopx.provenance import live
from product.flopx.sources import available, licences, sec, unavailable, yahoo
from product.flopx.tools import compustat_load, wrds_load
from product.models import Observation
from product.routers.flopx import router
from product.startup import STARTUP_TASKS, load_research_archive

UTC = timezone.utc
M = 1_000_000.0
NEXT_PWA_COPY = Path(__file__).resolve().parents[4] / "next-pwa" / "product" / "features" / "flopx" / "data" / "licences.json"
HEADER = "datadate,ticker,gvkey,revenue_q,capex_ytd,total_assets,report_date,fqtr\n"

# Fictional quarters for NVDA (fiscal year ending January) and VRT. Quarterly capex by hand, in millions:
#   NVDA 2024-01-31 Q4: first row, no prior quarter -> no capex point (skipped)
#   NVDA 2024-04-30 Q1: 10 (the year-to-date itself)
#   NVDA 2024-07-31 Q2: 25 - 10 = 15        2024-10-31 Q3: 45 - 25 = 20        2025-01-31 Q4: 70 - 45 = 25
#   NVDA 2025-04-30 Q1: 12 (rollover: the new fiscal year's year-to-date, NOT 12 - 70)
#   NVDA 2025-07-31 Q2: 30 - 12 = 18 (report date blank in the extract)
#   NVDA 2025-10-31 Q3: year-to-date blank -> no capex point (not a skip: nothing to derive from)
#   NVDA 2026-01-31 Q4: 80, but Q3 had no year-to-date -> skipped
#   VRT  2024-06-30 Q2: first row -> skipped;   VRT 2024-09-30 Q3: 9 - 5 = 4
SYNTHETIC = HEADER + "\n".join([
    "2024-01-31,NVDA,117768,100.0000,40.0000,1000.0000,2024-02-21,4",
    "2024-04-30,NVDA,117768,110.0000,10.0000,1050.0000,2024-05-22,1",
    "2024-07-31,NVDA,117768,120.0000,25.0000,1100.0000,2024-08-28,2",
    "2024-10-31,NVDA,117768,130.0000,45.0000,1150.0000,2024-11-20,3",
    "2025-01-31,NVDA,117768,150.0000,70.0000,1200.0000,2025-02-26,4",
    "2025-04-30,NVDA,117768,165.0000,12.0000,,2025-05-28,1",
    "2025-07-31,NVDA,117768,180.0000,30.0000,1300.0000,,2",
    "2025-10-31,NVDA,117768,195.0000,,1350.0000,2025-11-19,3",
    "2026-01-31,NVDA,117768,210.0000,80.0000,1400.0000,2026-02-25,4",
    "2024-06-30,VRT,036175,50.0000,5.0000,400.0000,2024-07-24,2",
    "2024-09-30,VRT,036175,55.0000,9.0000,410.0000,2024-10-23,3",
]) + "\n"
# Points: NVDA revenue 9 + total assets 8 + capex 6 = 23; VRT 2 + 2 + 1 = 5; 28 in all, 3 capex quarters skipped.
POINTS, SKIPPED = 28, 3
NVDA_CAPEX = {"2024-04-30": 10.0, "2024-07-31": 15.0, "2024-10-31": 20.0, "2025-01-31": 25.0, "2025-04-30": 12.0, "2025-07-31": 18.0}


@pytest.fixture
def db(monkeypatch):
    monkeypatch.setattr(cache_module.cache, "_backend", MemoryBackend(1024))
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(autocommit=False, autoflush=False, bind=engine)()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def extract(tmp_path) -> Path:
    path = tmp_path / "comp.csv"
    path.write_text(SYNTHETIC, encoding="utf-8")
    return path


def _member() -> SimpleNamespace:
    return SimpleNamespace(id=7, email="member@test.local")


def _silence_live(monkeypatch, *, sec_facts=None):
    """Yahoo and the SEC feeds answer unavailable (the shape the real adapters return on a 429); `sec_facts` is what
    the company-facts call returns, unavailable by default."""
    monkeypatch.setattr(yahoo, "fetch_bars", lambda ticker, range_="6mo", interval="1d": unavailable("yahoo-finance", "http-429"))
    monkeypatch.setattr(sec, "fetch_fundamentals", lambda cik, ticker=None: sec_facts or unavailable("sec-edgar", "http-429"))
    monkeypatch.setattr(sec, "fetch_filings", lambda cik, ticker=None, limit=10: unavailable("sec-edgar", "http-429"))
    monkeypatch.setattr(sec, "fetch_frame", lambda concept="capex", period=None: unavailable("sec-edgar", "http-429"))
    monkeypatch.setattr(terminal, "_live_closes", lambda data, ledger: {})


def _sec_facts_for_nvda():
    """A realistic SEC answer, built through the adapter's own parser from a company-facts payload: the latest fiscal
    quarter's revenue (212 million USD, period 2025-10-27 to 2026-01-25, filed 2026-02-25) and the fiscal year's
    revenue from the same 10-K (700 million, a twelve-month period)."""
    fetched = datetime(2026, 11, 8, 12, tzinfo=UTC)
    payload = {"facts": {"us-gaap": {"Revenues": {"units": {"USD": [
        {"start": "2025-10-27", "end": "2026-01-25", "val": 212_000_000, "form": "10-K", "fp": "FY", "fy": 2026, "filed": "2026-02-25"},
        {"start": "2025-01-27", "end": "2026-01-25", "val": 700_000_000, "form": "10-K", "fp": "FY", "fy": 2026, "filed": "2026-02-25"},
    ]}}}}}
    rows = sec.parse_facts(payload, "NVDA", fetched)
    # parse_facts keeps one row per period end, so the two share `end` and only the first survives: give the annual row its own end.
    annual = sec.parse_facts({"facts": {"us-gaap": {"Revenues": {"units": {"USD": [
        {"start": "2025-01-27", "end": "2026-01-26", "val": 700_000_000, "form": "10-K", "fp": "FY", "fy": 2026, "filed": "2026-02-25"},
    ]}}}}}, "NVDA", fetched)
    rows = rows + annual
    assert len(rows) == 2 and all(r.provenance.source == "sec-edgar" and r.provenance.kind == "live" for r in rows)
    return available("sec-edgar", rows, live("sec-edgar", max(r.provenance.observed_at for r in rows), fetched_at=fetched, licence="public domain"))


def _client(db, user):
    app = FastAPI()
    app.state.limiter = limiter
    app.include_router(router)
    app.dependency_overrides[deps.get_db] = lambda: db
    app.dependency_overrides[deps.get_current_user_or_none] = lambda: user
    return TestClient(app)


# -- the licence row ----------------------------------------------------------------------------------

def test_the_compustat_row_is_archivable_but_not_public_and_both_table_copies_match():
    row = licences.policy("wrds-compustat")
    assert row["name"] == "WRDS Compustat fundamentals quarterly" and row["use"] == "research-archive" and row["asserted"] is False
    assert row["licence"] == licences.policy("wrds-crsp")["licence"]                 # the same licence line as CRSP
    assert licences.archivable("wrds-compustat") is True and licences.public("wrds-compustat") is False
    assert licences.enabled("wrds-compustat") is True and licences.licence("wrds-compustat") == row["licence"]
    assert licences.public("sec-edgar") is True                                       # the gate reaches the research rows only
    assert "wrds-compustat" in {r["id"] for r in licences.rows()}
    if not NEXT_PWA_COPY.exists():
        pytest.skip("the next-pwa tree is not in this checkout")
    assert NEXT_PWA_COPY.read_bytes() == licences.TABLE_PATH.read_bytes()


# -- the derivation -------------------------------------------------------------------------------

def test_quarterly_capex_is_derived_within_a_fiscal_year_and_restarts_at_quarter_one(extract):
    rows = compustat_load.read_rows(extract)
    assert len(rows) == 11
    capex = compustat_load.quarterly_capex(rows)
    assert {d: capex[("NVDA", datetime.fromisoformat(d).date())] for d in NVDA_CAPEX} == NVDA_CAPEX
    assert capex[("VRT", datetime(2024, 9, 30).date())] == 4.0
    for missing in ("2024-01-31", "2025-10-31", "2026-01-31"):                        # a first-row Q4, a blank year-to-date, a gap before it
        assert ("NVDA", datetime.fromisoformat(missing).date()) not in capex
    assert ("VRT", datetime(2024, 6, 30).date()) not in capex
    # The rollover is the point: 2025-04-30 reads 12, never 12 - 70 = -58.
    assert capex[("NVDA", datetime(2025, 4, 30).date())] == 12.0


def test_points_are_in_usd_at_the_period_end_with_the_report_date_kept(extract):
    obs, extra, skipped = compustat_load.observations(compustat_load.read_rows(extract), loaded_at=datetime(2026, 11, 8, 12, tzinfo=UTC))
    assert len(obs) == POINTS and skipped == SKIPPED
    by_id = {(o.series_id, o.t.date().isoformat()): o for o in obs}
    revenue = by_id[("fundamental:NVDA:revenue_q", "2026-01-31")]
    assert revenue.value == 210.0 * M and revenue.unit == "USD"                                   # millions in the extract, USD stored
    assert revenue.t == datetime(2026, 1, 31, tzinfo=UTC)                                          # the fiscal period end
    assert revenue.provenance.observed_at == datetime(2026, 2, 25, tzinfo=UTC)                      # knowable from the report date
    assert revenue.provenance.kind == "archived" and revenue.provenance.source == "wrds-compustat"
    assert revenue.provenance.licence == licences.licence("wrds-compustat")
    assert "reported 2026-02-25" in revenue.provenance.note and "period 2025-11-01 to 2026-01-31" in revenue.provenance.note
    assert extra[id(revenue)] == {"compustat": {"gvkey": "117768", "fqtr": 4, "report_date": "2026-02-25", "capex_ytd": 80.0, "millions": 210.0}}
    capex = by_id[("fundamental:NVDA:capex_q", "2025-07-31")]
    assert capex.value == 18.0 * M and "derived as this quarter's year-to-date capex minus the previous quarter's" in capex.provenance.note
    assert capex.provenance.observed_at == capex.t and "report date not in the extract" in capex.provenance.note   # blank report date: period end, said so
    first_quarter = by_id[("fundamental:NVDA:capex_q", "2025-04-30")]
    assert first_quarter.value == 12.0 * M and "year-to-date figure itself (quarter 1)" in first_quarter.provenance.note
    assets = by_id[("fundamental:NVDA:total_assets", "2025-07-31")]
    assert assets.value == 1300.0 * M and "period instant to 2025-07-31" in assets.provenance.note
    assert ("fundamental:NVDA:total_assets", "2025-04-30") not in by_id and ("fundamental:NVDA:capex_q", "2025-10-31") not in by_id
    assert all(o.t != o.provenance.fetched_at for o in obs)                                         # never the load time
    assert compustat_load.quarter_start(datetime(2026, 2, 28).date()).isoformat() == "2025-12-01"


# -- load, reload, delete, CLI ---------------------------------------------------------------------

def test_a_second_load_writes_nothing_and_a_changed_value_is_rewritten(db, extract):
    first = compustat_load.load_file(db, extract)
    assert (first.rows, first.points, first.series, first.inserted, first.updated, first.unchanged, first.capex_skipped) == (11, POINTS, 6, POINTS, 0, 0, SKIPPED)
    assert first.tickers == {"NVDA": 23, "VRT": 5} and (first.first.isoformat(), first.last.isoformat()) == ("2024-01-31", "2026-01-31")
    assert db.query(Observation).count() == POINTS
    again = compustat_load.load_file(db, extract)
    assert (again.inserted, again.updated, again.unchanged) == (0, 0, POINTS) and db.query(Observation).count() == POINTS
    assert compustat_load.count_by_ticker(db) == {"NVDA": 23, "VRT": 5}
    corrected = extract.with_name("corrected.csv")
    corrected.write_text(SYNTHETIC.replace("2026-01-31,NVDA,117768,210.0000", "2026-01-31,NVDA,117768,215.0000"), encoding="utf-8")
    third = compustat_load.load_file(db, corrected)
    assert (third.inserted, third.updated, third.unchanged) == (0, 1, POINTS - 1) and db.query(Observation).count() == POINTS
    assert archive.archived_series(db, "fundamental:NVDA:revenue_q").points[-1].value == 215.0 * M
    dry = compustat_load.load_file(db, extract, dry_run=True)
    assert dry.dry_run and (dry.inserted, dry.unchanged) == (0, POINTS) and db.query(Observation).count() == POINTS
    assert "28 points over 6 series (2024-01-31 to 2026-01-31)" in first.line() and "3 capex quarters skipped" in first.line()


def test_delete_removes_every_compustat_row_and_leaves_crsp_and_sec_rows(db, extract, tmp_path):
    compustat_load.load_file(db, extract)
    crsp = tmp_path / "crsp.csv"
    crsp.write_text("date,ticker,permno,close,ret,volume,shares_out,cfacpr\n2024-01-02,NVDA,86580,199.35,0.03,1,1,40.0\n", encoding="utf-8")
    wrds_load.load_file(db, crsp)
    t = datetime(2026, 2, 25, tzinfo=UTC)
    sec_row = domain.PriceObservation("fundamental:NVDA:revenue", t, 212.0 * M, "USD", live("sec-edgar", t, fetched_at=t, licence="public domain"))
    assert archive.record(db, [sec_row]) == 1
    assert compustat_load.delete_all(db, dry_run=True) == POINTS and db.query(Observation).count() == POINTS + 2
    assert compustat_load.delete_all(db) == POINTS
    assert sorted(r.series_id for r in db.query(Observation).all()) == ["equity:NVDA:close", "fundamental:NVDA:revenue"]
    assert compustat_load.delete_all(db) == 0 and compustat_load.count_by_ticker(db) == {}
    assert wrds_load.count_by_ticker(db) == {"NVDA": 1}                                             # the CRSP loader's delete is its own


def test_the_cli_loads_reports_and_deletes(db, extract, monkeypatch, capsys):
    import infrastructure.database as database

    monkeypatch.setattr(database, "SessionLocal", lambda: db)
    assert compustat_load.main(["load", str(extract), "--dry-run"]) == 0 and db.query(Observation).count() == 0
    assert compustat_load.main(["load", str(extract)]) == 0 and db.query(Observation).count() == POINTS
    assert compustat_load.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "28 inserted, 0 updated, 0 unchanged" in out and "NVDA: 23" in out and "VRT: 5" in out
    assert compustat_load.main(["load", str(extract.with_name("missing.csv"))]) == 1
    assert compustat_load.main(["delete", "--dry-run"]) == 0 and db.query(Observation).count() == POINTS
    assert compustat_load.main(["delete"]) == 0 and db.query(Observation).count() == 0


# -- the startup task -----------------------------------------------------------------------------

def test_the_startup_task_loads_both_extracts_once_and_fails_soft(db, extract, tmp_path, caplog, monkeypatch):
    assert STARTUP_TASKS.index(load_research_archive) < STARTUP_TASKS.index(STARTUP_TASKS[-1])
    assert compustat_load.SECRET_FILE == Path("/etc/secrets/flopx-compustat.csv")
    monkeypatch.setattr(wrds_load, "SECRET_FILE", tmp_path / "no-crsp.csv")
    monkeypatch.setattr(compustat_load, "SECRET_FILE", extract)
    with caplog.at_level(logging.INFO):
        load_research_archive(db)
        load_research_archive(db)
    assert db.query(Observation).count() == POINTS                                                  # the second boot wrote nothing
    assert "wrds-crsp: no extract at" in caplog.text and "wrds-compustat: loaded 11 quarters as 28 points" in caplog.text
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="product.flopx.tools.compustat_load"):
        assert compustat_load.load_secret_file_if_present(db, tmp_path / "absent.csv") is None
    assert "no extract at" in caplog.text
    broken = tmp_path / "broken.csv"
    broken.write_text("datadate,ticker\n2024-01-31,NVDA\n", encoding="utf-8")
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="product.flopx.tools.compustat_load"):
        assert compustat_load.load_secret_file_if_present(db, broken) is None                       # no raise: the boot goes on
    assert "could not be loaded" in caplog.text and db.query(Observation).count() == POINTS


# -- the merge rule and the derived figures -----------------------------------------------------------

def test_sec_wins_the_period_both_report_and_compustat_supplies_the_rest():
    compustat = fundamentals.from_archive([
        domain.Series("fundamental:NVDA:revenue_q", "USD", [
            domain.PriceObservation("fundamental:NVDA:revenue_q", datetime(2025, 1, 31, tzinfo=UTC), 150.0 * M, "USD",
                                    _compustat_prov(datetime(2025, 2, 26, tzinfo=UTC), "period 2024-11-01 to 2025-01-31, reported 2025-02-26")),
            domain.PriceObservation("fundamental:NVDA:revenue_q", datetime(2026, 1, 31, tzinfo=UTC), 210.0 * M, "USD",
                                    _compustat_prov(datetime(2026, 2, 25, tzinfo=UTC), "period 2025-11-01 to 2026-01-31, reported 2026-02-25")),
        ]),
    ])
    assert [(f.concept, f.period_end, f.filed_at, f.value) for f in compustat] == [("revenue", "2025-01-31", "2025-02-26", 150.0 * M), ("revenue", "2026-01-31", "2026-02-25", 210.0 * M)]
    sec_rows = _sec_facts_for_nvda().records
    assert [fundamentals.period_class(f) for f in sec_rows] == ["quarter", "annual"]
    merged = fundamentals.merge(sec_rows, compustat)
    assert [(f.period_end, f.provenance.source, f.value) for f in merged] == [
        ("2025-01-31", "wrds-compustat", 150.0 * M),            # history only Compustat has
        ("2026-01-25", "sec-edgar", 212.0 * M),                 # the quarter both report: SEC stands, Compustat's 210 is dropped
        ("2026-01-26", "sec-edgar", 700.0 * M),                 # the annual figure is its own period class, replaced by nothing
    ]
    summary = fundamentals.summarise(merged)
    growth = next(f for f in summary["figures"] if f["concept"] == fundamentals.GROWTH)
    assert growth["value"] == pytest.approx(212.0 / 150.0 - 1.0) and growth["period_end"] == "2026-01-25" and growth["compared_to"] == "2025-01-31"
    assert growth["sources"] == ["sec-edgar", "wrds-compustat"] and growth["provenance"].kind == "derived"
    assert growth["provenance"].input_kinds == ("archived", "live") and growth["provenance"].observed_at == datetime(2026, 2, 25, tzinfo=UTC)
    latest = {row["concept"]: row for row in summary["latest"]}
    assert latest["revenue"]["source"] == "sec-edgar" and latest["revenue"]["value"] == 212.0 * M and latest["revenue"]["kind"] == "live"
    assert "capexIntensity" in summary["note"] and summary["sources"] == ["sec-edgar", "wrds-compustat"]
    # Without SEC the latest is Compustat's, and says so:
    alone = fundamentals.summarise(compustat)
    assert {row["concept"]: row["source"] for row in alone["latest"]} == {"revenue": "wrds-compustat"}
    assert alone["figures"][0]["value"] == pytest.approx(210.0 / 150.0 - 1.0) and alone["figures"][0]["sources"] == ["wrds-compustat"]
    # Authored sample rows carry no period clause, so they derive nothing:
    sample = terminal._data().companies["NVDA"].fundamentals
    assert sample and fundamentals.summarise(sample)["figures"] == [] and all(fundamentals.period_class(f) == "other" for f in sample)


def _compustat_prov(observed, note):
    return domain.Provenance(kind="archived", source="wrds-compustat", observed_at=observed, fetched_at=datetime(2026, 11, 8, tzinfo=UTC), licence="test", note=note)


# -- gating at the terminal and the routes --------------------------------------------------------------

def test_the_company_detail_gates_raw_rows_and_publishes_the_derived_figures(db, extract, monkeypatch):
    _silence_live(monkeypatch)
    compustat_load.load_file(db, extract)
    anonymous = terminal.company(db, "NVDA", None)
    assert {f.provenance.kind for f in anonymous["fundamentals"]} == {"sample"}                     # the raw rows an anonymous reader got before
    summary = anonymous["fundamentals_summary"]
    growth = next(f for f in summary["figures"] if f["concept"] == "revenueGrowthYoY")
    assert growth["value"] == pytest.approx(210.0 / 150.0 - 1.0) and growth["sources"] == ["wrds-compustat"]      # 40%, by hand
    intensity = next(f for f in summary["figures"] if f["concept"] == "capexIntensity")
    assert intensity["value"] == pytest.approx(18.0 / 180.0) and intensity["period_end"] == "2025-07-31"           # 10% of the quarter's revenue
    assert {row["concept"]: row["source"] for row in summary["latest"]} == {"capex": "wrds-compustat", "revenue": "wrds-compustat", "totalAssets": "wrds-compustat"}
    assert "wrds-compustat" in anonymous["freshness"].sources                                       # the derived figures name what they rest on
    member = terminal.company(db, "NVDA", _member())
    assert len(member["fundamentals"]) == 23 and {f.provenance.source for f in member["fundamentals"]} == {"wrds-compustat"}
    assert member["fundamentals_summary"]["figures"][0]["value"] == growth["value"]
    revenue_rows = [f for f in member["fundamentals"] if f.concept == "revenue"]
    assert [f.period_end for f in revenue_rows][-2:] == ["2025-10-31", "2026-01-31"] and revenue_rows[-1].filed_at == "2026-02-25"
    # A company with nothing in the archive reads as before for both readers:
    assert {f.provenance.kind for f in terminal.company(db, "CRWV", _member())["fundamentals"]} == {"sample"}
    assert terminal.company(db, "CRWV", None)["fundamentals_summary"]["figures"] == []


def test_the_company_detail_picks_sec_over_compustat_for_the_latest_quarter_and_names_the_source(db, extract, monkeypatch):
    _silence_live(monkeypatch, sec_facts=_sec_facts_for_nvda())
    compustat_load.load_file(db, extract)
    anonymous = terminal.company(db, "NVDA", None)
    assert [(f.period_end, f.provenance.source) for f in anonymous["fundamentals"]] == [("2026-01-25", "sec-edgar"), ("2026-01-26", "sec-edgar")]
    latest = {row["concept"]: row for row in anonymous["fundamentals_summary"]["latest"]}
    assert latest["revenue"]["source"] == "sec-edgar" and latest["revenue"]["value"] == 212.0 * M
    growth = next(f for f in anonymous["fundamentals_summary"]["figures"] if f["concept"] == "revenueGrowthYoY")
    assert growth["value"] == pytest.approx(212.0 / 150.0 - 1.0) and growth["sources"] == ["sec-edgar", "wrds-compustat"]
    member = terminal.company(db, "NVDA", _member())
    revenue_rows = [(f.period_end, f.provenance.source, f.value) for f in member["fundamentals"] if f.concept == "revenue"]
    assert revenue_rows[-3:] == [("2025-10-31", "wrds-compustat", 195.0 * M), ("2026-01-25", "sec-edgar", 212.0 * M), ("2026-01-26", "sec-edgar", 700.0 * M)]
    assert ("2026-01-31", "wrds-compustat", 210.0 * M) not in revenue_rows                          # the quarter SEC answered for is SEC's
    assert len(member["fundamentals"]) == 23 - 1 + 2
    assert set(member["freshness"].sources) >= {"sec-edgar", "wrds-compustat"}


def test_the_routes_gate_on_the_optional_user_dependency(db, extract, monkeypatch):
    _silence_live(monkeypatch)
    compustat_load.load_file(db, extract)
    anonymous = _client(db, None).get("/companies/NVDA")
    assert anonymous.status_code == 200
    body = anonymous.json()
    assert {f["provenance"]["kind"] for f in body["fundamentals"]} == {"sample"}
    figures = {f["concept"]: f for f in body["fundamentalsSummary"]["figures"]}
    assert figures["revenueGrowthYoY"]["value"] == pytest.approx(0.4) and figures["revenueGrowthYoY"]["sources"] == ["wrds-compustat"]
    assert figures["capexIntensity"]["provenance"]["kind"] == "derived" and figures["capexIntensity"]["provenance"]["inputKinds"] == ["archived"]
    member = _client(db, _member()).get("/companies/NVDA", headers={"Authorization": "Bearer x"}).json()
    assert len(member["fundamentals"]) == 23 and {f["provenance"]["source"] for f in member["fundamentals"]} == {"wrds-compustat"}
    assert member["fundamentals"][0]["provenance"]["licence"] == licences.licence("wrds-compustat")
    schemas.CompanyDetail.model_validate(member)
    # The raw series behind the figures follow the same gate through the series route:
    assert _client(db, None).get("/gpu/series", params={"id": "fundamental:NVDA:revenue_q", "range": "2000d"}).status_code == 404
    series = _client(db, _member()).get("/gpu/series", params={"id": "fundamental:NVDA:revenue_q", "range": "2000d"})   # a window reaching 2024-01-31
    assert series.status_code == 200 and len(series.json()["points"]) == 9 and series.json()["freshness"]["sources"] == ["wrds-compustat"]
    # The public list carries the derived summary and never a Compustat row:
    listing = _client(db, None).get("/companies").json()
    nvda = next(c for c in listing["companies"] if c["company"]["ticker"] == "NVDA")
    assert {f["provenance"]["kind"] for f in nvda["company"]["fundamentals"]} == {"sample"}
    assert nvda["fundamentalsSummary"]["latest"][0]["source"] == "wrds-compustat"
    assert next(f for f in nvda["fundamentalsSummary"]["figures"] if f["concept"] == "revenueGrowthYoY")["value"] == pytest.approx(0.4)
    vrt = next(c for c in listing["companies"] if c["company"]["ticker"] == "VRT")
    vrt_figures = {f["concept"]: f for f in vrt["fundamentalsSummary"]["figures"]}
    assert list(vrt_figures) == ["capexIntensity"] and vrt_figures["capexIntensity"]["value"] == pytest.approx(4.0 / 55.0)   # two quarters: a ratio, no year-earlier pair
    assert "no like-for-like quarterly pair for revenueGrowthYoY" in vrt["fundamentalsSummary"]["note"]
    assert "wrds-compustat" in listing["freshness"]["sources"]
    schemas.CompaniesResponse.model_validate(listing)
