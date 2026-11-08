"""The paper desk's auth gate and order flow over the real dependency closure.

Real sqlite, a real session-bound JWT minted through the login path, the real
`user_dependency`: anonymous -> 401, a signed-in user -> 200 and a filled
order whose fill is labelled with the SAMPLE close it used (the live adapter is
stubbed unavailable, so no network is touched).
"""

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
    # Every other live adapter goes through the shared transport seam; a 503 there is an honest
    # "unavailable" (and never a sample stand-in), with nothing leaving the process:
    monkeypatch.setattr(_http, "TRANSPORT", _http.httpx.MockTransport(lambda request: _http.httpx.Response(503)))
    monkeypatch.setattr(yahoo, "fetch_bars", lambda t, r="6mo", i="1d": unavailable("yahoo-finance", "test-offline"))
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[deps.get_db] = lambda: db
    return TestClient(app)


def _auth(db) -> dict:
    user = User(name="Trader", email="trader@flopx.test", is_verified=True)
    db.add(user)
    db.commit()
    db.refresh(user)
    _, access, _ = create_session_and_tokens(db, user.id)
    return {"Authorization": f"Bearer {access['access_token']}"}


def test_desk_is_closed_to_anonymous_callers(client):
    assert client.get("/flopx/paper/account").status_code == 401
    assert client.post("/flopx/paper/orders", json={"ticker": "NVDA", "side": "buy", "quantity": 1}).status_code == 401
    assert client.post("/flopx/paper/positions/close-all", json={"confirm": True}).status_code == 401
    assert client.get("/flopx/paper/orders").status_code == 401


def test_a_garbage_token_is_refused(client):
    assert client.get("/flopx/paper/account", headers={"Authorization": "Bearer nope"}).status_code == 401


def test_order_flow_for_a_signed_in_user(client, db):
    headers = _auth(db)

    account = client.get("/flopx/paper/account", headers=headers)
    assert account.status_code == 200
    body = account.json()
    assert body["paper"] is True and body["cashUsd"] == 1_000_000.0 and body["positions"] == []

    order = client.post("/flopx/paper/orders", json={"ticker": "nvda", "side": "buy", "quantity": 10}, headers=headers)
    assert order.status_code == 200, order.text
    filled = order.json()
    assert filled["status"] == "filled" and filled["ticker"] == "NVDA" and filled["origin"] == "manual"
    assert filled["paper"] is True
    assert filled["fillProvenance"]["kind"] == "sample"          # honest: the fill used the sample close
    assert filled["fillPrice"] > 0

    after = client.get("/flopx/paper/account", headers=headers).json()
    assert len(after["positions"]) == 1 and after["positions"][0]["ticker"] == "NVDA"
    assert after["cashUsd"] == pytest.approx(1_000_000.0 - 10 * filled["fillPrice"], abs=0.01)

    rejected = client.post("/flopx/paper/orders", json={"ticker": "NVDA", "side": "buy", "notionalUsd": 5_000_000}, headers=headers)
    assert rejected.status_code == 200 and rejected.json()["status"] == "rejected"
    assert rejected.json()["reason"] == "insufficient-cash"

    orders = client.get("/flopx/paper/orders", headers=headers).json()
    assert [o["status"] for o in orders["orders"]] == ["rejected", "filled"]

    refused = client.post("/flopx/paper/positions/close-all", json={"confirm": False}, headers=headers)
    assert refused.status_code == 409 and refused.json()["detail"]["code"] == "flopx.confirmation-required"

    closed = client.post("/flopx/paper/positions/close-all", json={"confirm": True}, headers=headers)
    assert closed.status_code == 200
    assert [o["ticker"] for o in closed.json()["closed"]] == ["NVDA"]
    assert closed.json()["account"]["positions"] == []


def test_malformed_orders_are_422(client, db):
    headers = _auth(db)
    both = client.post("/flopx/paper/orders", json={"ticker": "NVDA", "side": "buy", "quantity": 1, "notionalUsd": 100}, headers=headers)
    assert both.status_code == 422
    limit_without_price = client.post("/flopx/paper/orders", json={"ticker": "NVDA", "side": "buy", "orderType": "limit", "quantity": 1}, headers=headers)
    assert limit_without_price.status_code == 422


def test_overview_shows_the_signed_in_users_paper_summary(client, db):
    headers = _auth(db)
    body = client.get("/flopx/overview", headers=headers).json()
    assert body["portfolioSummary"] == {"paper": True, "equityUsd": 1_000_000.0, "cashUsd": 1_000_000.0,
                                        "unrealisedPnlUsd": 0.0, "realisedPnlUsd": 0.0, "positions": 0}
