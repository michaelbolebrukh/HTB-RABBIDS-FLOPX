"""TODO TEMP (QA harness) — delete with POST /flows/{flow_key}/test/reset.

The reset is the ONE route that deletes append-only snapshots, so the things
worth proving are the gate and the blast radius, not the happy path:

  * the grant matrix, through the REAL require_capability closure and real JWTs
    (no credentials -> 401, an account without the grant -> 403, the grant and
    admin-implicit -> 200);
  * the reset is COMPLETE — responses, reminder claims and the address-keyed
    lead all go, because any one of them left behind makes the flow either
    un-re-runnable or half-reset;
  * the reset is SCOPED — another flow's rows and another account's rows survive;
  * the target-email path is non-enumerating (unknown address = 200 with zeros,
    never a 404) and refuses to touch a third party without confirm.
"""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from pydantic import BaseModel, ConfigDict
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.testclient import TestClient

import infrastructure.dependencies as deps
from infrastructure.database import Base
from infrastructure.rate_limiter import limiter
import platform.models  # noqa: F401 — registers the tables on Base.metadata
from platform import question_flows as qf
from platform.capabilities import ALL_CAPABILITIES, FLOWS_QA_RESET
from platform.enums import Role
from platform.models import (
    QuestionFlowLead,
    QuestionFlowReminder,
    QuestionFlowResponse,
    RefreshSession,
    User,
)
from platform.routers.question_flows import router
from security import create_access_token


class Answers(BaseModel):
    model_config = ConfigDict(extra="forbid")
    year: str


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(autocommit=False, autoflush=False, bind=engine)()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture(autouse=True)
def registry():
    saved = dict(qf._FLOWS)
    qf._FLOWS.clear()
    qf.register_flow(qf.QuestionFlow(key="demo", version=1, answers_schema=Answers, title="Demo"))
    qf.register_flow(qf.QuestionFlow(key="other", version=1, answers_schema=Answers, title="Other"))
    try:
        yield
    finally:
        qf._FLOWS.clear()
        qf._FLOWS.update(saved)


@pytest.fixture(autouse=True)
def no_rate_limits():
    previous = limiter.enabled
    limiter.enabled = False
    try:
        yield
    finally:
        limiter.enabled = previous


@pytest.fixture
def client(db):
    app = FastAPI()
    app.state.limiter = limiter
    app.include_router(router)
    # The capability dependency is NOT overridden: the point of this file is the
    # real closure, so only the session is swapped:
    app.dependency_overrides[deps.get_db] = lambda: db
    try:
        yield TestClient(app)
    finally:
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


def _seed_state(db, user: User, flow_key="demo") -> None:
    """One of everything the reset is supposed to remove."""
    db.add(QuestionFlowResponse(user_id=user.id, flow_key=flow_key, version=1,
                                answers={"year": "x"}, completed_at=datetime.now(UTC)))
    db.add(QuestionFlowReminder(user_id=user.id, flow_key=flow_key, stage=0))
    db.add(QuestionFlowLead(email=user.email, flow_key=flow_key, version=1, answers={"year": "x"}))
    db.commit()


# -- the registry contract -----------------------------------------------------

def test_the_grant_string_is_registered():
    """A grant not in ALL_CAPABILITIES silently gates nothing."""
    assert FLOWS_QA_RESET in ALL_CAPABILITIES


# -- the gate ------------------------------------------------------------------

def test_unauthenticated_is_401(client):
    assert client.post("/flows/demo/test/reset", json={}).status_code == 401


def test_an_account_without_the_grant_is_403(client, db):
    user = _user(db, "plain@x.com")
    _seed_state(db, user)
    assert client.post("/flows/demo/test/reset", json={}, headers=_auth(user, db)).status_code == 403
    # And nothing was deleted on the way to being refused:
    assert db.query(QuestionFlowResponse).count() == 1


def test_the_grant_admits(client, db):
    user = _user(db, "qa@x.com", capabilities=[FLOWS_QA_RESET])
    _seed_state(db, user)
    response = client.post("/flows/demo/test/reset", json={}, headers=_auth(user, db))
    assert response.status_code == 200
    assert response.json()["responses"] == 1


def test_an_admin_admits_implicitly(client, db):
    admin = _user(db, "admin@x.com", role=Role.ADMIN)
    _seed_state(db, admin)
    assert client.post("/flows/demo/test/reset", json={}, headers=_auth(admin, db)).status_code == 200


def test_another_capability_does_not_admit(client, db):
    """Grants are not interchangeable — the payments QA reset is a different key."""
    user = _user(db, "payments@x.com", capabilities=["payments:qa-reset"])
    assert client.post("/flows/demo/test/reset", json={}, headers=_auth(user, db)).status_code == 403


# -- completeness --------------------------------------------------------------

def test_the_reset_clears_everything_that_gates_a_re_run(client, db):
    admin = _user(db, "admin@x.com", role=Role.ADMIN)
    _seed_state(db, admin)

    response = client.post("/flows/demo/test/reset", json={}, headers=_auth(admin, db))
    assert response.json() == {"flow_key": "demo", "responses": 1, "reminders": 1, "leads": 1}
    assert db.query(QuestionFlowResponse).count() == 0
    assert db.query(QuestionFlowReminder).count() == 0
    assert db.query(QuestionFlowLead).count() == 0


def test_the_reset_is_idempotent(client, db):
    admin = _user(db, "admin@x.com", role=Role.ADMIN)
    _seed_state(db, admin)
    client.post("/flows/demo/test/reset", json={}, headers=_auth(admin, db))
    again = client.post("/flows/demo/test/reset", json={}, headers=_auth(admin, db))
    assert again.status_code == 200
    assert again.json()["responses"] == 0


# -- blast radius --------------------------------------------------------------

def test_another_flow_is_untouched(client, db):
    admin = _user(db, "admin@x.com", role=Role.ADMIN)
    _seed_state(db, admin, flow_key="demo")
    _seed_state(db, admin, flow_key="other")

    client.post("/flows/demo/test/reset", json={}, headers=_auth(admin, db))
    survivors = db.query(QuestionFlowResponse).all()
    assert [row.flow_key for row in survivors] == ["other"]
    assert db.query(QuestionFlowReminder).count() == 1
    assert db.query(QuestionFlowLead).count() == 1


def test_another_account_is_untouched(client, db):
    admin = _user(db, "admin@x.com", role=Role.ADMIN)
    other = _user(db, "other@x.com")
    _seed_state(db, admin)
    _seed_state(db, other)

    client.post("/flows/demo/test/reset", json={}, headers=_auth(admin, db))
    survivors = db.query(QuestionFlowResponse).all()
    assert [row.user_id for row in survivors] == [other.id]


def test_an_unknown_flow_is_404_and_deletes_nothing(client, db):
    admin = _user(db, "admin@x.com", role=Role.ADMIN)
    _seed_state(db, admin)
    assert client.post("/flows/nope/test/reset", json={}, headers=_auth(admin, db)).status_code == 404
    assert db.query(QuestionFlowResponse).count() == 1


# -- the target-email path -----------------------------------------------------

def test_targeting_another_account_requires_confirm(client, db):
    admin = _user(db, "admin@x.com", role=Role.ADMIN)
    other = _user(db, "other@x.com")
    _seed_state(db, other)

    response = client.post("/flows/demo/test/reset",
                           json={"email": "other@x.com"}, headers=_auth(admin, db))
    assert response.status_code == 400
    assert db.query(QuestionFlowResponse).count() == 1


def test_a_confirmed_target_is_reset(client, db):
    admin = _user(db, "admin@x.com", role=Role.ADMIN)
    other = _user(db, "other@x.com")
    _seed_state(db, admin)
    _seed_state(db, other)

    response = client.post("/flows/demo/test/reset",
                           json={"email": "OTHER@X.COM", "confirm": True}, headers=_auth(admin, db))
    assert response.status_code == 200
    assert response.json()["responses"] == 1
    # The caller's own rows are NOT collateral:
    survivors = db.query(QuestionFlowResponse).all()
    assert [row.user_id for row in survivors] == [admin.id]


def test_an_unknown_address_is_200_with_zeros_not_a_404(client, db):
    """Non-enumerating: 'no such account' and 'nothing to reset' are one answer."""
    admin = _user(db, "admin@x.com", role=Role.ADMIN)
    _user(db, "exists@x.com")

    unknown = client.post("/flows/demo/test/reset",
                          json={"email": "nobody@x.com", "confirm": True}, headers=_auth(admin, db))
    known = client.post("/flows/demo/test/reset",
                        json={"email": "exists@x.com", "confirm": True}, headers=_auth(admin, db))
    assert unknown.status_code == known.status_code == 200
    assert unknown.json() == known.json()


def test_an_explicit_null_email_is_the_self_reset(client, db):
    """`{"email": null}` must mean 'me', not 'an unnamed third party' - otherwise
    a client that always sends the key would trip the confirm gate."""
    admin = _user(db, "admin@x.com", role=Role.ADMIN)
    _seed_state(db, admin)
    response = client.post("/flows/demo/test/reset",
                           json={"email": None}, headers=_auth(admin, db))
    assert response.status_code == 200
    assert response.json()["responses"] == 1


def test_a_malformed_address_never_reaches_the_delete(client, db):
    admin = _user(db, "admin@x.com", role=Role.ADMIN)
    _seed_state(db, admin)
    response = client.post("/flows/demo/test/reset",
                           json={"email": "not-an-address", "confirm": True},
                           headers=_auth(admin, db))
    assert response.status_code == 422
    assert db.query(QuestionFlowResponse).count() == 1


def test_the_confirm_gate_fires_before_the_lookup(client, db):
    """An unconfirmed request must be refused identically for both addresses, or
    the 400/200 split becomes the enumeration signal the counts avoid."""
    admin = _user(db, "admin@x.com", role=Role.ADMIN)
    _user(db, "exists@x.com")

    unknown = client.post("/flows/demo/test/reset",
                          json={"email": "nobody@x.com"}, headers=_auth(admin, db))
    known = client.post("/flows/demo/test/reset",
                        json={"email": "exists@x.com"}, headers=_auth(admin, db))
    assert unknown.status_code == known.status_code == 400
    assert unknown.json() == known.json()
