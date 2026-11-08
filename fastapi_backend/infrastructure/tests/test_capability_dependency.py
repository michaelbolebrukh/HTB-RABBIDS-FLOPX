"""The single authorization primitive: require_capability end to end.

Real JWTs, a real sqlite DB, and the REAL closure — covering the grant matrix
(no grant / grant / admin-implicit), the service-token path resolving to the
persisted service account, and the verification gate firing before the
capability check.
"""

import pytest
from fastapi import FastAPI
from sqlalchemy import create_engine
from sqlalchemy.orm import object_session, sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.testclient import TestClient

import infrastructure.dependencies as deps
from infrastructure.database import Base
from infrastructure.settings import settings as s
import platform.models  # noqa: F401 — registers User on Base.metadata
import product.models  # noqa: F401
from infrastructure.service_auth import service_account_email
from platform.capabilities import INBOX_READ, MARKETING_MANAGE
from platform.enums import Role
from platform.models import User
from platform.services.auth_services.auth_service import create_session_and_tokens


SERVICE_TOKEN = "svc_" + "s" * 40

app = FastAPI()

reader_dependency = deps.require_capability(INBOX_READ)
marketer_dependency = deps.require_capability(MARKETING_MANAGE)


@app.get("/needs-inbox-read")
def needs_inbox_read(user: reader_dependency):
    return {"email": user.email}


@app.get("/needs-marketing-manage")
def needs_marketing_manage(user: marketer_dependency):
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


def _make_user(db, email, *, role=Role.USER, capabilities=None, verified=True) -> User:
    user = User(name="X", email=email, role=role, capabilities=capabilities, is_verified=verified)
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


# -- session-JWT path ----------------------------------------------------------

def test_no_grant_is_refused(client, db_session):
    user = _make_user(db_session, "plain@x.com")
    assert client.get("/needs-inbox-read", headers=_auth(user)).status_code == 403


def test_grant_admits(client, db_session):
    user = _make_user(db_session, "reader@x.com", capabilities=[INBOX_READ])
    response = client.get("/needs-inbox-read", headers=_auth(user))
    assert response.status_code == 200
    assert response.json() == {"email": "reader@x.com"}


def test_admin_without_grant_admits_implicitly(client, db_session):
    admin = _make_user(db_session, "admin@x.com", role=Role.ADMIN, capabilities=None)
    assert client.get("/needs-inbox-read", headers=_auth(admin)).status_code == 200


def test_unverified_user_rejected_before_capability_check(client, db_session):
    # Even WITH the grant, an unverified account is refused by the verification
    # gate first — the structured cooldown detail proves which gate fired:
    user = _make_user(db_session, "new@x.com", capabilities=[INBOX_READ], verified=False)
    response = client.get("/needs-inbox-read", headers=_auth(user))
    assert response.status_code == 403
    assert "cooldown_seconds" in response.json()["detail"]


def test_missing_credentials_are_a_401(client):
    assert client.get("/needs-inbox-read").status_code == 401


# -- service-token path --------------------------------------------------------

@pytest.fixture
def service_token(monkeypatch):
    monkeypatch.setattr(s, "SERVICE_TOKENS", SERVICE_TOKEN)
    return {"Authorization": f"Bearer {SERVICE_TOKEN}"}


def test_service_token_admits_for_a_held_grant(client, db_session, service_token):
    _make_user(db_session, service_account_email(), capabilities=[INBOX_READ])
    response = client.get("/needs-inbox-read", headers=service_token)
    assert response.status_code == 200
    assert response.json() == {"email": service_account_email()}


def test_service_token_refused_for_a_grant_its_account_lacks(client, db_session, service_token):
    # No implicit admin for machines: the account passes only what it holds:
    _make_user(db_session, service_account_email(), capabilities=[INBOX_READ])
    assert client.get("/needs-marketing-manage", headers=service_token).status_code == 403


def test_service_token_without_seeded_account_fails_closed(client, db_session, service_token, caplog):
    with caplog.at_level("ERROR"):
        assert client.get("/needs-inbox-read", headers=service_token).status_code == 403
    assert any("not seeded" in record.message for record in caplog.records)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))


# -- registry validation at definition time ------------------------------------

def test_require_capability_rejects_an_unregistered_grant_string():
    """A typo'd gate is a startup error, not a silently admin-only endpoint.

    An unregistered string is held by no account, but `verify_capability` still
    short-circuits on admin — so without this check a mistyped gate would
    quietly narrow a surface to admins with nothing failing anywhere."""
    with pytest.raises(ValueError, match="Unknown capability"):
        deps.require_capability("inbox:reed")
    with pytest.raises(ValueError, match="Unknown capability"):
        deps.require_capability("")


def test_require_capability_accepts_every_registered_grant():
    from platform.capabilities import ALL_CAPABILITIES

    for capability in ALL_CAPABILITIES:
        assert deps.require_capability(capability) is not None
