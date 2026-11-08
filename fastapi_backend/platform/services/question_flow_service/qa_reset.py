"""TODO TEMP (QA harness) — remove before launch, with the
POST /flows/{flow_key}/test/reset route and the `flows:qa-reset` capability.

CAVEAT ON "remove before launch": that route now backs a product-side operator
control (the admin overlay's "Reset onboarding answers" button). The three
pieces must go together, or the button 404s silently. See the note on the route.

The ONLY module in the codebase that deletes question-flow state. It exists so a
flow can be re-run end to end with the SAME account during testing: without it
the first completion is permanent (snapshots are append-only by contract) and
onboarding can never be seen a second time.

It is deliberately a separate module rather than a function in `service.py`:
that module states the append-only invariant, and the carve-out belongs where it
is visibly temporary and trivially deletable — one file, one route, one grant.

WHAT A RESET CLEARS, and why each one is required for the flow to actually
re-appear rather than merely "look" reset:

  * question_flow_responses (user, flow)   THE gate. `completion_summary` builds
        `user.flows` from these rows, which is what the frontend onboarding gate
        reads; `completed_at` lives on the row, so it goes with it.
  * question_flow_reminders (user, flow)   The at-most-once CLAIMS. Not a gate on
        showing the flow, but a claimed stage can never fire again, so leaving
        them behind would make the reminder programme unrepeatable — a reset that
        silently disables half the feature under test.
  * question_flow_leads (email, flow)      The pre-account partial capture for the
        account's own address. Not a gate either, but a surviving row is a stale
        draft the next pre-account run would resume from.

WHAT IT DELIBERATELY DOES NOT TOUCH:

  * Anything a product built ON TOP of the answers (course skips granted, tracks
    unlocked, overlay dismissals, XP, achievements). Platform must not import
    product, and those rows are consequences the account accepted, not flow
    state. Note the practical consequence for QA: a derived recommendation
    recomputes from scratch (there is no stored recommendation), but a skip that
    was already ACCEPTED stays accepted.
  * The account itself — verification, password, sessions, marketing consent.
  * Any other flow's rows: every delete is scoped to (this account, this flow).
"""

import logging

from sqlalchemy.orm import Session

from platform.models import (
    QuestionFlowLead,
    QuestionFlowReminder,
    QuestionFlowResponse,
    User,
)


logger = logging.getLogger(__name__)


def reset_flow_state(db: Session, user: User | None, flow_key: str) -> dict[str, int]:
    """Deletes every trace of one account's participation in one flow.

    `user` may be None — the caller resolved a target address that has no
    account. That is NOT an error here: the reset endpoint answers identically
    for an unknown address and for an address with nothing to reset, so it can
    never be used to test which addresses exist. Nothing to delete is simply
    zero counts.

    One transaction: either the whole account/flow pairing is gone or nothing
    is, so a half-reset (responses cleared, reminder claims kept) is not a state
    the caller can land in.
    """
    counts = {"responses": 0, "reminders": 0, "leads": 0}
    if user is None:
        return counts

    try:
        counts["responses"] = (
            db.query(QuestionFlowResponse)
            .filter(QuestionFlowResponse.user_id == user.id)
            .filter(QuestionFlowResponse.flow_key == flow_key)
            .delete(synchronize_session=False)
        )
        counts["reminders"] = (
            db.query(QuestionFlowReminder)
            .filter(QuestionFlowReminder.user_id == user.id)
            .filter(QuestionFlowReminder.flow_key == flow_key)
            .delete(synchronize_session=False)
        )
        # Leads are address-keyed (they predate the account), and stored already
        # normalized — so the account's own address is lowered to match:
        email = (user.email or "").strip().lower()
        if email:
            counts["leads"] = (
                db.query(QuestionFlowLead)
                .filter(QuestionFlowLead.email == email)
                .filter(QuestionFlowLead.flow_key == flow_key)
                .delete(synchronize_session=False)
            )
        db.commit()
    except Exception:
        db.rollback()
        raise

    # Ids and counts only — never the answers, and never the target's address:
    logger.info(
        "QA reset of flow %s for user %s (responses=%d, reminders=%d, leads=%d).",
        flow_key, user.id, counts["responses"], counts["reminders"], counts["leads"],
    )
    return counts


def find_user_by_email(db: Session, email: str) -> User | None:
    """Resolves a target address to an account, or None. Never raises.

    Normalized the same way registration stores it. The caller must NOT turn
    None into a 404 — see `reset_flow_state`.
    """
    normalized = (email or "").strip().lower()
    if not normalized:
        return None
    return db.query(User).filter(User.email == normalized).first()
