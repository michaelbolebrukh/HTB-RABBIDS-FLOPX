"""The research archive (decision G13): the founder's WRDS CRSP closes loaded honestly and served to the right readers.

Every expectation is stated independently of the code: the split adjustment is worked out by hand, the session
close times from the New York session, the row counts from the synthetic file written here. No test reads the
real extract (it is never in the repository); the file below is fictional and includes a 4-for-1 split.
"""

import logging
from dataclasses import replace
from datetime import datetime, timedelta, timezone
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
from product.flopx import archive, domain, strategies, terminal
from product.flopx.provenance import live
from product.flopx.sources import licences, sec, unavailable, yahoo
from product.flopx.tools import wrds_load
from product.models import Observation
from product.routers.flopx import router
from product.startup import STARTUP_TASKS, load_research_archive

UTC = timezone.utc
NEXT_PWA_COPY = Path(__file__).resolve().parents[4] / "next-pwa" / "product" / "features" / "flopx" / "data" / "licences.json"
HEADER = "date,ticker,permno,close,ret,volume,shares_out,cfacpr\n"

# A fictional extract: SPLT pays a 4-for-1 split between its second and third day (the raw close drops from
# 101 to 25.5 while the cumulative factor drops from 4 to 1), NVDA's first row is the brief's worked example.
SYNTHETIC = HEADER + "\n".join([
    "2024-01-02,NVDA,86580,199.350000,0.030233,8890417,606000000,40.000000000000",
    "2024-01-03,NVDA,86580,212.470000,0.065814,22868175,606000000,40.000000000000",
    "2024-01-02,SPLT,99999,100.000000,0.010000,1000,500000,4.000000000000",
    "2024-01-03,SPLT,99999,101.000000,0.010000,1000,500000,4.000000000000",
    "2024-01-04,SPLT,99999,25.500000,0.009901,4000,2000000,1.000000000000",
    "2024-06-10,SPLT,99999,30.000000,0.010000,4000,2000000,1.000000000000",
    "2024-06-11,SPLT,99999,,,4000,2000000,1.000000000000",          # a blank close (a delisted day): skipped
]) + "\n"


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
    path = tmp_path / "crsp.csv"
    path.write_text(SYNTHETIC, encoding="utf-8")
    return path


def _member() -> SimpleNamespace:
    return SimpleNamespace(id=7, email="member@test.local")


def _no_live_sources(monkeypatch, *, gather: bool = True):
    """Every live adapter silent, so a composition reads the sample bag and the archive only. With `gather`
    False the fan-out itself returns nothing (the drift fixture's way); the company detail indexes its results
    positionally, so its test keeps the fan-out and silences the adapters at their seams instead."""
    if not gather:
        monkeypatch.setattr(terminal, "_gather", lambda calls: [])
    monkeypatch.setattr(yahoo, "fetch_bars", lambda ticker, range_="6mo", interval="1d": unavailable("yahoo-finance", "timeout"))
    monkeypatch.setattr(sec, "fetch_fundamentals", lambda cik, ticker=None: unavailable("sec-edgar", "timeout"))
    monkeypatch.setattr(sec, "fetch_filings", lambda cik, ticker=None, limit=10: unavailable("sec-edgar", "timeout"))
    monkeypatch.setattr(sec, "fetch_frame", lambda: unavailable("sec-edgar", "timeout"))


def _daily_closes(ticker: str, first: datetime, values: list[float]) -> str:
    """A fictional CRSP extract for one ticker: one row per weekday from `first`, factor 1, so adjusted == raw."""
    rows, day = [], first
    for v in values:
        while day.weekday() >= 5:
            day += timedelta(days=1)
        rows.append(f"{day.date().isoformat()},{ticker},1,{v:.6f},0.0,1000,1000,1.000000000000")
        day += timedelta(days=1)
    return HEADER + "\n".join(rows) + "\n"


# -- the licence row ----------------------------------------------------------------------------------

def test_the_wrds_row_is_archivable_but_not_public_and_both_table_copies_match():
    row = licences.policy("wrds-crsp")
    assert row["name"] == "WRDS CRSP daily stock file" and row["use"] == "research-archive" and row["asserted"] is False
    assert row["licence"] == "Licensed to the founder's institution for research; served to signed-in accounts only; removed after the hackathon"
    assert licences.archivable("wrds-crsp") is True and licences.public("wrds-crsp") is False
    assert licences.enabled("wrds-crsp") is True and licences.in_index("wrds-crsp") is True
    assert licences.licence("wrds-crsp") == row["licence"]                       # measured, not asserted: no caveat suffix
    for other in ("runpod", "yahoo-finance", "flopx-sample-v1", "a-source-nobody-decided"):
        assert licences.public(other) is True                                   # the gate reaches one row and nothing else
    assert "wrds-crsp" in {r["id"] for r in licences.rows()}
    if not NEXT_PWA_COPY.exists():
        pytest.skip("the next-pwa tree is not in this checkout")
    assert NEXT_PWA_COPY.read_bytes() == licences.TABLE_PATH.read_bytes()


# -- adjustment and observation time ------------------------------------------------------------------

def test_the_stored_close_is_split_adjusted_and_continuous_across_a_split(extract):
    rows = wrds_load.read_rows(extract)
    assert len(rows) == 6                                                        # the blank close is skipped
    obs, extra = wrds_load.observations(rows, loaded_at=datetime(2026, 11, 8, tzinfo=UTC))
    by_series = {}
    for o in obs:
        by_series.setdefault(o.series_id, []).append(o)
    nvda = by_series["equity:NVDA:close"]
    assert nvda[0].value == pytest.approx(199.35 / 40) and nvda[0].value == pytest.approx(4.98375)
    splt = [o.value for o in by_series["equity:SPLT:close"]]
    assert splt == pytest.approx([25.0, 25.25, 25.5, 30.0])                     # no 4x cliff where the raw close drops 101 -> 25.5
    assert max(abs(b / a - 1) for a, b in zip(splt, splt[1:])) < 0.2
    first = nvda[0]
    assert first.provenance.kind == "archived" and first.provenance.source == "wrds-crsp" and first.unit == "USD"
    assert first.provenance.licence == licences.licence("wrds-crsp")
    assert extra[id(first)] == {"crsp": {"permno": 86580, "close": 199.35, "cfacpr": 40.0}}   # the raw fields ride along for audit
    with pytest.raises(wrds_load.WrdsLoadError):
        wrds_load.adjusted_close(10.0, 0.0)


def test_each_point_is_stamped_at_the_session_close_in_utc_by_the_yahoo_rule(extract):
    # Worked by hand: New York opens 09:30 local; EST (UTC-5) in January puts the open at 14:30 UTC and the
    # 6.5 h regular session's close at 21:00 UTC; EDT (UTC-4) in June puts them at 13:30 and 20:00 UTC.
    assert wrds_load.session_close_utc(datetime(2024, 1, 2).date()) == datetime(2024, 1, 2, 21, 0, tzinfo=UTC)
    assert wrds_load.session_close_utc(datetime(2024, 6, 10).date()) == datetime(2024, 6, 10, 20, 0, tzinfo=UTC)
    assert wrds_load.session_close_utc(datetime(2018, 1, 2).date()) == datetime(2018, 1, 2, 21, 0, tzinfo=UTC)
    # The same helper the Yahoo adapter applies to a daily bar keyed at the open:
    open_ts = int(datetime(2024, 6, 10, 13, 30, tzinfo=UTC).timestamp())
    assert yahoo.bar_close_times([open_ts], "1d", {})[0] == int(datetime(2024, 6, 10, 20, 0, tzinfo=UTC).timestamp())
    obs, _ = wrds_load.observations(wrds_load.read_rows(extract), loaded_at=datetime(2026, 11, 8, 12, tzinfo=UTC))
    times = {(o.series_id, o.t.date().isoformat()): o.t for o in obs}
    assert times[("equity:SPLT:close", "2024-06-10")] == datetime(2024, 6, 10, 20, 0, tzinfo=UTC)
    assert times[("equity:NVDA:close", "2024-01-02")] == datetime(2024, 1, 2, 21, 0, tzinfo=UTC)
    assert all(o.t != o.provenance.fetched_at for o in obs)                      # never the load time


# -- load, reload, delete -----------------------------------------------------------------------------

def test_a_second_load_writes_nothing_and_a_changed_close_is_rewritten(db, extract):
    first = wrds_load.load_file(db, extract)
    assert (first.rows, first.series, first.inserted, first.updated, first.unchanged) == (6, 2, 6, 0, 0)
    assert first.tickers == {"NVDA": 2, "SPLT": 4} and (first.first.isoformat(), first.last.isoformat()) == ("2024-01-02", "2024-06-10")
    assert db.query(Observation).count() == 6
    again = wrds_load.load_file(db, extract)
    assert (again.inserted, again.updated, again.unchanged) == (0, 0, 6)
    assert db.query(Observation).count() == 6                                   # the row count is unchanged
    assert wrds_load.count_by_ticker(db) == {"NVDA": 2, "SPLT": 4}
    corrected = extract.with_name("corrected.csv")
    corrected.write_text(SYNTHETIC.replace("2024-01-03,NVDA,86580,212.470000", "2024-01-03,NVDA,86580,216.470000"), encoding="utf-8")
    third = wrds_load.load_file(db, corrected)
    assert (third.inserted, third.updated, third.unchanged) == (0, 1, 5) and db.query(Observation).count() == 6
    stored = archive.archived_series(db, "equity:NVDA:close")
    assert [round(p.value, 5) for p in stored.points] == [round(199.35 / 40, 5), round(216.47 / 40, 5)]
    dry = wrds_load.load_file(db, extract, dry_run=True)
    assert dry.dry_run and (dry.inserted, dry.unchanged) == (0, 6) and db.query(Observation).count() == 6


def test_delete_removes_every_wrds_row_and_nothing_else(db, extract):
    wrds_load.load_file(db, extract)
    t = datetime(2026, 11, 7, 12, tzinfo=UTC)
    other = domain.PriceObservation("offer:runpod-1:price", t, 2.4, "USD/GPU-hour", live("runpod", t, fetched_at=t, licence="test"))
    assert archive.record(db, [other]) == 1
    assert wrds_load.delete_all(db, dry_run=True) == 6 and db.query(Observation).count() == 7
    assert wrds_load.delete_all(db) == 6
    assert [r.series_id for r in db.query(Observation).all()] == ["offer:runpod-1:price"]
    assert wrds_load.delete_all(db) == 0 and wrds_load.count_by_ticker(db) == {}


def test_the_cli_loads_reports_and_deletes(db, extract, monkeypatch, capsys):
    import infrastructure.database as database

    monkeypatch.setattr(database, "SessionLocal", lambda: db)
    assert wrds_load.main(["load", str(extract), "--dry-run"]) == 0 and db.query(Observation).count() == 0
    assert wrds_load.main(["load", str(extract)]) == 0 and db.query(Observation).count() == 6
    assert wrds_load.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "6 inserted, 0 updated, 0 unchanged" in out and "NVDA: 2" in out and "SPLT: 4" in out
    assert wrds_load.main(["load", str(extract.with_name("missing.csv"))]) == 1
    assert wrds_load.main(["delete", "--dry-run"]) == 0 and db.query(Observation).count() == 6
    assert wrds_load.main(["delete"]) == 0 and db.query(Observation).count() == 0


# -- the startup task ---------------------------------------------------------------------------------

def test_the_startup_task_loads_a_mounted_file_once_and_fails_soft(db, extract, tmp_path, caplog):
    assert STARTUP_TASKS.index(load_research_archive) < STARTUP_TASKS.index(STARTUP_TASKS[-1])   # before the refresher starts
    assert wrds_load.SECRET_FILE == Path("/etc/secrets/flopx-crsp.csv")
    with caplog.at_level(logging.INFO, logger="product.flopx.tools.wrds_load"):
        assert wrds_load.load_secret_file_if_present(db, tmp_path / "absent.csv") is None
    assert db.query(Observation).count() == 0 and "no extract at" in caplog.text
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="product.flopx.tools.wrds_load"):
        report = wrds_load.load_secret_file_if_present(db, extract)
        again = wrds_load.load_secret_file_if_present(db, extract)
    assert report.inserted == 6 and again.inserted == 0 and db.query(Observation).count() == 6
    assert "wrds-crsp: loaded 6 rows over 2 series (2024-01-02 to 2024-06-10)" in caplog.text
    broken = tmp_path / "broken.csv"
    broken.write_text("date,ticker\n2024-01-02,NVDA\n", encoding="utf-8")
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="product.flopx.tools.wrds_load"):
        assert wrds_load.load_secret_file_if_present(db, broken) is None          # no raise: the boot goes on
    assert "could not be loaded" in caplog.text and db.query(Observation).count() == 6


# -- gating: who sees the raw points ------------------------------------------------------------------

def test_the_series_read_gives_anonymous_readers_the_sample_and_signed_in_readers_the_wrds_points(db, extract, monkeypatch):
    _no_live_sources(monkeypatch)
    wrds_load.load_file(db, extract)
    anonymous = terminal.series(db, "equity:NVDA:close", None)
    assert {p.provenance.kind for p in anonymous["points"]} == {"sample"}       # what an anonymous reader got before
    assert "wrds-crsp" not in anonymous["freshness"].sources
    member = terminal.series(db, "equity:NVDA:close", None, _member())
    assert [round(p.value, 5) for p in member["points"]] == [round(199.35 / 40, 5), round(212.47 / 40, 5)]
    assert {(p.provenance.kind, p.provenance.source) for p in member["points"]} == {("archived", "wrds-crsp")}
    assert member["freshness"].sources == ("wrds-crsp",)
    detail = member["freshness"].source_details[0]
    assert detail["licence"] == "Licensed to the founder's institution for research; served to signed-in accounts only; removed after the hackathon"
    # The archive's own reads honour the same flag, so no other composition can leak a point by accident:
    assert archive.archived_series(db, "equity:NVDA:close", public_only=True) is None
    assert archive.latest_before(db, "equity:NVDA:close", datetime(2030, 1, 1, tzinfo=UTC), public_only=True) is None
    assert archive.latest_before(db, "equity:NVDA:close", datetime(2030, 1, 1, tzinfo=UTC)).value == pytest.approx(212.47 / 40)
    # A ticker with no real data reads the same for both: sample.
    assert {p.provenance.kind for p in terminal.series(db, "equity:VRT:close", None, _member())["points"]} == {"sample"}


def test_the_route_gates_on_the_optional_user_dependency(db, extract, monkeypatch):
    _no_live_sources(monkeypatch)
    wrds_load.load_file(db, extract)

    def client(user):
        app = FastAPI()
        app.state.limiter = limiter
        app.include_router(router)
        app.dependency_overrides[deps.get_db] = lambda: db
        app.dependency_overrides[deps.get_current_user_or_none] = lambda: user
        return TestClient(app)

    anonymous = client(None).get("/gpu/series", params={"id": "equity:NVDA:close"})
    assert anonymous.status_code == 200
    assert {p["provenance"]["kind"] for p in anonymous.json()["points"]} == {"sample"}
    signed_in = client(_member()).get("/gpu/series", params={"id": "equity:NVDA:close"}, headers={"Authorization": "Bearer x"})
    assert signed_in.status_code == 200
    body = signed_in.json()
    assert [p["provenance"]["source"] for p in body["points"]] == ["wrds-crsp", "wrds-crsp"]
    assert body["points"][0]["t"] == "2024-01-02T21:00:00Z" and body["freshness"]["sources"] == ["wrds-crsp"]
    company_anon = client(None).get("/companies/NVDA").json()
    company_member = client(_member()).get("/companies/NVDA").json()
    assert company_anon["latestClose"]["provenance"]["kind"] == "sample"
    assert company_member["latestClose"]["provenance"]["source"] == "wrds-crsp" and company_member["latestClose"]["value"] == pytest.approx(212.47 / 40)


# -- the engines: real closes, the sample tail dropped ---------------------------------------------------

def test_the_evaluation_runs_on_the_real_closes_through_their_last_date_and_says_so(db, tmp_path, monkeypatch):
    _no_live_sources(monkeypatch, gather=False)
    # 120 fictional weekday closes for NVDA ending well before the authored 2026 sample series begins:
    values = [100.0 * (1.0 + 0.002 * i) + (3.0 if i % 7 == 0 else -1.5 if i % 5 == 0 else 0.0) for i in range(120)]
    path = tmp_path / "nvda.csv"
    path.write_text(_daily_closes("NVDA", datetime(2024, 1, 2), values), encoding="utf-8")
    wrds_load.load_file(db, path)
    sample_last = terminal._data().series["equity:NVDA:close"].points[-1].t
    assert sample_last.year == 2026

    out = terminal._evaluate_ticker_uncached(db, "NVDA")
    ev = out["evaluation"]
    last_real = archive.archived_series(db, "equity:NVDA:close").points[-1].t
    assert last_real.year == 2024 and ev.provenance.observed_at == last_real           # scored through the last real close
    assert ev.input_kinds == ["archived"]                                             # no sample point entered
    assert "wrds-crsp: WRDS CRSP daily stock file, scored through 2024-06-17" in ev.feature_sources["closes"]
    assert ev.reliability_note.startswith("out-of-sample on observed closes: scored through 2024-06-17 on wrds-crsp closes")
    assert "only the baseline is scored" in ev.reliability_note
    one_day = ev.horizons[0]
    assert one_day.origins == 59 and one_day.models[0].verdict == "reference"         # 120 closes, 60 warm-up, 1 ahead
    assert ev.features_dropped["gpu-price-change-30d"] == "no data for every origin"    # the authored compute series are dated 2026
    assert [p.source for p in out["provenances"]] == ["wrds-crsp"]
    # And the whole route: freshness names the source with its licence line.
    view = terminal.evaluation_view(db, "NVDA", "1d")
    body = schemas.EvaluationResponse.model_validate(view)
    assert body.freshness.sources == ["wrds-crsp"] and body.freshness.source_details[0].licence.startswith("Licensed to the founder's institution")
    # A ticker with no real data still evaluates on the sample bag, labelled sample:
    assert terminal._evaluate_ticker_uncached(db, "VRT")["evaluation"].input_kinds == ["sample"]


def test_price_only_backtests_run_on_the_real_closes_and_compute_rows_keep_the_authored_window(db, tmp_path, monkeypatch):
    _no_live_sources(monkeypatch, gather=False)
    values = [50.0 + 10.0 * (1 if (i // 15) % 2 else -1) * (i % 15) / 15 for i in range(150)]    # a sawtooth: the momentum rules act
    path = tmp_path / "crwv.csv"
    path.write_text(_daily_closes("CRWV", datetime(2024, 1, 2), values), encoding="utf-8")
    wrds_load.load_file(db, path)
    data = terminal._data()
    real_last = archive.archived_series(db, "equity:CRWV:close").points[-1].t

    portfolio = schemas.PortfolioEvaluationResponse.model_validate(terminal.evaluation_portfolio(db)).model_dump(by_alias=False)
    rows = {(r["strategy_id"], r["variant"]): r for r in portfolio["strategies"]}
    crwv_specs = [s.id for s in strategies.STRATEGIES.values() if s.universe[0] == "CRWV"]
    assert crwv_specs                                                                  # the divergence rule's universe leads with CRWV
    for sid in crwv_specs:
        price_only, compute = rows[(sid, strategies.VARIANT_PRICE_ONLY)], rows[(sid, strategies.VARIANT_COMPUTE)]
        assert price_only["end"] == real_last and price_only["start"].year == 2024      # walked the real trading days
        assert price_only["provenance"]["input_kinds"] == ["archived"] and "wrds-crsp closes" in price_only["provenance"]["note"]
        assert compute["end"] == data.series[strategies.INDEX_SERIES].points[-1].t       # the authored index window, as before
        assert compute["provenance"]["input_kinds"] == ["sample"] and "sample history" in compute["provenance"]["note"]
    assert portfolio["input_kinds"] == ["archived", "sample"]
    assert any("price-only rows run on observed closes (wrds-crsp) through 2024-07-29" in n for n in portfolio["notes"])
    assert any("no compute index covers the observed closes" in n for n in portfolio["notes"])
    assert "wrds-crsp" in portfolio["freshness"]["sources"]
    # With nothing in the archive the same read is the sample result it always was:
    plain = schemas.PortfolioEvaluationResponse.model_validate(terminal.evaluation_portfolio(None)).model_dump(by_alias=False)
    assert plain["input_kinds"] == ["sample"] and all(r["provenance"]["input_kinds"] == ["sample"] for r in plain["strategies"])
    assert any(n.startswith("sample closes") for n in plain["notes"])


def test_the_engine_drops_a_feature_with_no_data_from_its_input_kinds(monkeypatch):
    from product.flopx import evaluation

    t0 = datetime(2024, 1, 1, tzinfo=UTC)
    closes = domain.Series("equity:T:close", "USD", [domain.PriceObservation("equity:T:close", t0 + timedelta(days=i), 100.0 + (i % 3), "USD",
                                                                              replace(live("wrds-crsp", t0 + timedelta(days=i), fetched_at=t0 + timedelta(days=200)), kind="archived"))
                                                     for i in range(100)])
    later = datetime(2026, 5, 11, tzinfo=UTC)
    fcpi = domain.Series("fcpi", "USD/GPU-hour", [domain.PriceObservation("fcpi", later + timedelta(days=i), 2.0 + i / 10, "USD/GPU-hour",
                                                                          live("flopx-sample-v1", later + timedelta(days=i))) for i in range(10)])
    out = evaluation.evaluate_ticker("T", closes, fcpi=fcpi, pressure_scores=None, energy=None, feed=[])
    assert out.features_dropped["fcpi-change-30d"] == "no data for every origin"
    assert out.input_kinds == ["archived"] and out.provenance.input_kinds == ("archived",)
