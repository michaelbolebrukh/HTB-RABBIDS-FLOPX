"""Snapshot writes and reads for question flows.

A submission carries WHATEVER SUBSET of the flow's questions the client is
answering right now, and the writer merges it over what the account already
answered. That is what makes a flow granular: adding a question asks the one new
question of an existing member instead of restarting the whole flow, and the row
it writes is still a complete snapshot of that account's position. Which
questions are outstanding is not stored anywhere — it is derived from the answers
themselves by `questions.py`, which holds that definition alone.

Three invariants live here and nowhere else:

1. APPEND-ONLY. Nothing in this module updates or deletes a
   question_flow_responses row. A re-take is a new row; the old one stays. That
   is what makes the table an honest history rather than a mutable profile.
   The ONE carve-out is `qa_reset.py` — a TEMP, admin-only QA harness that
   deletes an account's rows for one flow so the flow can be re-run during
   testing. It is a separate module, behind its own grant, so removing it before
   launch restores the invariant to "nowhere, ever" by deleting one file.
2. `completed_at` IS SERVER-STAMPED. The client never supplies it. It is the
   staleness fact every consumer reads, so a client-supplied value would be a
   client-controlled gate.
3. ANSWERS ARE NEVER LOGGED. They are personal data by construction ("what year
   are you in", "where are you starting from"). Validation errors carry field
   locations and error types only — never the submitted values.
"""

import json
import logging
from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError
from sqlalchemy import func
from sqlalchemy.orm import Session

from platform.exceptions import (
    FlowAnswersInvalidException,
    FlowNotFoundException,
)
from platform.models import QuestionFlowResponse, User
from platform.question_flows import QuestionFlow, all_flows, get_flow
from platform.services.question_flow_service.questions import (
    answered_question_ids,
    carry_forward_answers,
    outstanding_question_ids,
    question_adapter,
)


logger = logging.getLogger(__name__)


# Serialized-answers ceiling. A code constant, not a setting: nothing about it is
# per-instance, and it is not a business rule but a floor under the row size.
# Real flows serialize to a few hundred bytes, so 16KB is ~50x headroom while
# still refusing a payload built to bloat the table:
MAX_ANSWERS_BYTES = 16 * 1024


def _compact_size(payload: Any) -> int:
    """Byte length of the payload as it would be stored (compact JSON)."""
    return len(json.dumps(payload, separators=(",", ":"), default=str).encode("utf-8"))


def _safe_field_errors(error: ValidationError) -> list[dict]:
    """Pydantic errors reduced to LOCATION + TYPE, with every value stripped.

    `ValidationError.errors()` carries the offending input in `input` and often
    inside `msg` too. Those are the user's answers, so the whole error list is
    rebuilt from the two fields that cannot contain them.
    """
    safe: list[dict] = []
    for raw in error.errors():
        location = ".".join(str(part) for part in raw.get("loc", ())) or "answers"
        safe.append({"field": location, "type": str(raw.get("type", "value_error"))})
    return safe


def _validate_submitted_answers(
    flow: QuestionFlow, answers: dict[str, Any]
) -> dict[str, Any]:
    """Validates the questions this submission actually answers, one at a time.

    Per question rather than against the whole model, because that is what makes
    a partial submission legal: validating `{"motivation": "..."}` against a
    three-required-field model would report the two questions the account
    answered last year as missing.

    Every failure in the payload is collected before raising, so a client is told
    about all of its bad answers at once rather than one per round trip. The
    error entries carry a FIELD and a TYPE only — never a submitted value.
    """
    known = flow.answers_schema.model_fields
    validated: dict[str, Any] = {}
    errors: list[dict] = []

    for question_id, value in answers.items():
        if question_id not in known:
            # An id the flow does not ask. The same signal `extra="forbid"` gives
            # on a whole-model validate, and the precise thing a client that is
            # running an older (or newer) build needs to hear:
            errors.append({"field": str(question_id), "type": "extra_forbidden"})
            continue
        if value is None:
            # A null is the absence of an answer (see questions.py). Refused
            # rather than stored, so "answered" cannot mean "sent me a null":
            errors.append({"field": str(question_id), "type": "missing"})
            continue
        adapter = question_adapter(flow, question_id)
        try:
            parsed = adapter.validate_python(value)
        except ValidationError as error:
            for raw in error.errors():
                errors.append({"field": str(question_id), "type": str(raw.get("type", "value_error"))})
            continue
        # Stored in its JSON form, so the answers column holds one shape whether
        # the value arrived through this path or through the full-schema dump
        # below:
        validated[question_id] = adapter.dump_python(parsed, mode="json")

    if errors:
        raise FlowAnswersInvalidException(errors)
    return validated


def submit_response(
    db: Session,
    user: User,
    flow_key: str,
    version: int,
    answers: dict[str, Any],
) -> QuestionFlowResponse:
    """Validates a PARTIAL answer set, merges it forward, and APPENDS a snapshot.

    `answers` may hold any subset of the flow's questions — typically the ones
    the client just asked, which for an existing member after a question is added
    is exactly one. What the account already answered is carried forward under
    it, so the stored row is always that account's full current position and no
    earlier answer is lost by answering something new.

    `version` is ACCEPTED AND IGNORED as a gate. It used to be a 409: a client
    whose version did not match the registry had its whole submission refused. On
    an additive change that is precisely wrong — it throws away answers that are
    still perfectly valid because a question the client had never seen was added
    behind it. A stale client is now caught per question instead: an id the flow
    no longer asks is a 422 naming that id, an answer whose option was retired is
    a 422 naming that question, and everything still valid is kept. The row is
    stamped with the SERVER's version, so history still records which contract
    generation wrote it.
    """
    flow = get_flow(flow_key)
    if flow is None:
        raise FlowNotFoundException()

    if not isinstance(answers, dict) or not answers:
        # An empty submission is not a completion. Refused explicitly so it can
        # never append a row that answers nothing and yet reads as a run:
        raise FlowAnswersInvalidException([{"field": "answers", "type": "missing"}])

    # Capping the RAW body before validation: a bloated payload would be rejected
    # anyway, but only after parsing it:
    if _compact_size(answers) > MAX_ANSWERS_BYTES:
        raise FlowAnswersInvalidException([{"field": "answers", "type": "too_large"}])

    submitted = _validate_submitted_answers(flow, answers)

    # Merge: what is still valid from the last snapshot, with this submission
    # winning wherever the two overlap (re-answering a question is allowed and is
    # how someone corrects an answer). Re-keyed in declaration order so the
    # stored blob's shape does not depend on what order a client sent things in:
    previous = latest_response(db, user.id, flow.key)
    merged = carry_forward_answers(flow, previous.answers if previous is not None else None)
    merged.update(submitted)
    merged = {q: merged[q] for q in flow.question_ids if q in merged}

    if not outstanding_question_ids(flow, merged):
        # The account now owes nothing, so the merged set satisfies the full
        # schema. Validating it as a whole is not redundant: it fills defaults
        # and runs any model-level validator, which per-question validation
        # cannot see. A complete snapshot is therefore byte-identical to what the
        # all-at-once writer produced before flows were granular:
        try:
            parsed = flow.answers_schema.model_validate(merged)
        except ValidationError as error:
            raise FlowAnswersInvalidException(_safe_field_errors(error))
        normalized = parsed.model_dump(mode="json")
    else:
        # A genuinely partial position: every value in it has been validated
        # against its own question, and the questions still missing are reported
        # as outstanding rather than defaulted into looking answered.
        normalized = merged

    if _compact_size(normalized) > MAX_ANSWERS_BYTES:
        raise FlowAnswersInvalidException([{"field": "answers", "type": "too_large"}])

    snapshot = QuestionFlowResponse(
        user_id=user.id,
        flow_key=flow.key,
        version=flow.version,
        answers=normalized,
        completed_at=datetime.now(UTC),
    )

    try:
        db.add(snapshot)
        db.commit()
        db.refresh(snapshot)
    except Exception:
        db.rollback()
        raise

    # Flow key and version only — never the answers:
    logger.info("Question flow %s v%d completed by user %s", flow.key, flow.version, user.id)
    return snapshot


def latest_response(db: Session, user_id: int, flow_key: str) -> QuestionFlowResponse | None:
    """The most recent snapshot for this account and flow, or None.

    Latest-wins over the append-only history: completed_at descending, id
    descending as the tiebreak so two snapshots stamped in the same microsecond
    still resolve deterministically to the later insert.
    """
    return (
        db.query(QuestionFlowResponse)
        .filter(QuestionFlowResponse.user_id == user_id)
        .filter(QuestionFlowResponse.flow_key == flow_key)
        .order_by(QuestionFlowResponse.completed_at.desc(), QuestionFlowResponse.id.desc())
        .first()
    )


def _latest_rows(db: Session, user_id: int) -> dict[str, QuestionFlowResponse]:
    """{flow_key: the account's latest snapshot} in ONE query, for every flow it touched.

    Portable by construction (the test suite runs on SQLite): a GROUP BY
    subquery for the per-flow maximum, joined back for the row - no DISTINCT ON
    and no window function. Exact-timestamp ties can return both rows, so the
    fold walks id ascending and lets the later insert win, matching
    latest_response's ordering - which is what stops the gate read and the detail
    read disagreeing about which answer is current.
    """
    latest = (
        db.query(
            QuestionFlowResponse.flow_key.label("flow_key"),
            func.max(QuestionFlowResponse.completed_at).label("max_completed"),
        )
        .filter(QuestionFlowResponse.user_id == user_id)
        .group_by(QuestionFlowResponse.flow_key)
        .subquery()
    )

    rows = (
        db.query(QuestionFlowResponse)
        .join(
            latest,
            (QuestionFlowResponse.flow_key == latest.c.flow_key)
            & (QuestionFlowResponse.completed_at == latest.c.max_completed),
        )
        .filter(QuestionFlowResponse.user_id == user_id)
        .order_by(QuestionFlowResponse.id.asc())
        .all()
    )
    return {row.flow_key: row for row in rows}


def completion_summary(db: Session, user_id: int) -> dict[str, dict]:
    """Per-flow completion for every flow this account has ANSWERED AT LEAST ONCE.

    Keyed on what the account actually answered, NOT on what is currently
    registered: a retired flow's completion still reports, which is what stops a
    rename or removal from silently re-gating people who already answered. That
    also means a flow the account has never touched is ABSENT rather than present
    with zeros, which the existing frontend gate depends on - "there is a key
    here" is how it reads "this account has onboarded". Use `question_progress`
    for the shape that covers never-answered flows too.

    Each entry carries the old two facts (`version` and `completed_at` of the
    stored snapshot) plus the granular three: which questions it answers, which
    are still outstanding, and whether anything is. A flow whose definition is
    gone reports complete with nothing outstanding - there is no current schema
    to owe anything against.
    """
    summary: dict[str, dict] = {}
    for flow_key, row in _latest_rows(db, user_id).items():
        entry = {"version": row.version, "completed_at": row.completed_at}
        flow = get_flow(flow_key)
        if flow is None:
            entry |= {"answered": [], "outstanding": [], "complete": True}
        else:
            outstanding = outstanding_question_ids(flow, row.answers)
            entry |= {
                "answered": list(answered_question_ids(flow, row.answers)),
                "outstanding": list(outstanding),
                "complete": not outstanding,
            }
        summary[flow_key] = entry
    return summary


def question_progress(db: Session, user_id: int) -> dict[str, dict]:
    """Per-question progress for every REGISTERED flow, answered or not.

    The read the granular gate is built on, and the reason it is separate from
    `completion_summary`: a client deciding what to ask needs the full question
    list and the outstanding subset for a flow the account has never touched,
    which by design has no entry in the summary at all. Here a fresh account
    simply has everything outstanding.

    `version` is the flow's CURRENT registered version (what a client should
    render), not the version of whatever row happens to be stored - that one
    stays in `completion_summary`.
    """
    rows = _latest_rows(db, user_id)
    progress: dict[str, dict] = {}
    for flow in all_flows():
        row = rows.get(flow.key)
        answers = row.answers if row is not None else None
        outstanding = outstanding_question_ids(flow, answers)
        progress[flow.key] = {
            "version": flow.version,
            "questions": list(flow.question_ids),
            "answered": list(answered_question_ids(flow, answers)),
            "outstanding": list(outstanding),
            "complete": not outstanding,
            "completed_at": row.completed_at if row is not None else None,
        }
    return progress


def outstanding_questions(db: Session, user_id: int, flow_key: str) -> tuple[str, ...]:
    """The questions this account still owes for one flow, in the order to ask them.

    An unregistered flow owes nothing: there is no definition to be outstanding
    against, and inventing one would re-gate people over a flow that no longer
    exists.
    """
    flow = get_flow(flow_key)
    if flow is None:
        return ()
    row = latest_response(db, user_id, flow_key)
    return outstanding_question_ids(flow, row.answers if row is not None else None)


def has_answered_all(db: Session, user_id: int, flow_key: str) -> bool:
    """THE granular gate: is there nothing left to ask this account for this flow?

    False the moment a new question is added, for everyone who has not answered
    it - which is the whole point. Contrast `has_completed`, which only asks
    whether the account ever submitted anything at all.
    """
    if get_flow(flow_key) is None:
        return True
    return not outstanding_questions(db, user_id, flow_key)


def has_completed(db: Session, user_id: int, flow_key: str) -> bool:
    """Has this account EVER submitted anything for this flow, at any version?

    Version-blind, and now question-blind too: it is the "has this account been
    through the flow at all" fact, not the gate. Adding a question does not make
    this go False - `has_answered_all` is what turns over. Kept distinct because
    the two have genuinely different consumers: the reminder sweep asks THIS one
    (a member who has been through onboarding must not be nudged again just
    because a question was added months later), while the frontend gate asks the
    granular one.
    """
    return (
        db.query(QuestionFlowResponse.id)
        .filter(QuestionFlowResponse.user_id == user_id)
        .filter(QuestionFlowResponse.flow_key == flow_key)
        .first()
        is not None
    )
