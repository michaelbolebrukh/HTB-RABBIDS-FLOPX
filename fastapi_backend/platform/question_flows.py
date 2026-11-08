"""The single question-flow registry: every structured question set the app asks.

The sibling of platform/capabilities.py, and the same idea: one in-process
registry of valid strings, populated at import time, never a table. A flow
definition is CODE (its questions, its answer schema, its version, its reminder
programme), so it ships and rolls back with the deploy that changed it; only the
ANSWERS are data. That is what makes a flow safe to evolve — there is no second
copy of the definition in a database to drift from the schema validating it.

Platform owns the mechanism; the current product instance owns the definitions
and pushes them in at import time (product/question_flows.py, imported from
product/startup). Platform therefore never imports product, and a clone that
replaces the product layer replaces every flow with it.

The one non-obvious validation is the blast guard. A flow with reminder stages
authorises the backend to email a slice of the user base on a schedule, and the
sweep's audience is "accounts old enough for this stage that never completed the
flow" — which, on the day the feature ships, is EVERY pre-existing account.
`remind_accounts_created_after` bounds that to accounts created after a stated
epoch, so registering reminders without one is refused here rather than
discovered in a mail provider's abuse report.
"""

import re
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Callable

from pydantic import BaseModel
from sqlalchemy import ColumnElement
from sqlalchemy.orm import Session


# Flow keys are stored verbatim on every response row and appear in URLs, so
# they are deliberately narrow: lowercase, no spaces, and short enough for the
# VARCHAR(64) column that holds them.
_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{1,63}$")

# A question id is a schema FIELD NAME, and it is just as public as a flow key:
# it is a JSON key inside every stored snapshot, an element of the outstanding
# list the API returns, and the key a frontend page registers itself under. Held
# to the same shape for the same reason:
_QUESTION_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")


@dataclass(frozen=True)
class ReminderStage:
    """One nudge in a flow's reminder programme, `after` the account was created.

    Stages are identified by their ORDINAL in the flow's tuple, and that ordinal
    is what the sent-record table stores. Reordering or removing a stage
    therefore re-points existing sent-records at a different nudge; append new
    stages at the end, and treat the tuple as an ordered log rather than a set.
    """
    after: timedelta


@dataclass(frozen=True)
class QuestionFlow:
    """A registered question flow: its identity, its contract, and its programme.

    THE QUESTIONS ARE THE ANSWER SCHEMA'S FIELDS. There is no second list to keep
    in step: `answers_schema.model_fields` is the flow's question set, each field
    name is a question's stable id, and the declaration order is the order the
    questions were introduced. Adding a question is adding a field; that one edit
    is what makes it outstanding for every account that has not answered it.

    A field name is therefore a CONTRACT, not a label: it is stored inside every
    snapshot's answers, the frontend keys its page for that question on it, and
    completion is computed per id. Rewording a question is free; renaming its
    field is retiring one question and adding another, and every account is asked
    the new one.
    """

    # Stable external identifier; stored on every snapshot and used in URLs:
    key: str
    # The contract GENERATION. Bumped by the product on a breaking answer-shape
    # change, recorded on every row so a snapshot says which contract wrote it,
    # and used by clients to invalidate a local draft. It is NOT a submission
    # gate and NOT what completion is computed from — see
    # platform/services/question_flow_service/questions.py:
    version: int
    # Pydantic model validating (and normalising) a submission's answers. Must
    # forbid extra keys — an unknown key is a client/server contract mismatch,
    # not payload to store blindly:
    answers_schema: type[BaseModel]
    # Human-readable name, used in reminder email copy:
    title: str

    # Empty = this flow never emails anyone (the common case):
    reminder_stages: tuple[ReminderStage, ...] = ()
    # Same-origin frontend path the reminder's call to action points at:
    reminder_cta_path: str = "/"
    # The blast guard (see the module docstring): reminders are only ever sent to
    # accounts created after this instant. REQUIRED when reminder_stages is set:
    remind_accounts_created_after: datetime | None = None
    # Opts this flow into the dormant pre-account lead module (see
    # platform/services/question_flow_service/leads.py). Default off:
    lead_capture: bool = False
    # Optional extra reminder exclusion, supplied by the PRODUCT because the
    # reason to exclude someone is always product-specific (platform cannot
    # import product tables to build it itself). Given the open session, must
    # return a SQLAlchemy boolean clause correlated to `platform.models.User.id`
    # (typically an `exists()` against a product table); the sweep filters OUT
    # any account matching it, alongside is_verified/suppression. Use this when
    # a different funnel already sends its own "welcome"-shaped email to the
    # same accounts, so this flow's generic reminder would otherwise double up
    # on day one for members who never saw this flow's own entry point:
    exclude_predicate: Callable[[Session], "ColumnElement[bool]"] | None = None

    @property
    def question_ids(self) -> tuple[str, ...]:
        """Every question this flow asks, in declaration order.

        Declaration order is the presentation order AND the append order: a
        question added to the end of the schema is the last one asked, which is
        what makes an additive change read naturally to someone who already
        answered the earlier ones.
        """
        return tuple(self.answers_schema.model_fields.keys())

    @property
    def required_question_ids(self) -> tuple[str, ...]:
        """The questions that can leave an account OUTSTANDING.

        A field with a default is an OPTIONAL question: it may be answered, it is
        reported as answered when it has a value, but its absence never makes the
        flow incomplete and never prompts anyone. That is the only way to add a
        question the product does not want to interrupt existing members for.
        """
        return tuple(
            name for name, field in self.answers_schema.model_fields.items() if field.is_required()
        )


# The registry itself. Module-level and process-local: it is rebuilt from code on
# every boot, so there is nothing to migrate and nothing to keep in sync.
_FLOWS: dict[str, QuestionFlow] = {}


def _validate(flow: QuestionFlow) -> None:
    """Rejects a definition that could not work, at import time rather than at request time."""
    if not isinstance(flow, QuestionFlow):
        raise TypeError("register_flow expects a QuestionFlow.")

    if not _KEY_PATTERN.match(flow.key or ""):
        raise ValueError(
            f"Invalid question flow key {flow.key!r}: expected ^[a-z][a-z0-9_-]{{1,63}}$."
        )

    if not isinstance(flow.version, int) or isinstance(flow.version, bool) or flow.version < 1:
        raise ValueError(f"Question flow {flow.key!r} needs an integer version >= 1.")

    if not (isinstance(flow.answers_schema, type) and issubclass(flow.answers_schema, BaseModel)):
        raise TypeError(f"Question flow {flow.key!r} needs a pydantic BaseModel answers_schema.")

    # Forbidding extra keys is part of the contract, not a style preference: with
    # extras allowed, a client typo lands in the stored snapshot and every later
    # reader has to defend against it:
    if flow.answers_schema.model_config.get("extra") != "forbid":
        raise ValueError(
            f"Question flow {flow.key!r} answers_schema must set model_config extra='forbid'."
        )

    # The schema's fields ARE the questions, so a flow with none asks nothing and
    # could never be outstanding for anybody — almost certainly a wiring mistake
    # (an empty model, or the wrong class passed in) rather than an intent:
    if not flow.answers_schema.model_fields:
        raise ValueError(
            f"Question flow {flow.key!r} answers_schema declares no fields, so the flow asks "
            "no questions. Each field is one question, named by a stable id."
        )

    # Question ids travel into stored JSON keys, API arrays and frontend page
    # keys, so they are held to the same narrowness as flow keys. Refused here,
    # at import time, rather than discovered as an unroutable frontend page:
    for question_id in flow.answers_schema.model_fields:
        if not _QUESTION_ID_PATTERN.match(question_id):
            raise ValueError(
                f"Question flow {flow.key!r} question id {question_id!r} is invalid: "
                "expected ^[a-z][a-z0-9_-]{0,63}$."
            )

    if not (flow.title or "").strip():
        raise ValueError(f"Question flow {flow.key!r} needs a title.")

    if not (flow.reminder_cta_path or "").startswith("/"):
        raise ValueError(
            f"Question flow {flow.key!r} reminder_cta_path must be a same-origin path starting with '/'."
        )

    for stage in flow.reminder_stages:
        if not isinstance(stage, ReminderStage):
            raise TypeError(f"Question flow {flow.key!r} reminder_stages must hold ReminderStage values.")
        if stage.after <= timedelta(0):
            raise ValueError(f"Question flow {flow.key!r} reminder stages must be a positive delay.")

    if flow.reminder_stages:
        epoch = flow.remind_accounts_created_after
        if epoch is None:
            raise ValueError(
                f"Question flow {flow.key!r} declares reminder stages, so it must also declare "
                "remind_accounts_created_after — otherwise the first sweep would mail every "
                "pre-existing account at once."
            )
        if epoch.tzinfo is None:
            raise ValueError(
                f"Question flow {flow.key!r} remind_accounts_created_after must be timezone-aware."
            )


def register_flow(flow: QuestionFlow) -> None:
    """Registers a flow definition. Idempotent for an identical re-registration.

    Import-time registration runs once per process in practice, but module
    re-imports happen (test collection, reload), so re-registering the SAME
    definition is a no-op. Registering a DIFFERENT definition under a key that is
    already taken is refused: two live definitions of one key would mean
    submissions validated against whichever import happened to win.
    """
    _validate(flow)

    # Normalising the stage tuple so a list literal in a product definition still
    # compares equal on re-registration:
    flow = replace(flow, reminder_stages=tuple(flow.reminder_stages))

    existing = _FLOWS.get(flow.key)
    if existing is not None:
        if existing == flow:
            return
        raise ValueError(
            f"Question flow {flow.key!r} is already registered with a different definition."
        )

    _FLOWS[flow.key] = flow


def get_flow(key: str) -> QuestionFlow | None:
    """The registered flow for a key, or None. Never raises — callers choose the 404."""
    return _FLOWS.get(key or "")


def all_flows() -> tuple[QuestionFlow, ...]:
    """Every registered flow, in registration order (what the sweep iterates)."""
    return tuple(_FLOWS.values())


def lead_capture_enabled(key: str) -> bool:
    """Whether the pre-account lead module is live for this flow.

    The lead store is dormant by default and every lead entry point asks this
    first, so an instance that never opts a flow in has no reachable lead
    surface at all (endpoints 404, purge no-ops).
    """
    flow = get_flow(key)
    return bool(flow is not None and flow.lead_capture)


def any_lead_capture_enabled() -> bool:
    """Whether ANY registered flow opts into the lead module (the sweep's gate)."""
    return any(flow.lead_capture for flow in _FLOWS.values())
