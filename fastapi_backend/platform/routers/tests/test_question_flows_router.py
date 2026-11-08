"""The /flows surface: what is public, what needs an account, and what 404s.

Three router-level decisions are load-bearing and are checked here rather than in
the service tests:

  * the definition read is PUBLIC (a client may need the current version before
    an account exists) but exposes only the contract, never answers;
  * submission accepts an UNVERIFIED account, because answers captured before
    signup are flushed the moment the account exists — which is before the email
    code has been entered;
  * every lead endpoint 404s while the module is dormant, indistinguishably from
    an unknown flow, and returns 202 for any address when it is live so it can't
    be used to test which addresses exist.
"""

from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from pydantic import BaseModel, ConfigDict
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from sqlalchemy.orm import sessionmaker
from starlette.testclient import TestClient

import infrastructure.dependencies as deps
from infrastructure.database import Base
from infrastructure.rate_limiter import limiter
import platform.models  # noqa: F401 — registers the tables on Base.metadata
from platform import question_flows as qf
from platform.models import QuestionFlowLead, QuestionFlowResponse, User
from platform.routers.question_flows import router
from platform.services.question_flow_service import leads


AUTH = {"Authorization": "Bearer test-token"}


class Answers(BaseModel):
    model_config = ConfigDict(extra="forbid")
    year: str


@pytest.fixture
def db():
    # TestClient runs sync endpoints on a worker thread, and SQLite connections
    # are thread-affine — one shared, thread-tolerant connection keeps the test's
    # own writes and the endpoint's reads in the same database:
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
    try:
        yield
    finally:
        qf._FLOWS.clear()
        qf._FLOWS.update(saved)


@pytest.fixture(autouse=True)
def no_rate_limits():
    """Limits are asserted by their declarations, not by hammering a shared
    in-memory counter that would leak across tests."""
    previous = limiter.enabled
    limiter.enabled = False
    try:
        yield
    finally:
        limiter.enabled = previous


def _enable_leads():
    qf._FLOWS.pop("demo", None)
    qf.register_flow(qf.QuestionFlow(key="demo", version=1, answers_schema=Answers,
                                     title="Demo", lead_capture=True))


def _client(db, user: User | None = None) -> TestClient:
    app = FastAPI()
    app.state.limiter = limiter
    app.include_router(router)
    app.dependency_overrides[deps.get_db] = lambda: db
    if user is not None:
        app.dependency_overrides[deps.get_current_unverified_user] = lambda: user
    return TestClient(app)


def _user(db, *, verified=True) -> User:
    user = User(name="A", email="a@example.com", is_verified=verified)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


# -- definition (public) -------------------------------------------------------

def test_the_definition_is_readable_without_an_account(db):
    response = _client(db).get("/flows/demo")
    assert response.status_code == 200
    # The question set is public alongside the version: a client renders the
    # flow before it has an account. WHICH of them a given account still owes is
    # authenticated (GET /user/), and is not here:
    assert response.json() == {"flow_key": "demo", "version": 1, "title": "Demo",
                               "lead_capture": False,
                               "questions": ["year"], "required_questions": ["year"]}


def test_an_unknown_flow_definition_is_404(db):
    assert _client(db).get("/flows/nope").status_code == 404


# -- submission ----------------------------------------------------------------

def test_an_unverified_account_may_submit(db):
    """The pre-auth ordering depends on this: answers flush at account creation,
    before the email code is entered."""
    user = _user(db, verified=False)
    response = _client(db, user).post(
        "/flows/demo/responses", json={"version": 1, "answers": {"year": "final-year"}}, headers=AUTH,
    )
    assert response.status_code == 201
    body = response.json()
    assert body["flow_key"] == "demo"
    assert body["answers"] == {"year": "final-year"}
    assert body["completed_at"]
    assert db.query(QuestionFlowResponse).count() == 1


def test_submission_requires_an_account(db):
    response = _client(db).post("/flows/demo/responses",
                                json={"version": 1, "answers": {"year": "x"}})
    assert response.status_code == 401


def test_a_stale_client_version_no_longer_refuses_the_submission(db):
    """The version is a stamp, not a gate — see the service test of the same name.

    Refusing the whole body over a version integer is what made an additive
    question change destructive; the answers themselves are what get checked.
    """
    user = _user(db)
    response = _client(db, user).post(
        "/flows/demo/responses", json={"version": 99, "answers": {"year": "x"}}, headers=AUTH,
    )
    assert response.status_code == 201
    assert response.json()["version"] == 1
    assert response.json()["answers"] == {"year": "x"}


def test_a_partial_body_answers_only_what_it_carries_and_merges_the_rest(db):
    """The HTTP contract the granular gate is built on.

    A client asks the outstanding questions and posts just those; the response is
    the account's full merged position, so the client never has to reassemble it.
    """
    class TwoQuestions(BaseModel):
        model_config = ConfigDict(extra="forbid")
        year: str
        motivation: str

    user = _user(db)
    client = _client(db, user)
    qf._FLOWS.pop("demo", None)
    qf.register_flow(qf.QuestionFlow(key="demo", version=1, answers_schema=TwoQuestions,
                                     title="Demo"))

    first = client.post("/flows/demo/responses",
                        json={"version": 1, "answers": {"year": "masters"}}, headers=AUTH)
    assert first.status_code == 201
    assert first.json()["answers"] == {"year": "masters"}

    second = client.post("/flows/demo/responses",
                         json={"version": 1, "answers": {"motivation": "career"}}, headers=AUTH)
    assert second.status_code == 201
    assert second.json()["answers"] == {"year": "masters", "motivation": "career"}
    # Append-only: two rows, and the first still holds only what it answered:
    assert db.query(QuestionFlowResponse).count() == 2


def test_an_empty_answers_body_is_422(db):
    user = _user(db)
    response = _client(db, user).post(
        "/flows/demo/responses", json={"version": 1, "answers": {}}, headers=AUTH,
    )
    assert response.status_code == 422


def test_invalid_answers_are_422(db):
    user = _user(db)
    response = _client(db, user).post(
        "/flows/demo/responses", json={"version": 1, "answers": {"nope": "x"}}, headers=AUTH,
    )
    assert response.status_code == 422
    assert response.json()["detail"]["errors"]


def test_latest_returns_the_snapshot_or_a_distinct_404(db):
    user = _user(db)
    client = _client(db, user)
    assert client.get("/flows/demo/responses/latest", headers=AUTH).status_code == 404

    db.add(QuestionFlowResponse(user_id=user.id, flow_key="demo", version=1,
                                answers={"year": "x"}, completed_at=datetime.now(UTC)))
    db.commit()

    response = client.get("/flows/demo/responses/latest", headers=AUTH)
    assert response.status_code == 200
    assert response.json()["answers"] == {"year": "x"}


def test_latest_never_leaks_another_accounts_snapshot(db):
    other = User(name="B", email="b@example.com", is_verified=True)
    db.add(other)
    db.commit()
    db.add(QuestionFlowResponse(user_id=other.id, flow_key="demo", version=1,
                                answers={"year": "theirs"}, completed_at=datetime.now(UTC)))
    db.commit()

    response = _client(db, _user(db)).get("/flows/demo/responses/latest", headers=AUTH)
    assert response.status_code == 404


# -- lead endpoints: dormant by default ----------------------------------------

def test_lead_capture_is_404_while_dormant(db):
    response = _client(db).post("/flows/demo/leads",
                                json={"email": "lead@example.com", "version": 1})
    assert response.status_code == 404
    assert db.query(QuestionFlowLead).count() == 0


def test_lead_resume_is_404_while_dormant(db):
    response = _client(db).post("/flows/demo/leads/resume", json={"token": "l1.1.x"})
    assert response.status_code == 404


def test_a_dormant_flows_lead_endpoint_looks_exactly_like_an_unknown_flow(db):
    client = _client(db)
    dormant = client.post("/flows/demo/leads", json={"email": "a@example.com", "version": 1})
    unknown = client.post("/flows/nope/leads", json={"email": "a@example.com", "version": 1})
    assert dormant.status_code == unknown.status_code == 404
    assert dormant.json() == unknown.json()


# -- lead endpoints: live ------------------------------------------------------

def test_capture_is_always_202_and_stores_the_lead(db):
    _enable_leads()
    response = _client(db).post(
        "/flows/demo/leads", json={"email": "Lead@Example.com", "version": 1,
                                   "answers": {"year": "x"}},
    )
    assert response.status_code == 202
    lead = db.query(QuestionFlowLead).one()
    assert lead.email == "lead@example.com"


def test_capture_reports_nothing_about_the_address(db):
    """Both a brand-new and an already-captured address get the same 202."""
    _enable_leads()
    client = _client(db)
    first = client.post("/flows/demo/leads", json={"email": "lead@example.com", "version": 1})
    second = client.post("/flows/demo/leads", json={"email": "lead@example.com", "version": 1})
    assert first.status_code == second.status_code == 202
    assert first.content == second.content
    assert db.query(QuestionFlowLead).count() == 1


def test_resume_returns_the_masked_address_and_answers(db):
    _enable_leads()
    lead = QuestionFlowLead(email="lead@example.com", flow_key="demo", version=1,
                            answers={"year": "x"})
    db.add(lead)
    db.commit()
    db.refresh(lead)

    response = _client(db).post("/flows/demo/leads/resume",
                                json={"token": leads.mint_resume_token(lead.id)})
    assert response.status_code == 200
    assert response.json() == {"email_masked": "l***@example.com", "version": 1,
                               "answers": {"year": "x"}}


def test_a_forged_resume_token_is_404(db):
    _enable_leads()
    response = _client(db).post("/flows/demo/leads/resume", json={"token": "l1.1.deadbeef"})
    assert response.status_code == 404
