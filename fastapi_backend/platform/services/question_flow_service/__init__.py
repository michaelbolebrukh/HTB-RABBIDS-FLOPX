"""Question-flow capture: completed answer snapshots, reminders, and dormant leads.

The public surface of the package. Callers (the /flows router, GET /user/,
signup, the lifespan hook) import from here; the split into service / reminders /
leads is internal.

The mental model, in one line: flow DEFINITIONS are code
(platform/question_flows.py), flow ANSWERS are append-only data, and everything
else in this package is either a read over that data or a schedule computed from
it.

A flow is asked PER QUESTION, not as a block. `questions.py` holds the single
definition of when a question counts as answered, and `service.py` writes
partial submissions that merge forward, so adding a question to a flow's schema
asks existing members that one question and nothing else. See this package's
DOCS.md for the operating procedure.
"""

from platform.services.question_flow_service.leads import (
    LEAD_RETENTION_DAYS,
    build_resume_url,
    capture_lead,
    delete_leads_for_email,
    mint_resume_token,
    on_account_created,
    purge_expired_leads,
    resume_lead,
)
from platform.services.question_flow_service.reminders import (
    SWEEP_INTERVAL_SECONDS,
    run_sweep,
    start_question_flow_sweep_loop,
    sweep_is_enabled,
)
from platform.services.question_flow_service.qa_reset import (
    find_user_by_email,
    reset_flow_state,
)
from platform.services.question_flow_service.questions import (
    answer_is_valid,
    answered_question_ids,
    carry_forward_answers,
    outstanding_question_ids,
)
from platform.services.question_flow_service.service import (
    MAX_ANSWERS_BYTES,
    completion_summary,
    has_answered_all,
    has_completed,
    latest_response,
    outstanding_questions,
    question_progress,
    submit_response,
)


__all__ = [
    # Snapshots:
    "submit_response",
    "latest_response",
    "completion_summary",
    "has_completed",
    "MAX_ANSWERS_BYTES",
    # Per-question completion (the granular gate):
    "question_progress",
    "outstanding_questions",
    "has_answered_all",
    "answered_question_ids",
    "outstanding_question_ids",
    "carry_forward_answers",
    "answer_is_valid",
    # Reminders:
    "start_question_flow_sweep_loop",
    "run_sweep",
    "sweep_is_enabled",
    "SWEEP_INTERVAL_SECONDS",
    # Dormant lead module:
    "on_account_created",
    "capture_lead",
    "resume_lead",
    "purge_expired_leads",
    "delete_leads_for_email",
    "mint_resume_token",
    "build_resume_url",
    "LEAD_RETENTION_DAYS",
    # TODO TEMP (QA harness) — the only deletes in the package; see qa_reset.py:
    "reset_flow_state",
    "find_user_by_email",
]
