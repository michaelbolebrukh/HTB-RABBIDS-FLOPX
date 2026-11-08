"""Per-question completion: what a flow asks, and what one account has answered.

This module is the whole reason onboarding is granular rather than all-or-nothing.
It holds ONE definition — when is a question answered — and every gate, every
summary and every submission resolves through it, so "outstanding" cannot mean
one thing to the API and another to the writer.

THE DEFINITION. A question is answered when the account's latest snapshot holds a
value under that question's id AND that value still validates against the
question's CURRENT annotation. Nothing is stored per question; it is derived on
read from rows that were already keyed by question id. Four behaviours fall out
of that single rule, and none of them needed a schema change or a migration:

  * ADDING a question. Nobody has a value for the new id, so it is outstanding
    for every existing account and for every new one. Everything they already
    answered stays answered — they are asked the new question and nothing else.
  * CHANGING a question's vocabulary in a BREAKING way (renaming an answer id,
    dropping an option). The stored value stops validating, so that ONE question
    goes outstanding again while its neighbours are untouched. This is the
    granular replacement for "bump the version and make everyone start over".
  * REMOVING a question. The id is no longer a field, so it is asked of nobody
    and reported to nobody. Old rows keep the value; they are simply not read.
  * RETIRING a whole flow. A snapshot whose flow is no longer registered has no
    current schema to check against, so callers report it complete rather than
    re-gating people who answered something that no longer exists.

WHY DERIVED AND NOT STORED. A per-question table would be a second copy of a fact
the snapshot already carries, and the two would drift the first time a row was
written by an older deploy. Deriving costs a handful of cached validations per
read of GET /user/ and can never disagree with the answers themselves.

NULL IS NOT AN ANSWER. A stored `None` counts as unanswered whatever the
annotation permits, because a client that omits a question and one that sends a
null mean the same thing and must not produce different gates. A flow that wants
"prefer not to say" declares it as an explicit answer id, which is a real answer
and reads as one in every downstream consumer.
"""

from functools import lru_cache
from typing import Any

from pydantic import BaseModel, TypeAdapter, ValidationError

from platform.question_flows import QuestionFlow


@lru_cache(maxsize=512)
def _adapter(schema: type[BaseModel], question_id: str) -> TypeAdapter:
    """A validator for ONE question, cached per (schema class, field).

    Per-field rather than whole-model because questions are answered
    independently: validating a partial submission against the full model would
    report every unanswered question as a missing-field error. The cache is keyed
    on the schema CLASS, so redefining a flow's schema (a reload, a test) simply
    produces new entries rather than serving a stale contract.
    """
    return TypeAdapter(schema.model_fields[question_id].annotation)


def question_adapter(flow: QuestionFlow, question_id: str) -> TypeAdapter | None:
    """The validator for one of this flow's questions, or None if it asks no such thing."""
    if question_id not in flow.answers_schema.model_fields:
        return None
    return _adapter(flow.answers_schema, question_id)


def answer_is_valid(flow: QuestionFlow, question_id: str, value: Any) -> bool:
    """Whether `value` is a usable answer to `question_id` under the CURRENT schema."""
    adapter = question_adapter(flow, question_id)
    if adapter is None or value is None:
        return False
    try:
        adapter.validate_python(value)
    except ValidationError:
        return False
    return True


def answered_question_ids(flow: QuestionFlow, answers: Any) -> tuple[str, ...]:
    """The questions this answer blob genuinely answers, in the flow's own order.

    Tolerant of anything in the `answers` column on purpose: a row written years
    ago, by a different deploy, is input to be read rather than a shape to trust.
    """
    if not isinstance(answers, dict):
        return ()
    return tuple(q for q in flow.question_ids if answer_is_valid(flow, q, answers.get(q)))


def outstanding_question_ids(flow: QuestionFlow, answers: Any) -> tuple[str, ...]:
    """The questions this account still owes, in the order they should be asked.

    Only REQUIRED questions can be outstanding — an optional question (a schema
    field with a default) is offered but never interrupts anyone.
    """
    answered = set(answered_question_ids(flow, answers))
    return tuple(q for q in flow.required_question_ids if q not in answered)


def carry_forward_answers(flow: QuestionFlow, answers: Any) -> dict[str, Any]:
    """The stored answers worth carrying into the next snapshot.

    Exactly the ones that are still asked and still valid. Dropping the rest is
    what turns a breaking vocabulary change into "re-ask that one question"
    instead of "write an invalid value forward forever": a value that no longer
    validates is not carried, so the question shows up as outstanding and the
    next snapshot is clean. The dropped value is not destroyed — the row it came
    from is append-only and still holds it.
    """
    if not isinstance(answers, dict):
        return {}
    return {q: answers[q] for q in answered_question_ids(flow, answers)}
