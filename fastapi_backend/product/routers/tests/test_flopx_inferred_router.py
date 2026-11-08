"""The inferred-state, research and fund routes (architecture §10.11) over the real dependency closure.

Every live adapter sits behind a 503 MockTransport, so nothing leaves the
process and every payload is sample-derived; the assertions are about the wire:
status codes, camelCase, `statementClass` and `freshness` where §10.1 requires
them, the auth gate on fund and accept, and the ledger rule that a proposal
never becomes an order until it is accepted.
"""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.testclient import TestClient

import infrastructure.dependencies as deps
from infrastructure.database import Base
import platform.models  # noqa: F401
import product.models  # noqa: F401
from platform.models import User
from platform.services.auth_services.auth_service import create_session_and_tokens
from product.enums import DecisionAction
from product.flopx import strategies
from product.flopx.data.loader import load_sample_uncached
from product.flopx.paper import broker
from product.flopx.sources import _http, unavailable, yahoo
from product.routers import router


@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(autocommit=False, autoflush=False, bind=engine)()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def client(db, monkeypatch):
    # Yahoo goes through the shared `_http` seam now, so the MockTransport below covers it too.
    monkeypatch.setattr(_http, "TRANSPORT", _http.httpx.MockTransport(lambda request: _http.httpx.Response(503)))
    monkeypatch.setattr(yahoo, "fetch_bars", lambda t, r="6mo", i="1d": unavailable("yahoo-finance", "test-offline"))
    monkeypatch.setattr(yahoo, "fetch_latest_close", lambda t: None)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[deps.get_db] = lambda: db
    return TestClient(app)


def _auth(db) -> tuple[dict, User]:
    user = User(name="Fund", email="fund@flopx.test", is_verified=True)
    db.add(user)
    db.commit()
    db.refresh(user)
    _, access, _ = create_session_and_tokens(db, user.id)
    return {"Authorization": f"Bearer {access['access_token']}"}, user


def test_pressure_grid_and_cell_are_public_inferences_with_freshness(client):
    grid = client.get("/flopx/pressure").json()
    assert grid["statementClass"] == "inference" and grid["method"] == "cpx-v1" and len(grid["cells"]) == 48
    # Vast.ai is asked again under decision G9-b (a market reading feeding the grid), so a refusal names it too.
    assert grid["freshness"]["sources"] and {u["source"] for u in grid["freshness"]["unavailable"]} >= {"runpod", "akash", "vast-ai"}
    cell = grid["cells"][0]
    assert {"regionId", "gpuModelId", "score", "band", "factors", "contradictions", "uncertainty", "inputKinds", "provenance", "statementClass"} <= set(cell)
    assert cell["uncertainty"]["label"] in ("exploratory", "indicative") and cell["inputKinds"] == ["sample"]
    assert all(f["normalised"] is None or -1 <= f["normalised"] <= 1 for f in cell["factors"]) and len(cell["factors"]) == 6
    detail = client.get("/flopx/pressure/us-tx/h100-sxm-80gb").json()
    assert detail["historyBasis"] == "2 sample snapshots; FLOPX-collected history starts at first deploy" and len(detail["history"]) == 2
    assert detail["freshness"]["sources"] == ["flopx-sample-v1"]
    assert client.get("/flopx/pressure/nowhere/h100-sxm-80gb").status_code == 404
    assert client.get("/flopx/pressure?gpu=no-such-gpu").json()["detail"]["code"] == "flopx.unknown-entity"
    filtered = client.get("/flopx/pressure?gpu=h100-sxm-80gb&region=us-tx,us-va").json()
    assert [(c["regionId"], c["gpuModelId"]) for c in filtered["cells"]] == [("us-tx", "h100-sxm-80gb"), ("us-va", "h100-sxm-80gb")]


def test_supply_exposure_and_anomalies(client):
    supply = client.get("/flopx/supply?region=us-tx").json()
    row = supply["regions"][0]
    assert row["statementClass"] == "inference" and row["announcedMw"] > 0 and row["effectiveMw"] < row["operatingMw"] + row["underConstructionMw"]
    assert {a["name"] for a in row["assumptions"]} >= {"utilisation", "no-date-probability"}
    exposure = client.get("/flopx/exposure?region=us-tx").json()
    assert exposure["statementClass"] == "inference" and exposure["exposures"] and not any(e["documented"] for e in exposure["exposures"])   # sample facilities and profiles are authored links (V1 F7)
    assert all("not a measured beta" in e["sensitivityBasis"] for e in exposure["exposures"])
    anomalies = client.get("/flopx/anomalies").json()
    assert anomalies["anomalies"] and anomalies["cellsScanned"] > 0 and set(anomalies["rules"]) == {"bottleneck", "announcement-mismatch", "price-dispersion", "overhang", "divergence"}
    first = anomalies["anomalies"][0]
    assert first["statementClass"] == "inference" and first["id"].startswith("anomaly:") and 1 <= first["severity"] <= 5 and first["severityBasis"]
    detail = client.get(f"/flopx/anomalies/{first['id']}").json()
    assert detail["signal"]["id"] == f"signal:anomaly:{first['id']}" and detail["signal"]["skipped"]["reason"] in ("no-documented-exposure", "insufficient-sample", "strategy-not-followed")
    assert detail["cell"]["regionId"] == first["regionId"] and detail["effectiveSupply"]["regionId"] == first["regionId"] and detail["freshness"]
    assert client.get("/flopx/anomalies/anomaly:nope:x:y").status_code == 404
    kinds = {a["type"] for a in client.get("/flopx/anomalies?kind=divergence").json()["anomalies"]}
    assert kinds <= {"divergence"}


def test_infra_scenario_is_a_conditional_forecast_with_both_paths(client):
    body = {"regionId": "us-tx", "gpuModelId": "h100-sxm-80gb", "horizonDays": 45,
            "assumptions": [{"kind": "delay-facility", "facilityId": "fac-002", "months": 12}, {"kind": "energy-shock", "pct": 25}]}
    out = client.post("/flopx/scenario/infra", json=body).json()
    assert out["statementClass"] == "forecast" and out["conditional"] is True and out["label"] == "demonstrative"
    assert len(out["path"]) == 45 and {"baseline", "conditional", "lower", "upper", "baselineLower"} <= set(out["path"][0])
    assert out["applied"] == ["delay-facility:fac-002+12m", "energy-shock=+25.0%"] and out["scenarioSupply"]["effectiveMw"] < out["baselineSupply"]["effectiveMw"]
    assert out["assumptions"] == body["assumptions"] and out["provenance"]["kind"] == "derived" and out["freshness"]
    assert client.post("/flopx/scenario/infra", json={**body, "horizonDays": 400}).status_code == 422
    assert client.post("/flopx/scenario/infra", json={**body, "regionId": "nowhere"}).status_code == 404


def test_evaluation_reports_insufficient_sample_and_the_portfolio_shows_costs_and_variants(client):
    out = client.get("/flopx/research/evaluation?target=CRWV&horizon=5d,63d").json()
    ev = out["evaluations"][0]
    assert ev["statementClass"] == "inference" and ev["inputKinds"] == ["sample"] and "authored data" in ev["reliabilityNote"]
    by_h = {h["horizonDays"]: h for h in ev["horizons"]}
    assert set(by_h) == {5, 63} and by_h[63]["verdict"] == "insufficient-sample"
    assert [m["model"] for m in by_h[5]["models"]] == ["baseline", "augmented", "combined"]
    assert by_h[5]["verdict"] in ("improves", "inconclusive", "worsens", "insufficient-sample")
    assert client.get("/flopx/research/evaluation?target=NOPE").status_code == 404
    assert client.get("/flopx/research/evaluation?horizon=7d").status_code == 404
    portfolio = client.get("/flopx/research/evaluation/portfolio").json()
    assert portfolio["costBps"] == 5.0 and portfolio["inputKinds"] == ["sample"] and portfolio["benchmark"]["rule"].startswith("equal-weight")
    variants = {(s["strategyId"], s["variant"]) for s in portfolio["strategies"]}
    assert variants == {(sid, v) for sid in strategies.STRATEGIES for v in ("compute", "price-only")}
    assert all(s["mode"] == "backtest" and s["statementClass"] == "decision" and "turnoverRatio" in s for s in portfolio["strategies"])


def test_signals_are_public_for_anomalies_and_per_account_for_decisions(client, db):
    anon = client.get("/flopx/research/signals").json()
    assert anon["signals"] and all(s["id"].startswith("signal:anomaly:") and s["statementClass"] == "inference" for s in anon["signals"])
    first = anon["signals"][0]
    detail = client.get(f"/flopx/research/signals/{first['id']}").json()
    assert detail["anomaly"]["id"] == first["sourceId"] and detail["skipped"]["reason"] in ("no-documented-exposure", "insufficient-sample", "strategy-not-followed")
    # A strategy signal belongs to an account: anonymous is 401 (so the client refreshes and retries), not 404.
    anon_strategy = client.get("/flopx/research/signals/signal:divergence:CRWV:2026-01-01")
    assert anon_strategy.status_code == 401 and anon_strategy.json()["detail"]["code"] == "flopx.sign-in-required"
    headers, _ = _auth(db)
    assert client.get("/flopx/research/signals/signal:divergence:CRWV:2026-01-01", headers=headers).status_code == 404


def test_fund_and_accept_need_a_user_and_a_proposal_never_trades_until_accepted(client, db, monkeypatch):
    assert client.get("/flopx/fund/summary").status_code == 401
    assert client.post("/flopx/research/strategies/event-reaction/decisions/1/accept").status_code == 401
    headers, user = _auth(db)
    empty = client.get("/flopx/fund/summary", headers=headers).json()
    assert empty["paper"] is True and empty["equityCurve"] == [] and empty["inception"] is None and empty["targetPositions"] == [] and empty["fills"] == 0
    assert {c["id"] for c in empty["clusters"]} == {"gpu-cloud", "semis", "power-and-dc", "infrastructure"} and empty["clustersProvenance"]["kind"] == "sample"

    # A step through the API proposes and places nothing:
    stepped = client.post("/flopx/research/strategies/divergence/step", headers=headers).json()
    assert stepped["order"] is None and stepped["decision"]["status"] in ("proposed", "skipped") and stepped["decision"]["statementClass"] == "decision"
    assert client.get("/flopx/paper/orders", headers=headers).json()["orders"] == []

    # A proposal from a date on which the rule buys, then the accept route:
    data = load_sample_uncached()
    spec = strategies.STRATEGIES["event-reaction"]
    account = broker.get_or_create_account(db, user.id)
    at = next(p.t for p in data.series[strategies.INDEX_SERIES].points[61:] if strategies.decide(spec, data, p.t).action == DecisionAction.BUY.value)
    row, _ = strategies.step_forward(db, spec, data, account, at=at)
    # Read and accept while the proposal is inside its horizon (the sample's buy date is months back).
    monkeypatch.setattr(strategies, "utcnow", lambda: at + timedelta(days=1))
    assert client.get("/flopx/paper/orders", headers=headers).json()["orders"] == []
    summary = client.get("/flopx/fund/summary", headers=headers).json()
    assert [t["decisionId"] for t in summary["targetPositions"]] == [row.id] and summary["targetPositions"][0]["status"] == "proposed" and summary["account"]["positions"] == []

    accepted = client.post(f"/flopx/research/strategies/event-reaction/decisions/{row.id}/accept", headers=headers).json()
    assert accepted["decision"]["status"] == "filled" and accepted["order"]["status"] == "filled" and accepted["order"]["paper"] is True
    assert accepted["order"]["fillProvenance"]["kind"] == "sample"
    assert len(client.get("/flopx/paper/orders", headers=headers).json()["orders"]) == 1
    again = client.post(f"/flopx/research/strategies/event-reaction/decisions/{row.id}/accept", headers=headers)
    assert again.status_code == 422 and again.json()["detail"]["reason"] == "decision-not-proposed"
    assert client.post(f"/flopx/research/strategies/event-reaction/decisions/999/accept", headers=headers).status_code == 404

    funded = client.get("/flopx/fund/summary", headers=headers).json()
    assert funded["fills"] == 1 and funded["targetPositions"] == [] and len(funded["account"]["positions"]) == 1
    assert funded["equityCurve"] and funded["equityCurveProvenance"]["kind"] == "derived" and "sample" in funded["equityCurveProvenance"]["inputKinds"]
    assert set(funded["attributionByOrigin"]) == {"strategy:event-reaction"} and set(funded["attributionBySignalKind"]) == {"event"}
    assert sum(funded["exposureByCluster"].values()) == pytest.approx(funded["account"]["positions"][0]["weight"], abs=1e-4)
    assert funded["benchmark"]["tickers"] and funded["drawdownSeries"]
    mine = client.get("/flopx/research/signals", headers=headers).json()["signals"]
    chain = next(s for s in mine if s["id"] == row.signal_id)
    assert [d["status"] for d in chain["decisionRefs"]] == ["filled"] and chain["orders"][0]["status"] == "filled" and chain["forecastRef"]["statementClass"] == "forecast"
    assert client.get(f"/flopx/research/signals/{row.signal_id}", headers=headers).json()["id"] == row.signal_id


def test_forward_decisions_are_never_visible_to_anonymous_readers_or_other_accounts(client, db):
    # Falsifier V3 (2026-11-07): an unfiltered query served one account's proposals and
    # fills to signed-out readers and to other signed-in accounts.
    headers, user = _auth(db)
    data = load_sample_uncached()
    spec = strategies.STRATEGIES["event-reaction"]
    account = broker.get_or_create_account(db, user.id)
    at = next(p.t for p in data.series[strategies.INDEX_SERIES].points[61:] if strategies.decide(spec, data, p.t).action == DecisionAction.BUY.value)
    row, _ = strategies.step_forward(db, spec, data, account, at=at)

    mine = client.get("/flopx/research/strategies/event-reaction", headers=headers).json()
    assert [d["id"] for d in mine["forward"]["decisions"]] == [row.id]

    anonymous = client.get("/flopx/research/strategies/event-reaction").json()
    assert anonymous["forward"]["decisions"] == [] and anonymous["forward"]["proposed"] == 0
    assert all(s["forward"]["decisions"] == [] for s in client.get("/flopx/research/strategies").json()["strategies"])

    other = User(name="Other", email="other@flopx.test", is_verified=True)
    db.add(other)
    db.commit()
    db.refresh(other)
    _, access, _ = create_session_and_tokens(db, other.id)
    theirs = client.get("/flopx/research/strategies/event-reaction", headers={"Authorization": f"Bearer {access['access_token']}"}).json()
    assert theirs["forward"]["decisions"] == []


def test_infra_reads_are_observations_that_never_promote_announced_capacity(client):
    energy = client.get("/flopx/infra/energy").json()
    assert [z["zone"] for z in energy["zones"]] == ["ERCOT"] and energy["zones"][0]["statementClass"] == "observation" and len(energy["missing"]) == 7
    projects = client.get("/flopx/infra/projects?status=announced,planned").json()["projects"]
    assert projects and all(p["stage"] in ("announced", "planned") and p["facility"]["status"] == p["stage"] for p in projects)
    assert all(o["documented"] is True and o["basis"] == "facility-owner" for p in projects for o in p["documentedOwners"])
    grid = client.get("/flopx/infra/grid").json()
    assert len(grid["regions"]) == 8 and grid["constraintRule"].startswith("power-stress")
    texas = next(r for r in grid["regions"] if r["region"]["id"] == "us-tx")
    assert texas["seriesId"] == "energy:ERCOT:day-ahead" and texas["flagClass"] == "inference" and isinstance(texas["constrained"], bool)
    assert all(r["powerStressNormalised"] is None for r in grid["regions"] if r["region"]["id"] != "us-tx")


def test_ask_brief_is_composed_from_evidence_and_says_it_is_not_a_language_model(client):
    out = client.post("/flopx/ask", json={"query": "what is happening in texas"}).json()
    assert out["intent"] == "brief" and out["filters"]["target"] == "region:us-tx" and out["engine"] == "grammar"
    brief = out["brief"]
    assert brief["method"] == "composed-from-evidence-v1" and brief["notLanguageModel"] is True and brief["statementClass"] == "inference"
    assert brief["sentences"] and all(s["statementClass"] in ("observation", "inference", "forecast", "decision") for s in brief["sentences"])
    cited = {i for s in brief["sentences"] for i in s["evidenceIds"]}
    assert cited and cited <= {e["id"] for e in brief["evidence"]}
    assert any("not" in s["text"] and "utilisation" in s["text"] for s in brief["sentences"])
    assert brief["freshness"]["sources"]
    plain = client.post("/flopx/ask", json={"query": "cheapest H100 in Texas right now"}).json()
    assert plain["intent"] == "gpu" and plain["brief"] is None
    ticker = client.post("/flopx/ask", json={"query": "brief on CRWV"}).json()
    assert ticker["filters"]["target"] == "ticker:CRWV" and ticker["brief"]["targetKind"] == "ticker"


# -- falsifier V3 (2026-11-07) ---------------------------------------------------------------------

def test_close_all_needs_the_json_literal_true(client, db):
    headers, _ = _auth(db)
    for value in ("true", "yes", 1, "on"):
        response = client.post("/flopx/paper/positions/close-all", headers=headers, json={"confirm": value})
        assert response.status_code == 422, value
    assert client.post("/flopx/paper/positions/close-all", headers=headers, json={"confirm": False}).status_code == 409
    assert client.post("/flopx/paper/positions/close-all", headers=headers, json={"confirm": True}).status_code == 200


def test_a_replayed_step_is_labelled_and_bounded_to_the_sample(client, db, monkeypatch):
    headers, _ = _auth(db)
    data = load_sample_uncached()
    spec = strategies.STRATEGIES["divergence"]
    buy = next(p.t for p in data.series[strategies.INDEX_SERIES].points[61:] if strategies.decide(spec, data, p.t).action == DecisionAction.BUY.value)
    # The clock a day after the replayed date, inside the 20-day horizon: a replay of a date whose
    # horizon has already passed is recorded as lapsed, never as proposed (falsifier R2-33).
    monkeypatch.setattr(strategies, "utcnow", lambda: buy + timedelta(days=1))
    replay = client.post("/flopx/research/strategies/divergence/step", headers=headers, json={"asOf": buy.date().isoformat()}).json()
    assert replay["replay"] is True and replay["order"] is None
    assert replay["decision"]["status"] == "proposed" and replay["decision"]["rationale"]["replayOf"] == buy.date().isoformat()
    assert client.get("/flopx/paper/orders", headers=headers).json()["orders"] == []
    now = client.post("/flopx/research/strategies/divergence/step", headers=headers).json()
    assert now["replay"] is False and "replayOf" not in now["decision"]["rationale"]
    out = client.post("/flopx/research/strategies/divergence/step", headers=headers, json={"asOf": "2020-01-01"})
    assert out.status_code == 422 and out.json()["detail"]["reason"] == "replay-out-of-range"


def test_the_fund_states_the_provenance_and_risk_of_what_it_shows(client, db, monkeypatch):
    headers, user = _auth(db)
    data = load_sample_uncached()
    spec = strategies.STRATEGIES["divergence"]
    account = broker.get_or_create_account(db, user.id)
    at = next(p.t for p in data.series[strategies.INDEX_SERIES].points[61:] if strategies.decide(spec, data, p.t).action == DecisionAction.BUY.value)
    row, _ = strategies.step_forward(db, spec, data, account, at=at)
    monkeypatch.setattr(strategies, "utcnow", lambda: at + timedelta(days=1))
    summary = client.get("/flopx/fund/summary", headers=headers).json()
    [target] = summary["targetPositions"]
    assert target["decisionId"] == row.id and target["provenance"]["kind"] == "derived" and target["provenance"]["inputKinds"] == ["sample"]
    assert target["risk"]["measure"] == "one-sigma-move-over-horizon" and target["risk"]["oneSigmaUsd"] > 0 and target["expiresAt"]
    assert summary["accountProvenance"] is None                                    # cash only, nothing marked
    desk = client.get("/flopx/paper/account", headers=headers).json()
    assert desk["maxDrawdownPct"] == summary["maxDrawdownPct"] == summary["account"]["maxDrawdownPct"]
    # Past the horizon the proposal is expired: the fund no longer offers it and accept refuses it.
    monkeypatch.setattr(strategies, "utcnow", lambda: at + timedelta(days=spec_horizon(spec) + 1))
    assert client.get("/flopx/fund/summary", headers=headers).json()["targetPositions"] == []
    refused = client.post(f"/flopx/research/strategies/divergence/decisions/{row.id}/accept", headers=headers)
    assert refused.status_code == 422 and refused.json()["detail"]["reason"] == "decision-expired"


def spec_horizon(spec) -> int:
    return strategies.HORIZON_DAYS[spec.id]
