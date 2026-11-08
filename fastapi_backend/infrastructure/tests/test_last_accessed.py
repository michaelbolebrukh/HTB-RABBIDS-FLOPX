"""users.last_accessed_at: stamped on the one authenticated path, and throttled.

Real JWTs, a real sqlite DB and the REAL dependency closure, so what is covered
is the behaviour every endpoint inherits: the first authenticated request stamps,
requests inside the throttle window cost NOTHING, the first request after the
window stamps again, and a stamping failure is invisible to the caller.
"""

from datetime import datetime, timedelta, UTC

import pytest
from fastapi import FastAPI
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, object_session, sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.testclient import TestClient

import infrastructure.dependencies as deps
from infrastructure.database import Base
from infrastructure.dependencies import LAST_ACCESSED_THROTTLE
from infrastructure.settings import settings as s
import platform.models  # noqa: F401 — registers User on Base.metadata
import product.models  # noqa: F401
from infrastructure.service_auth import service_account_email
from platform.capabilities import INBOX_READ
from platform.models import User
from platform.services.auth_services.auth_service import create_session_and_tokens


SERVICE_TOKEN = "svc_" + "l" * 40

app = FastAPI()

reader_dependency = deps.require_capability(INBOX_READ)


@app.get("/whoami")
def whoami(user: deps.user_dependency):
    return {"email": user.email}


@app.get("/needs-inbox-read")
def needs_inbox_read(user: reader_dependency):
    return {"email": user.email}


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
    app.dependency_overrides[deps.get_db] = lambda: db_session
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def _make_user(db, email="active@x.com", *, capabilities=None, last_accessed_at=None) -> User:
    user = User(
        name="X", email=email, capabilities=capabilities,
        is_verified=True, last_accessed_at=last_accessed_at,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _auth(user: User) -> dict:
    """Minting through the real login path, so the token carries a live `sid`.

    Access tokens are session-bound (`assert_session_live` rejects one whose
    RefreshSession is missing, revoked or expired), so a hand-rolled token with
    no session is refused by construction - as it should be. Going through
    `create_session_and_tokens` also keeps these suites honest: they exercise
    the same mint path production does.
    """
    _, access, _ = create_session_and_tokens(object_session(user), user.id)
    return {"Authorization": f"Bearer {access['access_token']}"}


def _stored(db, user_id: int):
    """Reading the column straight out of the database, never off the loaded
    instance — the point of every assertion here is what was PERSISTED:"""
    db.expire_all()
    return db.query(User.last_accessed_at).filter(User.id == user_id).scalar()


# -- the stamp -----------------------------------------------------------------

def test_first_authenticated_request_stamps(client, db_session):
    user = _make_user(db_session)
    assert user.last_accessed_at is None

    before = datetime.now(UTC).replace(tzinfo=None)
    assert client.get("/whoami", headers=_auth(user)).status_code == 200

    stamped = _stored(db_session, user.id)
    assert stamped is not None
    assert stamped >= before - timedelta(seconds=5)


def test_anonymous_request_stamps_nobody(client, db_session):
    user = _make_user(db_session)
    assert client.get("/whoami").status_code == 401
    assert _stored(db_session, user.id) is None


# -- the throttle --------------------------------------------------------------

def test_request_inside_the_window_does_not_write(client, db_session):
    fresh = datetime.now(UTC) - (LAST_ACCESSED_THROTTLE / 2)
    user = _make_user(db_session, last_accessed_at=fresh)
    original = _stored(db_session, user.id)

    assert client.get("/whoami", headers=_auth(user)).status_code == 200

    # Byte-identical, so the request provably issued no UPDATE rather than
    # rewriting the same instant:
    assert _stored(db_session, user.id) == original


def test_request_after_the_window_writes_again(client, db_session):
    stale = datetime.now(UTC) - (LAST_ACCESSED_THROTTLE + timedelta(minutes=1))
    user = _make_user(db_session, last_accessed_at=stale)
    original = _stored(db_session, user.id)

    assert client.get("/whoami", headers=_auth(user)).status_code == 200

    refreshed = _stored(db_session, user.id)
    assert refreshed is not None and refreshed > original


def test_a_burst_of_requests_costs_one_write(client, db_session):
    user = _make_user(db_session)
    headers = _auth(user)

    assert client.get("/whoami", headers=headers).status_code == 200
    first = _stored(db_session, user.id)

    for _ in range(5):
        assert client.get("/whoami", headers=headers).status_code == 200

    assert _stored(db_session, user.id) == first


# -- failure is invisible to the caller ----------------------------------------

def test_a_failed_stamp_never_breaks_the_request(client, db_session, monkeypatch, caplog):
    user = _make_user(db_session)
    # Signing in before the database is broken - the subject here is the STAMP
    # surviving a dead commit, not the login:
    headers = _auth(user)

    def boom(self, *args, **kwargs):
        raise RuntimeError("database went away")

    monkeypatch.setattr(Session, "commit", boom)

    with caplog.at_level("WARNING"):
        response = client.get("/whoami", headers=headers)

    # The user's actual call still succeeds, and the failure is logged rather
    # than swallowed silently:
    assert response.status_code == 200
    assert response.json() == {"email": "active@x.com"}
    assert any("last_accessed_at" in record.message for record in caplog.records)


# -- machines are not people ---------------------------------------------------

@pytest.fixture
def service_token(monkeypatch):
    monkeypatch.setattr(s, "SERVICE_TOKENS", SERVICE_TOKEN)
    return {"Authorization": f"Bearer {SERVICE_TOKEN}"}


def test_service_token_requests_do_not_stamp(client, db_session, service_token):
    # A shared machine principal stamped by every cron would read as
    # permanently active while saying nothing about anyone:
    service = _make_user(db_session, service_account_email(), capabilities=[INBOX_READ])

    assert client.get("/needs-inbox-read", headers=service_token).status_code == 200
    assert _stored(db_session, service.id) is None


def test_a_human_holding_the_grant_still_stamps(client, db_session):
    # The capability path is not an exception — it resolves humans through the
    # same get_user, so a granted account is stamped like any other:
    user = _make_user(db_session, "reader@x.com", capabilities=[INBOX_READ])

    assert client.get("/needs-inbox-read", headers=_auth(user)).status_code == 200
    assert _stored(db_session, user.id) is not None


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
