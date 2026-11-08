"""One public read: /flopx/overview answers anonymously, in camelCase, with freshness."""

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
from product.flopx.sources import _http, yahoo
from product.routers import router


@pytest.fixture
def client(monkeypatch):
    # No test may touch the network: the live adapter's HTTP seam is replaced
    # with a failure so any accidental call surfaces as a test error.
    # Yahoo goes through the shared `_http` seam now, so the MockTransport below covers it too.
    # Every other live adapter goes through the shared transport seam; a 503 there is an honest
    # "unavailable" (and never a sample stand-in), with nothing leaving the process:
    monkeypatch.setattr(_http, "TRANSPORT", _http.httpx.MockTransport(lambda request: _http.httpx.Response(503)))
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(autocommit=False, autoflush=False, bind=engine)()
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[deps.get_db] = lambda: session
    try:
        yield TestClient(app)
    finally:
        session.close()


def test_overview_is_public_and_carries_freshness(client):
    response = client.get("/flopx/overview")
    assert response.status_code == 200
    body = response.json()
    assert set(body) >= {"indicators", "gpuSnapshot", "watchlist", "markets", "feed", "freshness"}
    assert body["portfolioSummary"] is None                      # anonymous: no account
    assert body["freshness"]["generatedAt"] and body["freshness"]["oldestObservedAt"]
    # The indicators are derived (their source names the method); everything else read is sample:
    assert body["freshness"]["sources"] == ["fcpi-v1:median-of-adjusted-asks", "flopx-sample-v1"]
    assert len(body["gpuSnapshot"]) == 6 and len(body["watchlist"]) == 8 and len(body["feed"]) == 10


def test_overview_indicators_are_labelled_derived_from_sample(client):
    body = client.get("/flopx/overview").json()
    fcpi = [i for i in body["indicators"] if i["id"].startswith("index:fcpi:")]
    assert len(fcpi) == 6
    for indicator in fcpi:
        assert indicator["provenance"]["kind"] == "derived"
        assert indicator["provenance"]["inputKinds"] == ["sample"]
        assert indicator["unit"] == "USD/GPU-hour" and indicator["route"].startswith("/gpu/")


def test_wire_shape_is_camel_case_only(client):
    body = client.get("/flopx/gpu/offers?gpu=h100-sxm-80gb").json()
    first = body["offers"][0]
    assert "pricePerGpuHourUsd" in first and "price_per_gpu_hour_usd" not in first
    assert first["adjustment"]["steps"][0]["name"] in {"contract", "interruptible", "reserved"}
    assert first["provenance"]["observedAt"].endswith("Z") or "+" in first["provenance"]["observedAt"]


def test_unknown_entities_are_404_with_a_code(client):
    response = client.get("/flopx/gpu/index/no-such-gpu")
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "flopx.unknown-entity"
    # 24 monthly points cannot support a 14-step horizon (needs 2 x 14 = 28):
    response = client.post("/flopx/forecast", json={"seriesId": "gpu:h100-sxm-80gb:on-demand:global:monthly", "horizon": 14})
    assert response.status_code == 422 and response.json()["detail"]["code"] == "flopx.insufficient-history"
    assert response.json()["detail"]["required_points"] == 28


def test_forecast_over_sample_history_is_flagged_exploratory(client):
    # The 180-day sample series is authored, so whatever the backtest says the forecast
    # cannot be anything but exploratory; the reason must say why.
    response = client.post("/flopx/forecast", json={"seriesId": "gpu:h100-sxm-80gb:on-demand:global", "horizon": 14})
    assert response.status_code == 200
    body = response.json()
    assert body["exploratory"] is True
    assert "sample-inputs-only" in body["exploratoryReasons"]


def test_company_bars_degrade_to_unavailable_not_sample(client, monkeypatch):
    from product.flopx.sources import unavailable
    monkeypatch.setattr(yahoo, "fetch_bars", lambda t, r="6mo", i="1d": unavailable("yahoo-finance", "test-offline"))
    body = client.get("/flopx/companies/NVDA/bars").json()
    assert body["bars"] == []
    assert body["unavailable"] == {"source": "yahoo-finance", "reason": "test-offline"}
    assert body["sampleSeriesId"] == "equity:NVDA:close"


def test_vast_rows_carry_the_market_reading_line_on_the_wire(client, monkeypatch):
    # Decision G9-b: a Vast.ai ask is shown, and the serialised row itself says what it is.
    import json
    from datetime import datetime, timezone
    from pathlib import Path

    from product.flopx.provenance import live
    from product.flopx.sources import available, vast

    recorded = Path(__file__).resolve().parents[2] / "flopx" / "tests" / "fixtures" / "recorded" / "vast" / "bundles.json"
    t = datetime(2026, 11, 7, 15, 51, 9, tzinfo=timezone.utc)
    rows = vast.parse(json.loads(recorded.read_text(encoding="utf-8"))["body"], t, t)
    monkeypatch.setattr(vast, "fetch_gpu_offers", lambda: available("vast-ai", rows, live("vast-ai", t, fetched_at=t)))
    body = client.get("/flopx/gpu/offers?gpu=h100-sxm-80gb").json()
    vast_rows = [o for o in body["offers"] if o["providerId"] == "vast-ai"]
    assert len(vast_rows) == 2
    assert all(o["provenance"]["note"].startswith("Vast.ai asks: a current market reading, not an index or benchmark") for o in vast_rows)
