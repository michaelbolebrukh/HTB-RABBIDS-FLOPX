"""Revoking a session must invalidate its ACCESS tokens, not just its refresh cookie.

An access token is a self-contained signature, so nothing about a sign-out
reaches one already in the wild. Before this check a second tab kept a fully
privileged token for the rest of its TTL after the user signed out in the first
- reads and mutations alike - and "sign out everywhere" was unbuildable on top
of the revoke. These tests pin every state a session claim can be in, and close
with the end-to-end claim the whole change rests on: a real POST /auth/logout
makes an access token minted before it stop working.
"""

from datetime import datetime, timedelta, UTC

import pytest
from fastapi import FastAPI
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.testclient import TestClient

import infrastructure.dependencies as deps
from infrastructure.database import Base
import platform.models  # noqa: F401 - registers the models on Base.metadata
import product.models  # noqa: F401
from platform.models import RefreshSession, User
from platform.services.auth_services.auth_service import create_session_and_tokens
from security import create_access_token, set_all_auth_cookies
from security.cookies import CSRF_COOKIE_NAME, REFRESH_COOKIE_NAME


app = FastAPI()


@app.get("/whoami")
def whoami(user: deps.user_dependency):
    return {"email": user.email}


@app.get("/maybe")
def maybe(user: deps.optional_user_dependency):
    """Mirrors a page that serves anonymous and signed-in callers alike."""
    return {"email": user.email if user else None}


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


def _make_user(db, email="tabs@x.com") -> User:
    user = User(name="X", email=email, is_verified=True)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _make_session(db, user, *, revoked=False, expired=False) -> RefreshSession:
    now = datetime.now(UTC)
    row = RefreshSession(
        user_id=user.id,
        current_jti="jti-1",
        expires_at=now - timedelta(days=1) if expired else now + timedelta(days=30),
        revoked_at=now if revoked else None,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _auth(user, sid) -> dict:
    return {"Authorization": f"Bearer {create_access_token(user.id, sid=str(sid))['access_token']}"}


# -- the session claim's states ------------------------------------------------

def test_live_session_admits(client, db_session):
    user = _make_user(db_session)
    session = _make_session(db_session, user)
    assert client.get("/whoami", headers=_auth(user, session.id)).status_code == 200


def test_revoked_session_rejects_its_access_token(client, db_session):
    """The whole point: the token is still signed and unexpired, and must fail."""
    user = _make_user(db_session)
    session = _make_session(db_session, user)
    headers = _auth(user, session.id)
    assert client.get("/whoami", headers=headers).status_code == 200

    session.revoked_at = datetime.now(UTC)
    db_session.add(session)
    db_session.commit()

    # Same token, same signature, well inside its TTL - the other tab's request.
    assert client.get("/whoami", headers=headers).status_code == 401


def test_expired_session_rejects_its_access_token(client, db_session):
    user = _make_user(db_session)
    session = _make_session(db_session, user, expired=True)
    assert client.get("/whoami", headers=_auth(user, session.id)).status_code == 401


def test_unknown_session_id_is_rejected(client, db_session):
    user = _make_user(db_session)
    assert client.get("/whoami", headers=_auth(user, 999_999)).status_code == 401


def test_another_users_session_is_rejected(client, db_session):
    """The session must belong to the subject, or a token could ride someone else's."""
    owner = _make_user(db_session, "owner@x.com")
    other = _make_user(db_session, "other@x.com")
    session = _make_session(db_session, owner)
    assert client.get("/whoami", headers=_auth(other, session.id)).status_code == 401


def test_malformed_session_claim_is_rejected(client, db_session):
    user = _make_user(db_session)
    token = create_access_token(user.id, sid="not-a-number")["access_token"]
    response = client.get("/whoami", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401


def test_out_of_range_session_claim_is_rejected_not_crashed(client, db_session):
    """An absurdly wide claim is a 401, never a 500.

    SQLite - which this suite runs on - raises OverflowError rather than simply
    matching nothing. PostgreSQL returns no rows at any magnitude, so on the
    production dialect this passes either way; the guard exists to keep the two
    dialects from disagreeing inside a security check.
    """
    user = _make_user(db_session)
    token = create_access_token(user.id, sid=str(2 ** 70))["access_token"]
    response = client.get("/whoami", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401


def test_a_session_claim_is_mandatory_to_mint(db_session):
    """Fail-closed at the mint site: no sid means no token at all.

    An access token without a session claim would be permanently unrevocable, so
    the mint site refuses rather than the check admitting it.
    """
    user = _make_user(db_session)
    for bad in (None, ""):
        with pytest.raises(ValueError):
            create_access_token(user.id, sid=bad)


def test_a_token_that_slipped_through_without_a_sid_is_still_refused(client, db_session):
    """The other half of the pair: the CHECK fails closed too, not just the mint.

    `create_access_token` refuses to mint one of these, so reaching this branch
    means going under it to `_create_token` - which is the point. The mint-site
    guard is a future mistake's first line of defence; this is the second, and
    it is what makes "no sid" mean "refused" rather than "unrevocable forever".
    """
    from security.tokens import _create_token
    from platform.enums import TokenUse
    from infrastructure.settings import settings

    user = _make_user(db_session)
    _make_session(db_session, user)          # a live session exists; the CLAIM is missing

    for extra in (None, {"sid": None}):
        token, _ = _create_token(user.id, settings.TOKEN_TTL_TIMEDELTA, TokenUse.ACCESS, extra)
        response = client.get("/whoami", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 401, f"a token with extra={extra} was admitted"


# -- optional auth -------------------------------------------------------------

def test_optional_auth_degrades_to_anonymous_on_a_revoked_session(client, db_session):
    """A dead session must read as signed-out on these routes, not as an error."""
    user = _make_user(db_session)
    session = _make_session(db_session, user, revoked=True)
    response = client.get("/maybe", headers=_auth(user, session.id))
    assert response.status_code == 200
    assert response.json() == {"email": None}


# -- end to end ----------------------------------------------------------------

def test_signing_out_kills_an_access_token_minted_before_it(client, db_session):
    """The integration claim, with a real POST /auth/logout doing the revoking.

    Every other test here revokes by writing `revoked_at` by hand. This one runs
    the actual sign-out endpoint - CSRF validation, cookie transport and all -
    and then replays the access token the way a second tab would.
    """
    from platform.routers.auth import router as auth_router

    signout_app = FastAPI()
    signout_app.include_router(auth_router)
    signout_app.dependency_overrides[deps.get_db] = lambda: db_session

    user = _make_user(db_session, "e2e@x.com")
    _, access, rt = create_session_and_tokens(db_session, user.id)
    headers = {"Authorization": f"Bearer {access['access_token']}"}

    # The token works before the sign-out:
    assert client.get("/whoami", headers=headers).status_code == 200

    with TestClient(signout_app) as signout_client:
        # Placing the refresh + CSRF cookies exactly as a real login would:
        from fastapi import Response as _Response
        carrier = _Response()
        set_all_auth_cookies(carrier, rt["refresh_token"], rt["refresh_token_expiry"])
        jar = {
            c.split("=", 1)[0]: c.split("=", 1)[1].split(";", 1)[0]
            for c in carrier.headers.getlist("set-cookie")
        }
        signout_client.cookies.set(REFRESH_COOKIE_NAME, jar[REFRESH_COOKIE_NAME])
        signout_client.cookies.set(CSRF_COOKIE_NAME, jar[CSRF_COOKIE_NAME])

        logout = signout_client.post(
            "/auth/logout", headers={"X-CSRF-Token": jar[CSRF_COOKIE_NAME]},
        )
        assert logout.status_code < 400, logout.text

    signout_app.dependency_overrides.clear()

    # The other tab's token, untouched by the sign-out and now worthless:
    assert client.get("/whoami", headers=headers).status_code == 401
