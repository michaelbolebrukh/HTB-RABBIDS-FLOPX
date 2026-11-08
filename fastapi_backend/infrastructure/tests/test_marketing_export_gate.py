"""The suppression export is gated by a capability, like its sibling.

`GET /marketing/suppressions/export` returns up to 5000 raw recipient addresses.
It used to be admitted by a timestamped `HASH_KEY` HMAC — a third authorization
primitive alongside capabilities and entitlements, reachable with no account and
no audit attribution, while `GET /marketing/suppressions` required
`marketing:manage` for exactly the same data. There is one primitive now.
"""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.testclient import TestClient

import infrastructure.dependencies as deps
import platform.models  # noqa: F401 — registers User on Base.metadata
import product.models  # noqa: F401
from infrastructure.database import Base
from platform.capabilities import MARKETING_MANAGE, USERS_READ
from platform.enums import Role
from platform.models import RefreshSession, User
from platform.routers.marketing import router as marketing_router
from platform.services import marketing_service as ms
from security import create_access_token


EXPORT_URL = "/marketing/suppressions/export"


@pytest.fixture
def db_session():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(autocommit=False, autoflush=False, bind=engine)()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def client(db_session):
    app = FastAPI()
    app.include_router(marketing_router)
    app.dependency_overrides[deps.get_db] = lambda: db_session
    ms.suppress_email(db_session, "unsubscribed@example.com")
    yield TestClient(app)
    app.dependency_overrides.clear()


def _user(db, email, *, role=Role.USER, capabilities=None) -> User:
    user = User(name="X", email=email, role=role, capabilities=capabilities, is_verified=True)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _session(db, user: User) -> RefreshSession:
    """A live session row, because the access token's `sid` claim is checked."""
    row = RefreshSession(
        user_id=user.id,
        current_jti=f"jti-{user.id}",
        expires_at=datetime.now(UTC) + timedelta(days=30),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _auth(user: User, db) -> dict:
    sid = _session(db, user).id
    return {"Authorization": f"Bearer {create_access_token(user.id, sid=str(sid))['access_token']}"}


def test_export_refuses_an_anonymous_caller(client):
    """No credential at all — the old HMAC query pair is gone, so this is a 401:"""
    assert client.get(EXPORT_URL).status_code == 401
    # And a request shaped like the retired signed call is no better:
    assert client.get(f"{EXPORT_URL}?ts=1&sig=deadbeef").status_code == 401


def test_export_refuses_an_account_without_the_grant(client, db_session):
    plain = _user(db_session, "plain@example.com")
    other_grant = _user(db_session, "other@example.com", capabilities=[USERS_READ])
    assert client.get(EXPORT_URL, headers=_auth(plain, db_session)).status_code == 403
    assert client.get(EXPORT_URL, headers=_auth(other_grant, db_session)).status_code == 403


def test_export_admits_the_marketing_manage_grant(client, db_session):
    manager = _user(db_session, "ops@example.com", capabilities=[MARKETING_MANAGE])
    response = client.get(EXPORT_URL, headers=_auth(manager, db_session))
    assert response.status_code == 200
    assert response.json() == {"emails": ["unsubscribed@example.com"]}


def test_export_admits_an_admin_implicitly(client, db_session):
    admin = _user(db_session, "admin@example.com", role=Role.ADMIN)
    assert client.get(EXPORT_URL, headers=_auth(admin, db_session)).status_code == 200


def test_export_and_its_sibling_share_one_gate(client, db_session):
    """Same data, same grant — no path to the list that the other refuses:"""
    plain = _user(db_session, "plain@example.com")
    manager = _user(db_session, "ops@example.com", capabilities=[MARKETING_MANAGE])
    for url in (EXPORT_URL, "/marketing/suppressions"):
        assert client.get(url, headers=_auth(plain, db_session)).status_code == 403
        assert client.get(url, headers=_auth(manager, db_session)).status_code == 200
