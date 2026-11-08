"""Capability gates on managed-link list/create/update/delete; public resolve stays open."""

from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from slowapi.middleware import SlowAPIMiddleware
from starlette.testclient import TestClient

import infrastructure.dependencies as deps
from infrastructure.rate_limiter import limiter
from platform.capabilities import LINKS_MANAGE
from platform.enums import Role
from platform.models import ManagedLink, User
from platform.routers.links import router
from platform.services import link_service as ls


AUTH = {"Authorization": "Bearer test-token"}

PLAIN = User(role=Role.USER, capabilities=None)
MANAGER = User(role=Role.USER, capabilities=[LINKS_MANAGE])
ADMIN = User(role=Role.ADMIN)

_NOW = datetime(2026, 8, 6, tzinfo=timezone.utc)


def _row(**overrides) -> ManagedLink:
    base = dict(
        key="demo",
        destination="/team",
        label="Demo",
        is_active=True,
        created_at=_NOW,
        updated_at=_NOW,
    )
    base.update(overrides)
    return ManagedLink(**base)


@pytest.fixture(autouse=True)
def _stub_service(monkeypatch):
    monkeypatch.setattr(ls, "list_links", lambda db, include_inactive=True: [_row()])
    monkeypatch.setattr(ls, "create_link", lambda db, data: _row(key=data.key, destination=data.destination, label=data.label))
    monkeypatch.setattr(
        ls,
        "update_link",
        lambda db, key, data: _row(
            key=key,
            destination=data.destination or "/team",
            label=data.label,
            is_active=True if data.is_active is None else data.is_active,
        ),
    )
    monkeypatch.setattr(ls, "resolve_link", lambda db, key: _row(key=key))
    monkeypatch.setattr(ls, "delete_link", lambda db, key: None)


def _client(monkeypatch, user: User | None) -> TestClient:
    if user is not None:
        monkeypatch.setattr(deps, "get_current_verified_user", lambda db, token: user)
    app = FastAPI()
    app.state.limiter = limiter
    app.add_middleware(SlowAPIMiddleware)
    app.include_router(router)
    app.dependency_overrides[deps.get_db] = lambda: None
    return TestClient(app)


def test_public_resolve_needs_no_auth(monkeypatch):
    client = _client(monkeypatch, None)
    res = client.get("/links/demo")
    assert res.status_code == 200
    assert res.json()["key"] == "demo"
    assert res.json()["destination"] == "/team"


def test_list_requires_links_manage(monkeypatch):
    assert _client(monkeypatch, PLAIN).get("/links", headers=AUTH).status_code == 403
    assert _client(monkeypatch, MANAGER).get("/links", headers=AUTH).status_code == 200
    assert _client(monkeypatch, ADMIN).get("/links", headers=AUTH).status_code == 200


def test_create_requires_links_manage(monkeypatch):
    body = {"key": "card", "destination": "/contact", "label": "Card"}
    assert _client(monkeypatch, PLAIN).post("/links", headers=AUTH, json=body).status_code == 403
    ok = _client(monkeypatch, MANAGER).post("/links", headers=AUTH, json=body)
    assert ok.status_code == 200
    assert ok.json()["key"] == "card"


def test_update_requires_links_manage(monkeypatch):
    body = {"destination": "https://example.com"}
    assert _client(monkeypatch, PLAIN).patch("/links/demo", headers=AUTH, json=body).status_code == 403
    assert _client(monkeypatch, ADMIN).patch("/links/demo", headers=AUTH, json=body).status_code == 200


def test_delete_requires_links_manage(monkeypatch):
    assert _client(monkeypatch, PLAIN).delete("/links/demo", headers=AUTH).status_code == 403
    assert _client(monkeypatch, MANAGER).delete("/links/demo", headers=AUTH).status_code == 204
    assert _client(monkeypatch, ADMIN).delete("/links/demo", headers=AUTH).status_code == 204
