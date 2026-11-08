"""The question-flow registry's validation contract.

The registry is the only place a flow definition is checked, and it is checked at
IMPORT time — a bad definition must break the boot that introduced it, not the
first request that happens to hit it. The tests that matter most here are the
refusals: the blast guard (reminders without an epoch), the extra='forbid'
requirement, and the conflicting-re-registration guard.
"""

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import BaseModel, ConfigDict

from platform import question_flows as qf


class Answers(BaseModel):
    model_config = ConfigDict(extra="forbid")
    a: str


class LooseAnswers(BaseModel):
    a: str


EPOCH = datetime(2026, 8, 1, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def isolated_registry():
    """Snapshot/restore so a test never sees another test's (or the product's) flows."""
    saved = dict(qf._FLOWS)
    qf._FLOWS.clear()
    try:
        yield
    finally:
        qf._FLOWS.clear()
        qf._FLOWS.update(saved)


def _flow(**overrides) -> qf.QuestionFlow:
    defaults = dict(key="demo", version=1, answers_schema=Answers, title="Demo")
    defaults.update(overrides)
    return qf.QuestionFlow(**defaults)


# -- happy path ----------------------------------------------------------------

def test_registered_flow_is_readable_by_key_and_in_all_flows():
    flow = _flow()
    qf.register_flow(flow)
    # Equal, not identical: the registry normalises the stage tuple on the way
    # in, so what it holds is a value-equal copy:
    assert qf.get_flow("demo") == flow
    assert qf.all_flows() == (flow,)


def test_unknown_key_reads_as_none_rather_than_raising():
    # Callers own the 404; the registry never raises on a read:
    assert qf.get_flow("nope") is None
    assert qf.get_flow("") is None
    assert qf.lead_capture_enabled("nope") is False


# -- key / version / schema validation ----------------------------------------

@pytest.mark.parametrize("key", ["", "A", "1abc", "x", "has space", "trailing-" + "x" * 70, "Ünicode"])
def test_invalid_keys_are_refused(key):
    with pytest.raises(ValueError):
        qf.register_flow(_flow(key=key))


@pytest.mark.parametrize("version", [0, -1, True])
def test_version_must_be_a_positive_integer(version):
    with pytest.raises(ValueError):
        qf.register_flow(_flow(version=version))


def test_answers_schema_must_forbid_extra_keys():
    # Extras allowed would let a client typo land in the stored snapshot:
    with pytest.raises(ValueError):
        qf.register_flow(_flow(answers_schema=LooseAnswers))


def test_answers_schema_must_be_a_pydantic_model():
    with pytest.raises(TypeError):
        qf.register_flow(_flow(answers_schema=dict))


def test_title_is_required():
    with pytest.raises(ValueError):
        qf.register_flow(_flow(title="   "))


# -- the question set (the schema's fields) ------------------------------------

class ThreeQuestions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    year: str
    experience: str
    referrer: str = "unknown"


class NoQuestions(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BadlyNamedQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    Year_Of_Study: str


def test_the_questions_are_the_schemas_fields_in_declaration_order():
    """There is no second list of questions to drift from the schema, and the
    order is the order they were declared — which is the order they are asked."""
    flow = _flow(answers_schema=ThreeQuestions)
    assert flow.question_ids == ("year", "experience", "referrer")


def test_only_fields_without_a_default_can_leave_an_account_outstanding():
    """A field with a default is an optional question: offered, never an
    interruption. That is the one way to add a question without prompting
    existing members for it."""
    flow = _flow(answers_schema=ThreeQuestions)
    assert flow.required_question_ids == ("year", "experience")


def test_a_schema_with_no_fields_is_refused():
    # A flow that asks nothing can never be outstanding for anybody, so it is
    # a wiring mistake rather than an intent:
    with pytest.raises(ValueError):
        qf.register_flow(_flow(answers_schema=NoQuestions))


def test_a_question_id_that_is_not_a_safe_identifier_is_refused():
    # Question ids are stored JSON keys, API array elements and frontend page
    # keys — held to the same narrowness as a flow key, at import time:
    with pytest.raises(ValueError):
        qf.register_flow(_flow(answers_schema=BadlyNamedQuestion))


def test_reminder_cta_path_must_be_a_same_origin_path():
    with pytest.raises(ValueError):
        qf.register_flow(_flow(reminder_cta_path="https://elsewhere.example/welcome"))


# -- THE blast guard -----------------------------------------------------------

def test_reminder_stages_without_an_epoch_are_refused():
    """Without the epoch, the first sweep would mail every pre-existing account."""
    with pytest.raises(ValueError, match="remind_accounts_created_after"):
        qf.register_flow(_flow(reminder_stages=(qf.ReminderStage(after=timedelta(days=1)),)))


def test_reminder_stages_with_an_epoch_register():
    flow = _flow(
        reminder_stages=(qf.ReminderStage(after=timedelta(days=1)),),
        remind_accounts_created_after=EPOCH,
    )
    qf.register_flow(flow)
    assert qf.get_flow("demo").reminder_stages[0].after == timedelta(days=1)


def test_the_epoch_must_be_timezone_aware():
    with pytest.raises(ValueError, match="timezone-aware"):
        qf.register_flow(_flow(
            reminder_stages=(qf.ReminderStage(after=timedelta(days=1)),),
            remind_accounts_created_after=datetime(2026, 8, 1),
        ))


def test_a_flow_with_no_stages_needs_no_epoch():
    qf.register_flow(_flow())
    assert qf.get_flow("demo").remind_accounts_created_after is None


@pytest.mark.parametrize("delay", [timedelta(0), timedelta(seconds=-1)])
def test_reminder_stage_delays_must_be_positive(delay):
    with pytest.raises(ValueError):
        qf.register_flow(_flow(
            reminder_stages=(qf.ReminderStage(after=delay),),
            remind_accounts_created_after=EPOCH,
        ))


# -- re-registration -----------------------------------------------------------

def test_identical_re_registration_is_idempotent():
    qf.register_flow(_flow())
    qf.register_flow(_flow())
    assert len(qf.all_flows()) == 1


def test_a_list_of_stages_still_compares_equal_on_re_registration():
    # Product definitions may pass a list; the registry normalises to a tuple so
    # a re-import does not look like a conflicting definition:
    stages = [qf.ReminderStage(after=timedelta(days=1))]
    qf.register_flow(_flow(reminder_stages=stages, remind_accounts_created_after=EPOCH))
    qf.register_flow(_flow(reminder_stages=stages, remind_accounts_created_after=EPOCH))
    assert len(qf.all_flows()) == 1


def test_conflicting_re_registration_is_refused():
    qf.register_flow(_flow(version=1))
    with pytest.raises(ValueError, match="already registered"):
        qf.register_flow(_flow(version=2))
    # The first definition survives untouched:
    assert qf.get_flow("demo").version == 1


# -- lead capture gate ---------------------------------------------------------

def test_lead_capture_is_off_by_default():
    qf.register_flow(_flow())
    assert qf.lead_capture_enabled("demo") is False
    assert qf.any_lead_capture_enabled() is False


def test_lead_capture_reads_through_when_a_flow_opts_in():
    qf.register_flow(_flow(lead_capture=True))
    assert qf.lead_capture_enabled("demo") is True
    assert qf.any_lead_capture_enabled() is True
