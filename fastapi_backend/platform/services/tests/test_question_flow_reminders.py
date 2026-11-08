"""The reminder sweep: who is due, and the at-most-once guarantee.

Two things are worth proving and they are the two that hurt when wrong.

DUE-SELECTION is a set of guards, not a query optimisation: unverified accounts,
accounts younger than the stage delay, accounts predating the flow's blast-guard
epoch, and accounts that already answered must all be out — and the blast guard
in particular is what stands between shipping a flow and emailing the entire
pre-existing user base on the first tick.

CLAIM-FIRST is the at-most-once mechanism: the sent-record is committed before
the send, so a repeated sweep, a concurrent worker or a crash cannot produce a
second email. The deliberate cost is that a failed send is forfeited, which this
also pins down.

ONE EMAIL PER TICK is the third: an account that has overshot several stages at
once gets the most advanced one and nothing else, with the stages it skipped
claimed-but-unsent so they cannot fire later either.
"""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import BaseModel, ConfigDict
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from infrastructure.database import Base
import platform.models  # noqa: F401 — registers the tables on Base.metadata
from platform import question_flows as qf
from platform.models import (
    MarketingSuppression,
    QuestionFlowLead,
    QuestionFlowReminder,
    QuestionFlowResponse,
    User,
)
from platform.services.question_flow_service import reminders


class Answers(BaseModel):
    model_config = ConfigDict(extra="forbid")
    year: str


EPOCH = datetime(2026, 8, 1, tzinfo=UTC)
NOW = datetime(2026, 9, 1, tzinfo=UTC)


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
    saved = dict(qf._FLOWS)
    qf._FLOWS.clear()
    qf.register_flow(qf.QuestionFlow(
        key="demo", version=1, answers_schema=Answers, title="Demo",
        reminder_stages=(
            qf.ReminderStage(after=timedelta(days=1)),
            qf.ReminderStage(after=timedelta(days=4)),
        ),
        reminder_cta_path="/welcome",
        remind_accounts_created_after=EPOCH,
    ))
    try:
        yield
    finally:
        qf._FLOWS.clear()
        qf._FLOWS.update(saved)


def _register_stages(*days: float) -> None:
    """Re-registers "demo" with a different stage ladder (in days)."""
    qf._FLOWS.pop("demo", None)
    qf.register_flow(qf.QuestionFlow(
        key="demo", version=1, answers_schema=Answers, title="Demo",
        reminder_stages=tuple(qf.ReminderStage(after=timedelta(days=d)) for d in days),
        reminder_cta_path="/welcome",
        remind_accounts_created_after=EPOCH,
    ))


@pytest.fixture
def sent(monkeypatch):
    """Captures sends instead of hitting the provider."""
    captured: list[dict] = []

    def _capture(**kwargs):
        captured.append(kwargs)

    monkeypatch.setattr(reminders, "send_templated_email", _capture)
    return captured


def _user(db, email="a@example.com", *, created_days_ago=10, verified=True) -> User:
    user = User(name="A", email=email, is_verified=verified,
                created_at=NOW - timedelta(days=created_days_ago))
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _sweep(db):
    return reminders.run_sweep(db, now=NOW)


# -- due selection -------------------------------------------------------------

def test_an_account_overdue_for_several_stages_gets_exactly_one_email(db, sent, monkeypatch):
    """The anti-burst rule, and the reason the skipped stages are still claimed.

    A 14-day-old account is past all three of a 24h/4d/12d ladder. Firing each
    would deliver three near-identical nudges in the same minute — the exact
    thing a late verification, a provider wired after launch, or a missed tick
    would cause. So the MOST ADVANCED due stage is the one that goes out, and
    the two it overshot are claimed without sending: the claim is the durable
    record of that decision, so they cannot fire on a later tick either.
    """
    _register_stages(1, 4, 12)
    _user(db, created_days_ago=14)

    # Claim-first means the stage being sent is the only one claimed at the
    # moment of the send — which is how "stage 2, not stage 0" is observable:
    stages_at_send: list[list[int]] = []
    capture = reminders.send_templated_email

    def _observe(**kwargs):
        stages_at_send.append([r.stage for r in db.query(QuestionFlowReminder).all()])
        capture(**kwargs)

    monkeypatch.setattr(reminders, "send_templated_email", _observe)

    assert _sweep(db) == 1
    assert len(sent) == 1
    assert stages_at_send == [[2]]
    # All three stages are recorded, so the two skipped ones are settled:
    assert sorted(r.stage for r in db.query(QuestionFlowReminder).all()) == [0, 1, 2]

    # And the superseded stages stay superseded:
    assert _sweep(db) == 0
    assert len(sent) == 1


def test_only_the_stages_whose_delay_has_elapsed_fire(db, sent):
    # Two days old: past stage 0 (24h), not yet stage 1 (4d):
    _user(db, created_days_ago=2)
    assert _sweep(db) == 1
    assert [r.stage for r in db.query(QuestionFlowReminder).all()] == [0]


def test_an_account_younger_than_the_first_stage_is_not_reminded(db, sent):
    _user(db, created_days_ago=0)
    assert _sweep(db) == 0
    assert sent == []


def test_unverified_accounts_are_never_reminded(db, sent):
    """Verification mail owns them; a second stream to an unconfirmed address is
    how a sending domain acquires a reputation problem."""
    _user(db, verified=False)
    assert _sweep(db) == 0


def test_the_blast_guard_excludes_accounts_predating_the_flows_epoch(db, sent):
    legacy = User(name="Legacy", email="legacy@example.com", is_verified=True,
                  created_at=EPOCH - timedelta(days=1))
    db.add(legacy)
    db.commit()
    assert _sweep(db) == 0
    assert db.query(QuestionFlowReminder).count() == 0


def test_completing_the_flow_removes_an_account_from_every_future_stage(db, sent):
    user = _user(db, created_days_ago=10)
    db.add(QuestionFlowResponse(user_id=user.id, flow_key="demo", version=1,
                                answers={"year": "x"}, completed_at=NOW - timedelta(days=9)))
    db.commit()
    assert _sweep(db) == 0


def test_a_response_to_a_different_flow_does_not_count(db, sent):
    user = _user(db, created_days_ago=10)
    db.add(QuestionFlowResponse(user_id=user.id, flow_key="other", version=1,
                                answers={}, completed_at=NOW))
    db.commit()
    # Still due (the other flow's answer is irrelevant), and still one email:
    assert _sweep(db) == 1
    assert sorted(r.stage for r in db.query(QuestionFlowReminder).all()) == [0, 1]


def test_suppressed_addresses_are_excluded_in_sql_and_not_claimed(db, sent):
    """A courtesy check, but a real one: nothing is claimed, so re-subscribing
    later still leaves the nudges available."""
    _user(db, email="stop@example.com")
    db.add(MarketingSuppression(email="stop@example.com"))
    db.commit()

    assert _sweep(db) == 0
    assert db.query(QuestionFlowReminder).count() == 0


def test_a_suppressed_account_does_not_occupy_a_batch_slot(db, sent, monkeypatch):
    """The starvation case: excluded in the query, not skipped in the loop.

    Suppressed accounts sit at the head of the id-ordered batch, so a post-query
    skip would spend the whole batch on them and never reach anyone else — on
    this tick or any future one, since nothing about them ever changes.
    """
    monkeypatch.setattr(reminders, "REMINDER_BATCH_SIZE", 2)
    for index in range(2):
        _user(db, email=f"stop{index}@example.com")
        db.add(MarketingSuppression(email=f"stop{index}@example.com"))
    db.commit()
    _user(db, email="live@example.com")  # higher id, behind both suppressed rows

    assert _sweep(db) == 1
    assert [payload["recipient"] for payload in sent] == ["live@example.com"]
    # Only the eligible account was claimed:
    assert db.query(QuestionFlowReminder).count() == 2


def test_exclude_predicate_excludes_matching_accounts_in_sql_and_not_claimed(db, sent):
    """A product-supplied exclusion (e.g. "this account already got a different
    funnel's welcome email") behaves exactly like suppression: excluded, and
    never claimed, so it stays free to be reminded if the exclusion ever lifts."""
    excluded = _user(db, email="funnel@example.com")
    _register_stages(1, 4)
    qf._FLOWS["demo"] = replace(
        qf._FLOWS["demo"], exclude_predicate=lambda db: User.id == excluded.id
    )

    assert _sweep(db) == 0
    assert db.query(QuestionFlowReminder).count() == 0


def test_exclude_predicate_does_not_occupy_a_batch_slot(db, sent, monkeypatch):
    """Same starvation argument as suppression: a clause, not a post-filter."""
    monkeypatch.setattr(reminders, "REMINDER_BATCH_SIZE", 2)
    excluded_ids = [_user(db, email=f"excluded{i}@example.com").id for i in range(2)]
    live = _user(db, email="live@example.com")  # higher id, behind both excluded rows
    qf._FLOWS["demo"] = replace(
        qf._FLOWS["demo"], exclude_predicate=lambda db: User.id.in_(excluded_ids)
    )

    assert _sweep(db) == 1
    assert [payload["recipient"] for payload in sent] == ["live@example.com"]
    # Only the eligible account was claimed (both its stages elapsed):
    assert {r.user_id for r in db.query(QuestionFlowReminder).all()} == {live.id}


def test_the_batch_size_bounds_one_tick(db, sent, monkeypatch):
    monkeypatch.setattr(reminders, "REMINDER_BATCH_SIZE", 2)
    for index in range(5):
        _user(db, email=f"u{index}@example.com")
    # A batch of two accounts, one email each (both stages have elapsed, so the
    # more advanced one supersedes the other):
    assert _sweep(db) == 2
    assert sorted(r.stage for r in db.query(QuestionFlowReminder).all()) == [0, 0, 1, 1]


# -- at-most-once --------------------------------------------------------------

def test_a_repeated_sweep_sends_nothing_more(db, sent):
    _user(db, created_days_ago=10)
    assert _sweep(db) == 1
    assert _sweep(db) == 0
    assert len(sent) == 1
    # Both stages are settled by the first tick — one sent, one superseded:
    assert db.query(QuestionFlowReminder).count() == 2


def test_the_claim_is_committed_before_the_send(db, sent, monkeypatch):
    """Ordering, not just presence: a crash mid-send must not re-send."""
    seen: list[int] = []

    def _observe(**kwargs):
        seen.append(db.query(QuestionFlowReminder).count())

    monkeypatch.setattr(reminders, "send_templated_email", _observe)
    _user(db, created_days_ago=2)
    _sweep(db)
    assert seen == [1]  # the row was already durable when the send ran


def test_a_pre_existing_sent_record_is_honoured(db, sent):
    user = _user(db, created_days_ago=10)
    db.add(QuestionFlowReminder(user_id=user.id, flow_key="demo", stage=0))
    db.commit()
    assert _sweep(db) == 1
    assert sorted(r.stage for r in db.query(QuestionFlowReminder).all()) == [0, 1]


def test_a_stage_superseded_on_an_earlier_tick_never_fires_later(db, sent):
    """The claim-without-send is what makes this true: an account that overshot
    stage 0 has a stage-0 row, so a later tick has nothing left to deliver."""
    _user(db, created_days_ago=10)
    assert _sweep(db) == 1

    superseded = db.query(QuestionFlowReminder).filter(
        QuestionFlowReminder.stage == 0).one()
    assert superseded.flow_key == "demo"

    # Even much later, with every stage long elapsed:
    assert reminders.run_sweep(db, now=NOW + timedelta(days=365)) == 0
    assert len(sent) == 1


def test_a_failed_send_is_forfeited_rather_than_retried(db, monkeypatch):
    def _explode(**kwargs):
        raise RuntimeError("provider down")

    monkeypatch.setattr(reminders, "send_templated_email", _explode)
    _user(db, created_days_ago=2)

    # The sweep survives the failure and keeps the claim, so the next tick will
    # not turn a lost email into a duplicate:
    assert reminders.run_sweep(db, now=NOW) == 1
    assert db.query(QuestionFlowReminder).count() == 1
    assert reminders.run_sweep(db, now=NOW) == 0


# -- payload -------------------------------------------------------------------

def test_the_reminder_carries_a_cta_and_a_working_stop_link(db, sent):
    _user(db, created_days_ago=2)
    _sweep(db)

    payload = sent[0]
    assert payload["flow_title"] == "Demo"
    assert payload["cta_url"].endswith("/welcome")
    assert payload["stop_url"].startswith("https://")
    assert "token=" in payload["stop_url"]
    # Answers never travel in a reminder:
    assert "answers" not in payload


def test_the_reminder_carries_the_rfc_8058_unsubscribe_header(db, sent):
    """The same stop link the footer shows, promoted to a header so the mail
    client's native Unsubscribe control works rather than the user hitting
    "spam" to make the nudges stop."""
    _user(db, created_days_ago=2)
    _sweep(db)

    payload = sent[0]
    assert payload["headers"]["List-Unsubscribe"] == f"<{payload['stop_url']}>"
    # No one-click POST claim: this target is a human confirm page:
    assert "List-Unsubscribe-Post" not in payload["headers"]


# -- lifecycle gate ------------------------------------------------------------

def test_the_loop_does_not_start_when_no_flow_asks_for_it():
    qf._FLOWS.clear()
    assert reminders.sweep_is_enabled() is False
    assert reminders.start_question_flow_sweep_loop() is None


def test_the_loop_is_enabled_by_a_lead_capture_flow_alone():
    qf._FLOWS.clear()
    qf.register_flow(qf.QuestionFlow(key="demo", version=1, answers_schema=Answers,
                                     title="Demo", lead_capture=True))
    assert reminders.sweep_is_enabled() is True


def test_the_loop_stays_enabled_after_lead_capture_is_switched_off():
    # The orphaned-PII window: rows captured while capture was on must still be
    # purged after it is switched off, so a registered flow keeps the sweep
    # alive even with no reminder stages and no live capture:
    qf._FLOWS.clear()
    qf.register_flow(qf.QuestionFlow(key="demo", version=1, answers_schema=Answers,
                                     title="Demo", lead_capture=False))
    assert reminders.sweep_is_enabled() is True


def test_run_sweep_purges_leads_even_without_an_email_provider(db):
    qf._FLOWS.clear()
    qf.register_flow(qf.QuestionFlow(key="demo", version=1, answers_schema=Answers,
                                     title="Demo", lead_capture=True))
    lead = QuestionFlowLead(email="old@example.com", flow_key="demo", version=1)
    db.add(lead)
    db.commit()
    db.query(QuestionFlowLead).update(
        {"created_at": datetime.now(UTC) - timedelta(days=45)}
    )
    db.commit()

    # send_reminders=False is the no-provider tick — the purge must still run:
    reminders.run_sweep(db, send_reminders=False)
    assert db.query(QuestionFlowLead).count() == 0
