"""Per-question completion: the property that makes onboarding granular.

The claim under test, in the terms it was asked for: adding a question to a flow
and shipping its page must ask an existing member THAT QUESTION and nothing else,
the next time they land anywhere the gate runs. Everything they answered before
stays answered, nothing they answered is overwritten, and a brand-new account is
still asked the whole set.

Every test here adds, retires, or breaks a question on a live registry and then
asserts what the API would tell a client, because that is the only place the
behaviour is observable. The negatives matter as much as the positives: a
retired flow must never re-gate anyone, a null must never count as an answer,
and the reminder programme must not turn an additive change into a mail blast.
"""

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import BaseModel, ConfigDict
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from typing import Literal, Optional

from infrastructure.database import Base
import platform.models  # noqa: F401 — registers the tables on Base.metadata
from platform.exceptions import FlowAnswersInvalidException
from platform.models import QuestionFlowResponse, User
from platform import question_flows as qf
from platform.services import question_flow_service as qfs


# The "old" flow: the three questions that shipped first.
class AnswersV1(BaseModel):
    model_config = ConfigDict(extra="forbid")
    year: Literal["first-year", "masters"]
    experience: Literal["beginner", "quant-experience"]
    goal: Literal["break-in", "explore"]


# The same flow after a fourth question is appended — the ONLY edit a product
# makes to ask something new.
class AnswersV2(BaseModel):
    model_config = ConfigDict(extra="forbid")
    year: Literal["first-year", "masters"]
    experience: Literal["beginner", "quant-experience"]
    goal: Literal["break-in", "explore"]
    motivation: Literal["career", "curiosity"]


COMPLETE_V1 = {"year": "masters", "experience": "beginner", "goal": "explore"}


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
    """An isolated registry holding only this file's flow."""
    saved = dict(qf._FLOWS)
    qf._FLOWS.clear()
    _register(AnswersV1)
    try:
        yield
    finally:
        qf._FLOWS.clear()
        qf._FLOWS.update(saved)


def _register(schema, version=1, **overrides):
    """Re-points "demo" at a different question set — i.e. ships a new deploy."""
    qf._FLOWS.pop("demo", None)
    qf.register_flow(
        qf.QuestionFlow(key="demo", version=version, answers_schema=schema, title="Demo", **overrides)
    )


def _user(db, email="a@example.com") -> User:
    user = User(name="A", email=email, is_verified=True)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _progress(db, user):
    return qfs.question_progress(db, user.id)["demo"]


# -- the headline behaviour ----------------------------------------------------

def test_a_new_question_is_the_only_thing_asked_of_someone_who_answered_the_old_set(db):
    user = _user(db)
    qfs.submit_response(db, user, "demo", 1, COMPLETE_V1)
    assert _progress(db, user)["outstanding"] == []

    # The deploy that adds a question:
    _register(AnswersV2)

    progress = _progress(db, user)
    assert progress["outstanding"] == ["motivation"]
    assert progress["answered"] == ["year", "experience", "goal"]
    assert progress["complete"] is False
    assert progress["questions"] == ["year", "experience", "goal", "motivation"]


def test_answering_only_the_new_question_completes_the_flow_and_keeps_the_old_answers(db):
    user = _user(db)
    qfs.submit_response(db, user, "demo", 1, COMPLETE_V1)
    _register(AnswersV2)

    # Exactly what the client sends after asking the one outstanding question:
    snapshot = qfs.submit_response(db, user, "demo", 1, {"motivation": "career"})

    assert snapshot.answers == {**COMPLETE_V1, "motivation": "career"}
    assert qfs.outstanding_questions(db, user.id, "demo") == ()
    assert qfs.has_answered_all(db, user.id, "demo") is True


def test_a_partial_submission_never_loses_or_overwrites_an_earlier_answer(db):
    """Append-only plus merge: the old row is intact AND its answers carried forward."""
    user = _user(db)
    qfs.submit_response(db, user, "demo", 1, COMPLETE_V1)
    _register(AnswersV2)
    qfs.submit_response(db, user, "demo", 1, {"motivation": "curiosity"})

    rows = db.query(QuestionFlowResponse).order_by(QuestionFlowResponse.id).all()
    assert len(rows) == 2
    assert rows[0].answers == COMPLETE_V1
    assert rows[1].answers["goal"] == "explore"
    assert rows[1].answers["motivation"] == "curiosity"


def test_a_brand_new_account_is_asked_every_question(db):
    _register(AnswersV2)
    user = _user(db)

    progress = _progress(db, user)
    assert progress["outstanding"] == ["year", "experience", "goal", "motivation"]
    assert progress["answered"] == []
    assert progress["complete"] is False
    assert progress["completed_at"] is None
    # And it is REPORTED, unlike the completion summary, which is keyed on what
    # the account answered and so has no entry at all:
    assert qfs.completion_summary(db, user.id) == {}


def test_an_account_that_answered_everything_has_nothing_outstanding(db):
    _register(AnswersV2)
    user = _user(db)
    qfs.submit_response(db, user, "demo", 1, {**COMPLETE_V1, "motivation": "career"})

    progress = _progress(db, user)
    assert progress["outstanding"] == []
    assert progress["complete"] is True
    assert progress["answered"] == ["year", "experience", "goal", "motivation"]


def test_one_account_gaining_a_question_does_not_touch_another(db):
    mine = _user(db, "a@example.com")
    theirs = _user(db, "b@example.com")
    qfs.submit_response(db, mine, "demo", 1, COMPLETE_V1)
    _register(AnswersV2)
    qfs.submit_response(db, mine, "demo", 1, {"motivation": "career"})

    assert qfs.outstanding_questions(db, mine.id, "demo") == ()
    assert qfs.outstanding_questions(db, theirs.id, "demo") == (
        "year", "experience", "goal", "motivation",
    )


# -- partial submission mechanics ----------------------------------------------

def test_a_partial_submission_that_still_leaves_questions_owed_is_stored_as_partial(db):
    """Progressive answering works: two questions arrive, two remain owed."""
    _register(AnswersV2)
    user = _user(db)

    qfs.submit_response(db, user, "demo", 1, {"year": "masters", "goal": "explore"})

    assert qfs.outstanding_questions(db, user.id, "demo") == ("experience", "motivation")
    assert qfs.latest_response(db, user.id, "demo").answers == {
        "year": "masters", "goal": "explore",
    }


def test_a_partial_submission_is_stored_in_the_flows_declaration_order(db):
    """The stored blob's shape must not depend on what order a client sent things."""
    _register(AnswersV2)
    user = _user(db)
    qfs.submit_response(db, user, "demo", 1, {"goal": "explore", "year": "masters"})
    assert list(qfs.latest_response(db, user.id, "demo").answers) == ["year", "goal"]


def test_re_answering_a_question_wins_over_the_carried_forward_value(db):
    user = _user(db)
    qfs.submit_response(db, user, "demo", 1, COMPLETE_V1)
    qfs.submit_response(db, user, "demo", 1, {"goal": "break-in"})

    latest = qfs.latest_response(db, user.id, "demo")
    assert latest.answers["goal"] == "break-in"
    # ... and the rest of the account's position rode along untouched:
    assert latest.answers["year"] == "masters"
    assert latest.answers["experience"] == "beginner"


def test_an_empty_submission_is_refused_rather_than_appended(db):
    user = _user(db)
    with pytest.raises(FlowAnswersInvalidException) as caught:
        qfs.submit_response(db, user, "demo", 1, {})
    assert caught.value.errors == [{"field": "answers", "type": "missing"}]
    assert db.query(QuestionFlowResponse).count() == 0


def test_a_null_is_not_an_answer(db):
    """Omitting a question and sending null for it must mean the same thing."""
    user = _user(db)
    with pytest.raises(FlowAnswersInvalidException) as caught:
        qfs.submit_response(db, user, "demo", 1, {"year": None})
    assert caught.value.errors == [{"field": "year", "type": "missing"}]


def test_every_bad_answer_in_one_body_is_reported_at_once(db):
    user = _user(db)
    with pytest.raises(FlowAnswersInvalidException) as caught:
        qfs.submit_response(db, user, "demo", 1, {"year": "not-an-option", "nope": "x"})

    fields = {item["field"] for item in caught.value.errors}
    assert fields == {"year", "nope"}
    assert {"field": "nope", "type": "extra_forbidden"} in caught.value.errors


def test_an_invalid_answer_never_echoes_the_submitted_value(db):
    user = _user(db)
    secret = "my-private-answer-value"
    with pytest.raises(FlowAnswersInvalidException) as caught:
        qfs.submit_response(db, user, "demo", 1, {"year": secret})
    assert secret not in repr(caught.value.detail)
    assert all(set(item) == {"field", "type"} for item in caught.value.errors)


# -- optional questions --------------------------------------------------------

class AnswersWithOptional(BaseModel):
    model_config = ConfigDict(extra="forbid")
    year: Literal["first-year", "masters"]
    experience: Literal["beginner", "quant-experience"]
    goal: Literal["break-in", "explore"]
    referrer: Optional[Literal["friend", "search"]] = None


def test_a_question_with_a_default_is_offered_but_never_outstanding(db):
    """The way to add a question without interrupting existing members."""
    user = _user(db)
    qfs.submit_response(db, user, "demo", 1, COMPLETE_V1)
    _register(AnswersWithOptional)

    progress = _progress(db, user)
    assert progress["questions"] == ["year", "experience", "goal", "referrer"]
    assert progress["outstanding"] == []
    assert progress["complete"] is True
    # Offered, and reported as answered once it has a value:
    qfs.submit_response(db, user, "demo", 1, {"referrer": "friend"})
    assert _progress(db, user)["answered"] == ["year", "experience", "goal", "referrer"]


# -- breaking a question, retiring a question, retiring a flow -----------------

class AnswersWithRenamedGoalOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    year: Literal["first-year", "masters"]
    experience: Literal["beginner", "quant-experience"]
    # "explore" was retired in favour of "browse" — a BREAKING change to a stored
    # answer, which used to require a version bump and a whole re-run:
    goal: Literal["break-in", "browse"]


def test_retiring_an_answer_option_re_asks_only_that_question(db):
    user = _user(db)
    qfs.submit_response(db, user, "demo", 1, COMPLETE_V1)  # goal == "explore"

    _register(AnswersWithRenamedGoalOptions, version=2)

    progress = _progress(db, user)
    assert progress["outstanding"] == ["goal"]
    assert progress["answered"] == ["year", "experience"]


def test_the_re_answer_of_a_broken_question_does_not_carry_the_dead_value_forward(db):
    user = _user(db)
    qfs.submit_response(db, user, "demo", 1, COMPLETE_V1)
    _register(AnswersWithRenamedGoalOptions, version=2)

    snapshot = qfs.submit_response(db, user, "demo", 1, {"goal": "browse"})

    assert snapshot.answers == {"year": "masters", "experience": "beginner", "goal": "browse"}
    # The dead value is not destroyed — the row that holds it is append-only:
    first = db.query(QuestionFlowResponse).order_by(QuestionFlowResponse.id).first()
    assert first.answers["goal"] == "explore"


class AnswersWithGoalRemoved(BaseModel):
    model_config = ConfigDict(extra="forbid")
    year: Literal["first-year", "masters"]
    experience: Literal["beginner", "quant-experience"]


def test_removing_a_question_asks_it_of_nobody_and_reports_it_to_nobody(db):
    user = _user(db)
    qfs.submit_response(db, user, "demo", 1, COMPLETE_V1)

    _register(AnswersWithGoalRemoved, version=2)

    progress = _progress(db, user)
    assert progress["questions"] == ["year", "experience"]
    assert progress["outstanding"] == []
    assert "goal" not in progress["answered"]
    # The stored row still holds it; nothing reads it:
    assert qfs.latest_response(db, user.id, "demo").answers["goal"] == "explore"


def test_a_retired_flow_is_reported_complete_and_never_re_gates_anyone(db):
    user = _user(db)
    qfs.submit_response(db, user, "demo", 1, COMPLETE_V1)
    qf._FLOWS.clear()

    summary = qfs.completion_summary(db, user.id)["demo"]
    assert summary["complete"] is True
    assert summary["outstanding"] == []
    # No definition means nothing to owe against, on either read:
    assert qfs.outstanding_questions(db, user.id, "demo") == ()
    assert qfs.has_answered_all(db, user.id, "demo") is True
    assert qfs.question_progress(db, user.id) == {}


# -- the shape the frontend gate reads -----------------------------------------

def test_the_completion_summary_still_omits_flows_the_account_never_answered(db):
    """The pre-existing gate reads `flows[key] != null` as "has onboarded".

    Putting a never-answered flow in there would tell every such gate that a
    brand-new account had already finished — so the granular read is a SEPARATE
    field and this one stays keyed on what was actually answered.
    """
    _register(AnswersV2)
    user = _user(db)
    assert qfs.completion_summary(db, user.id) == {}
    assert "demo" in qfs.question_progress(db, user.id)


def test_the_completion_summary_reports_a_newly_added_question_as_outstanding(db):
    user = _user(db)
    qfs.submit_response(db, user, "demo", 1, COMPLETE_V1)
    _register(AnswersV2)

    summary = qfs.completion_summary(db, user.id)["demo"]
    assert summary["complete"] is False
    assert summary["outstanding"] == ["motivation"]
    # The stored snapshot's own facts are unchanged by the deploy:
    assert summary["version"] == 1
    assert summary["completed_at"] is not None


def test_question_progress_reports_the_flows_current_version_not_the_rows(db):
    user = _user(db)
    qfs.submit_response(db, user, "demo", 1, COMPLETE_V1)
    _register(AnswersV2, version=7)

    assert qfs.question_progress(db, user.id)["demo"]["version"] == 7
    assert qfs.completion_summary(db, user.id)["demo"]["version"] == 1


def test_the_user_contract_carries_question_ids_and_never_an_answer_value(db):
    from platform.schemas import UserResponse

    user = _user(db)
    qfs.submit_response(db, user, "demo", 1, COMPLETE_V1)
    _register(AnswersV2)
    user.flows_summary = qfs.completion_summary(db, user.id)
    user.flow_questions = qfs.question_progress(db, user.id)

    body = UserResponse.model_validate(user)
    assert body.flow_questions["demo"].outstanding == ["motivation"]
    assert body.flows["demo"].outstanding == ["motivation"]
    # Answer VALUES are a separate, explicit fetch; only ids ride on the account:
    assert "masters" not in body.model_dump_json()


def test_the_user_contract_leaves_both_flow_fields_null_when_nothing_computed_them(db):
    """A login response must not read as "definitely not onboarded"."""
    from platform.schemas import UserResponse

    body = UserResponse.model_validate(_user(db))
    assert body.flows is None
    assert body.flow_questions is None


# -- what deliberately did NOT change ------------------------------------------

def test_adding_a_question_does_not_re_arm_the_reminder_programme(db):
    """The sweep targets accounts that never submitted ANYTHING for the flow.

    Re-pointing it at "has outstanding questions" would mail the entire
    membership every time a question was added — the exact blast the flow
    registry's epoch guard exists to prevent. The sweep's audience predicate is
    "no snapshot row exists", which `has_completed` states in one call, and it
    stays true across an additive change while the granular gate turns over.
    """
    user = _user(db)
    qfs.submit_response(db, user, "demo", 1, COMPLETE_V1)
    _register(AnswersV2)

    assert qfs.has_completed(db, user.id, "demo") is True
    assert qfs.has_answered_all(db, user.id, "demo") is False


def test_a_complete_submission_is_normalised_exactly_as_before(db):
    """Defaults filled, one shape per contract — unchanged by partial support."""
    _register(AnswersWithOptional)
    user = _user(db)
    snapshot = qfs.submit_response(db, user, "demo", 1, COMPLETE_V1)
    assert snapshot.answers == {**COMPLETE_V1, "referrer": None}


def test_completion_is_read_from_the_latest_snapshot_not_the_union_of_history(db):
    """Latest-wins is the rule everywhere else; it must be the rule here too.

    An earlier row that answered a question the latest row does not is history,
    not current state — otherwise deleting an answer would be impossible and the
    gate would disagree with `latest_response`.
    """
    _register(AnswersV2)
    user = _user(db)
    base = datetime(2026, 8, 1, tzinfo=UTC)
    db.add(QuestionFlowResponse(user_id=user.id, flow_key="demo", version=1,
                                answers={**COMPLETE_V1, "motivation": "career"},
                                completed_at=base))
    db.add(QuestionFlowResponse(user_id=user.id, flow_key="demo", version=1,
                                answers={"year": "masters"},
                                completed_at=base + timedelta(days=1)))
    db.commit()

    assert qfs.outstanding_questions(db, user.id, "demo") == (
        "experience", "goal", "motivation",
    )


def test_a_row_whose_answers_are_not_a_dict_is_read_as_answering_nothing(db):
    """Rows are input to be read, not a shape to trust: a blob written by some
    older deploy must degrade to "unanswered", never raise on GET /user/."""
    user = _user(db)
    db.add(QuestionFlowResponse(user_id=user.id, flow_key="demo", version=1,
                                answers=["not", "a", "dict"],
                                completed_at=datetime(2026, 8, 1, tzinfo=UTC)))
    db.commit()

    assert _progress(db, user)["outstanding"] == ["year", "experience", "goal"]
    assert qfs.completion_summary(db, user.id)["demo"]["complete"] is False
