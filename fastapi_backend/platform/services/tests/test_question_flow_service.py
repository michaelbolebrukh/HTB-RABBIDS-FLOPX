"""Snapshot capture, the gate reads, and the dormant lead module.

The properties under test are the ones the rest of the system relies on:
append-only history, a server-stamped completion timestamp the client cannot
influence, a stale client caught per question rather than wholesale, answers
never echoed back in an error, latest-wins summaries, and a lead store that is
genuinely inert unless a flow opts in.

Per-question completion — the property that lets a question be added without
re-asking the rest — has its own file, test_question_flow_granularity.py.
"""

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import BaseModel, ConfigDict
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from infrastructure.database import Base
import platform.models  # noqa: F401 — registers the tables on Base.metadata
from platform import question_flows as qf
from platform.exceptions import (
    FlowAnswersInvalidException,
    FlowNotFoundException,
    InvalidResumeTokenException,
)
from platform.models import QuestionFlowLead, QuestionFlowResponse, User
from platform.services import question_flow_service as qfs
from platform.services.question_flow_service import leads, service


class Answers(BaseModel):
    model_config = ConfigDict(extra="forbid")
    year: str
    note: str = ""


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(autocommit=False, autoflush=False, bind=engine)()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture(autouse=True)
def registry():
    """One registered flow per test, isolated from the product's real flows."""
    saved = dict(qf._FLOWS)
    qf._FLOWS.clear()
    qf.register_flow(qf.QuestionFlow(key="demo", version=1, answers_schema=Answers, title="Demo"))
    try:
        yield
    finally:
        qf._FLOWS.clear()
        qf._FLOWS.update(saved)


def _register(**overrides):
    """Re-registers "demo" with different settings (e.g. a bumped version)."""
    defaults = dict(key="demo", version=1, answers_schema=Answers, title="Demo")
    defaults.update(overrides)
    qf._FLOWS.pop(defaults["key"], None)
    qf.register_flow(qf.QuestionFlow(**defaults))


def _user(db, email="a@example.com") -> User:
    user = User(name="A", email=email, is_verified=True)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


# -- submit --------------------------------------------------------------------

def test_submit_stores_a_normalized_snapshot_with_a_server_stamped_timestamp(db):
    user = _user(db)
    before = datetime.now(UTC)

    snapshot = qfs.submit_response(db, user, "demo", 1, {"year": "final-year"})

    assert snapshot.flow_key == "demo"
    assert snapshot.version == 1
    # Normalised: the schema's default filled in, so every snapshot of a version
    # has one shape:
    assert snapshot.answers == {"year": "final-year", "note": ""}
    stamped = snapshot.completed_at
    stamped = stamped if stamped.tzinfo else stamped.replace(tzinfo=UTC)
    assert stamped >= before.replace(microsecond=0)


def test_completed_at_cannot_be_supplied_by_the_client(db):
    # There is no client-facing path to it at all: an extra key is refused by the
    # flow schema, so the staleness fact stays server-owned:
    user = _user(db)
    with pytest.raises(FlowAnswersInvalidException):
        qfs.submit_response(db, user, "demo", 1,
                            {"year": "final-year", "completed_at": "1999-01-01T00:00:00Z"})


def test_submitting_appends_rather_than_replacing(db):
    user = _user(db)
    qfs.submit_response(db, user, "demo", 1, {"year": "first-year"})
    qfs.submit_response(db, user, "demo", 1, {"year": "final-year"})

    rows = db.query(QuestionFlowResponse).order_by(QuestionFlowResponse.id).all()
    assert len(rows) == 2
    # The earlier answer is still there — the table is a history, not a profile:
    assert rows[0].answers["year"] == "first-year"
    assert qfs.latest_response(db, user.id, "demo").answers["year"] == "final-year"


def test_unknown_flow_is_a_404(db):
    with pytest.raises(FlowNotFoundException):
        qfs.submit_response(db, _user(db), "nope", 1, {"year": "x"})


def test_a_stale_client_version_is_accepted_and_stamped_with_the_servers(db):
    """The version is no longer a gate.

    It used to be a 409 that refused the whole submission. On an additive change
    that throws away answers which are still perfectly valid, so a mismatch is
    now simply accepted and the row records the SERVER's version — the contract
    generation that actually validated it.
    """
    user = _user(db)
    _register(version=3)

    snapshot = qfs.submit_response(db, user, "demo", 1, {"year": "x"})

    assert snapshot.version == 3
    assert snapshot.answers["year"] == "x"


def test_a_stale_client_is_caught_per_question_rather_than_wholesale(db):
    """An id the flow no longer asks is a 422 naming THAT question.

    The granular replacement for the old blanket conflict: the client learns
    exactly which of its questions is gone, instead of being told to discard
    everything the person just typed.
    """
    user = _user(db)
    _register(version=2)

    with pytest.raises(FlowAnswersInvalidException) as caught:
        qfs.submit_response(db, user, "demo", 1, {"totally": "wrong"})

    assert caught.value.errors == [{"field": "totally", "type": "extra_forbidden"}]
    assert db.query(QuestionFlowResponse).count() == 0


def test_invalid_answers_are_422_and_never_echo_the_submitted_values(db):
    user = _user(db)
    secret = "my-private-answer-value"

    with pytest.raises(FlowAnswersInvalidException) as caught:
        qfs.submit_response(db, user, "demo", 1, {"year": 5, "unexpected": secret})

    exc = caught.value
    assert exc.status_code == 422
    assert exc.errors  # field locations + types
    payload = repr(exc.detail)
    # Answers are PII: the response body carries locations and types only:
    assert secret not in payload
    assert all(set(item) == {"field", "type"} for item in exc.errors)
    assert {"field": "unexpected", "type": "extra_forbidden"} in exc.errors


def test_answers_over_the_size_cap_are_refused(db):
    user = _user(db)
    oversized = {"year": "x", "note": "n" * (service.MAX_ANSWERS_BYTES + 1)}
    with pytest.raises(FlowAnswersInvalidException) as caught:
        qfs.submit_response(db, user, "demo", 1, oversized)
    assert caught.value.errors == [{"field": "answers", "type": "too_large"}]
    assert db.query(QuestionFlowResponse).count() == 0


# -- reads ---------------------------------------------------------------------

def test_latest_response_is_none_before_anything_is_submitted(db):
    assert qfs.latest_response(db, _user(db).id, "demo") is None


def test_latest_response_breaks_exact_timestamp_ties_by_insert_order(db):
    user = _user(db)
    stamp = datetime(2026, 8, 1, 12, 0, 0, tzinfo=UTC)
    for marker in ("older", "newer"):
        db.add(QuestionFlowResponse(user_id=user.id, flow_key="demo", version=1,
                                    answers={"year": marker}, completed_at=stamp))
        db.commit()
    assert qfs.latest_response(db, user.id, "demo").answers["year"] == "newer"


def test_completion_summary_reports_the_latest_per_flow(db):
    user = _user(db)
    base = datetime(2026, 8, 1, tzinfo=UTC)
    rows = [
        ("demo", 1, base),
        ("demo", 2, base + timedelta(days=1)),        # the latest for demo
        ("other", 7, base + timedelta(hours=1)),
    ]
    for key, version, when in rows:
        db.add(QuestionFlowResponse(user_id=user.id, flow_key=key, version=version,
                                    answers={}, completed_at=when))
    db.commit()

    summary = qfs.completion_summary(db, user.id)
    assert set(summary) == {"demo", "other"}
    assert summary["demo"]["version"] == 2
    assert summary["other"]["version"] == 7


def test_completion_summary_breaks_exact_timestamp_ties_the_same_way_latest_does(db):
    """The GROUP BY max can match several rows when two snapshots share a
    timestamp, so the fold walks id ascending and lets the later insert win —
    the same resolution `latest_response` gives, which is what stops the gate
    read and the detail read disagreeing about which answer is current."""
    user = _user(db)
    stamp = datetime(2026, 8, 1, 12, 0, 0, tzinfo=UTC)
    for version in (1, 2):
        db.add(QuestionFlowResponse(user_id=user.id, flow_key="demo", version=version,
                                    answers={}, completed_at=stamp))
        db.commit()

    assert qfs.completion_summary(db, user.id)["demo"]["version"] == 2
    assert qfs.latest_response(db, user.id, "demo").version == 2


def test_completion_summary_is_scoped_to_one_account(db):
    mine, theirs = _user(db, "a@example.com"), _user(db, "b@example.com")
    qfs.submit_response(db, theirs, "demo", 1, {"year": "x"})
    assert qfs.completion_summary(db, mine.id) == {}


def test_completion_summary_still_reports_a_flow_that_is_no_longer_registered(db):
    """A retired or renamed flow must not silently re-gate people who answered."""
    user = _user(db)
    qfs.submit_response(db, user, "demo", 1, {"year": "x"})
    qf._FLOWS.clear()
    assert "demo" in qfs.completion_summary(db, user.id)


def test_has_completed_is_version_blind(db):
    user = _user(db)
    qfs.submit_response(db, user, "demo", 1, {"year": "x"})
    _register(version=9)
    # A version bump means "the questions changed", not "answer again":
    assert qfs.has_completed(db, user.id, "demo") is True
    assert qfs.has_completed(db, user.id, "other") is False


# -- lead module dormancy ------------------------------------------------------

def test_lead_capture_404s_while_the_module_is_dormant(db):
    with pytest.raises(FlowNotFoundException):
        qfs.capture_lead(db, "demo", "lead@example.com", 1, {"year": "x"})
    assert db.query(QuestionFlowLead).count() == 0


def test_lead_resume_404s_while_the_module_is_dormant(db):
    with pytest.raises(FlowNotFoundException):
        qfs.resume_lead(db, "demo", leads.mint_resume_token(1))


def test_housekeeping_is_deliberately_not_gated_on_dormancy(db):
    """Rows outlive the switch that created them.

    Capture and resume 404 while dormant, but the purge is the ORPHANED-PII
    path: gating it would mean a flow enabled for a week and then switched off
    leaves real personal data with no way to ever delete it, which is exactly
    what the 30-day retention promise forbids.
    """
    db.add(QuestionFlowLead(email="old@example.com", flow_key="demo", version=1,
                            created_at=datetime(2020, 1, 1, tzinfo=UTC)))
    db.commit()
    assert qfs.purge_expired_leads(db) == 1
    assert db.query(QuestionFlowLead).count() == 0


# -- lead module when a flow opts in -------------------------------------------

def test_capture_upserts_one_row_per_address_and_flow(db):
    _register(lead_capture=True)
    qfs.capture_lead(db, "demo", "  Lead@Example.COM ", 1, {"year": "first-year"})
    qfs.capture_lead(db, "demo", "lead@example.com", 1, {"year": "final-year"})

    rows = db.query(QuestionFlowLead).all()
    assert len(rows) == 1
    assert rows[0].email == "lead@example.com"       # normalized on write
    assert rows[0].answers == {"year": "final-year"}  # last write wins


def test_resume_returns_a_masked_address_and_the_saved_answers(db):
    _register(lead_capture=True)
    lead = qfs.capture_lead(db, "demo", "lead@example.com", 1, {"year": "x"})

    resumed = qfs.resume_lead(db, "demo", leads.mint_resume_token(lead.id))
    assert resumed["email_masked"] == "l***@example.com"
    assert resumed["answers"] == {"year": "x"}
    assert resumed["version"] == 1


@pytest.mark.parametrize("token", ["", "garbage", "l1.1.deadbeef", "l1.notanint.x", "v1.1.x"])
def test_bad_resume_tokens_all_look_identical(db, token):
    _register(lead_capture=True)
    with pytest.raises(InvalidResumeTokenException):
        qfs.resume_lead(db, "demo", token)


def test_a_valid_token_for_a_purged_lead_is_the_same_404(db):
    _register(lead_capture=True)
    with pytest.raises(InvalidResumeTokenException):
        qfs.resume_lead(db, "demo", leads.mint_resume_token(4242))


def test_purge_removes_unconverted_leads_past_the_retention_window(db):
    _register(lead_capture=True)
    now = datetime(2026, 8, 1, tzinfo=UTC)
    db.add_all([
        QuestionFlowLead(email="old@example.com", flow_key="demo", version=1,
                         created_at=now - timedelta(days=leads.LEAD_RETENTION_DAYS + 1)),
        QuestionFlowLead(email="fresh@example.com", flow_key="demo", version=1,
                         created_at=now - timedelta(days=1)),
    ])
    db.commit()

    assert qfs.purge_expired_leads(db, now=now) == 1
    assert [r.email for r in db.query(QuestionFlowLead).all()] == ["fresh@example.com"]


def test_converted_leads_survive_the_purge(db):
    _register(lead_capture=True)
    now = datetime(2026, 8, 1, tzinfo=UTC)
    user = _user(db)
    db.add(QuestionFlowLead(email="old@example.com", flow_key="demo", version=1,
                            converted_user_id=user.id,
                            created_at=now - timedelta(days=leads.LEAD_RETENTION_DAYS + 5)))
    db.commit()
    assert qfs.purge_expired_leads(db, now=now) == 0


# -- conversion ----------------------------------------------------------------

def test_on_account_created_deletes_every_lead_for_the_new_address(db):
    _register(lead_capture=True)
    db.add_all([
        QuestionFlowLead(email="new@example.com", flow_key="demo", version=1),
        QuestionFlowLead(email="new@example.com", flow_key="retired", version=1),
        QuestionFlowLead(email="someone@example.com", flow_key="demo", version=1),
    ])
    db.commit()

    qfs.on_account_created(db, _user(db, "New@Example.com"))

    remaining = [r.email for r in db.query(QuestionFlowLead).all()]
    assert remaining == ["someone@example.com"]


def test_on_account_created_still_converts_while_the_module_is_dormant(db):
    """Same reason as the purge: a row that exists has to be deletable, whatever
    the capture switch currently says."""
    db.add(QuestionFlowLead(email="new@example.com", flow_key="demo", version=1))
    db.commit()
    qfs.on_account_created(db, _user(db, "new@example.com"))
    assert db.query(QuestionFlowLead).count() == 0


def test_rows_captured_while_enabled_are_still_purged_after_capture_is_switched_off(db):
    """The end-to-end orphan case: enable, capture, disable, housekeep."""
    _register(lead_capture=True)
    now = datetime(2026, 8, 1, tzinfo=UTC)
    expired = qfs.capture_lead(db, "demo", "old@example.com", 1, {"year": "x"})
    expired.created_at = now - timedelta(days=leads.LEAD_RETENTION_DAYS + 1)
    fresh = qfs.capture_lead(db, "demo", "signup@example.com", 1, {"year": "y"})
    fresh.created_at = now - timedelta(days=1)
    db.commit()

    # The product changes its mind and turns capture back off:
    _register(lead_capture=False)
    assert qf.any_lead_capture_enabled() is False

    # Retention still collects the expired row:
    assert qfs.purge_expired_leads(db, now=now) == 1
    # ...and conversion still collects the one whose owner just signed up:
    qfs.on_account_created(db, _user(db, "signup@example.com"))
    assert db.query(QuestionFlowLead).count() == 0


def test_on_account_created_never_raises(db, monkeypatch):
    """Signup must not fail because housekeeping did."""
    _register(lead_capture=True)
    monkeypatch.setattr(leads, "delete_leads_for_email",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    qfs.on_account_created(db, _user(db))  # no exception


# -- the user contract's three-state `flows` field -----------------------------

def test_user_response_flows_is_null_when_nothing_computed_it(db):
    """Every producer but GET /user/ leaves it null, and null must read as
    UNKNOWN — otherwise a login response would bounce a finished user back
    through onboarding."""
    from platform.schemas import UserResponse

    user = _user(db)
    assert UserResponse.model_validate(user).flows is None


def test_user_response_flows_distinguishes_computed_empty_from_not_computed(db):
    from platform.schemas import UserResponse

    user = _user(db)
    user.flows_summary = qfs.completion_summary(db, user.id)
    assert UserResponse.model_validate(user).flows == {}

    qfs.submit_response(db, user, "demo", 1, {"year": "final-year"})
    user.flows_summary = qfs.completion_summary(db, user.id)
    summary = UserResponse.model_validate(user).flows
    assert set(summary) == {"demo"}
    assert summary["demo"].version == 1
    # The gate read carries facts ABOUT the run plus question IDS, and never an
    # answer value — answers are a separate, explicit fetch:
    assert set(summary["demo"].model_dump()) == {
        "version", "completed_at", "answered", "outstanding", "complete",
    }
    assert summary["demo"].answered == ["year", "note"]
    assert summary["demo"].outstanding == []
    assert summary["demo"].complete is True
