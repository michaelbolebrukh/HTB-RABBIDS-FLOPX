"""The hourly question-flow reminder sweep.

Structurally a clone of payment_services/reconcile_scheduler: an asyncio task
started from the app lifespan, sleep-first, `asyncio.to_thread` for the blocking
DB + provider work, a non-blocking PostgreSQL advisory lock per tick so exactly
one worker sweeps, and fail-open throughout.

Why a sweep rather than scheduling one message per signup: a stage's audience is
"accounts that STILL have not answered", which is a fact about the present, not
about signup time. Anything scheduled at signup would have to be cancelled the
moment someone answers, and a missed cancellation is an email telling a user to
do something they already did. Recomputing the audience each tick makes that
class of bug unrepresentable — completing the flow removes you from every
future stage by construction.

At-most-once is enforced by CLAIM-FIRST: the sent-record row is inserted and
committed BEFORE the send, and the unique constraint on
(user_id, flow_key, stage) is what makes a concurrent worker, a retry, or a
crashed tick unable to produce a second email. The deliberate consequence is
that a send failing after its claim is FORFEITED rather than retried — a missing
nudge is a nothing; a duplicate nudge is a complaint.

At-most-once PER STAGE is not enough on its own, though: an account can become
due for several stages at once (a late verification, a provider wired after
launch, an outage that skipped ticks), and firing each of them would deliver a
burst of near-identical nudges in one minute. So a tick also enforces AT MOST
ONE EMAIL PER (user, flow): stages are walked MOST-ADVANCED FIRST, and the
lower stages an account has overshot are still CLAIMED — without sending. The
claim is the record of the decision, so a superseded stage can never fire on a
later tick either.
"""

import asyncio
import logging
import zlib
from datetime import UTC, datetime

from sqlalchemy import func, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from infrastructure.database import SessionLocal, engine
from infrastructure.settings import settings as s
from platform.enums import EmailTemplateType
from platform.models import (
    MarketingSuppression,
    QuestionFlowReminder,
    QuestionFlowResponse,
    User,
)
from platform.question_flows import QuestionFlow, all_flows
from platform.services import marketing_service
from platform.services.email_services import provider_is_configured, send_templated_email
from platform.services.question_flow_service import leads


logger = logging.getLogger(__name__)


# Hourly. A code constant, not an env var: reminder stages are expressed in days,
# so the only thing this number controls is how promptly a stage fires within its
# day — nothing per-instance, and a change here is a code change:
SWEEP_INTERVAL_SECONDS = 3600

# Ceiling on sends per (flow, stage) per tick. Bounds the blast radius of a
# mistake to 200 emails an hour and keeps one tick's work bounded; a genuine
# backlog simply drains over the following ticks:
REMINDER_BATCH_SIZE = 200

# Distinct from the migration and reconcile advisory-lock ids so the three never
# collide:
_LOCK_ID = zlib.crc32(b"question_flow_sweep") & 0x7FFFFFFF


def _reminder_flows() -> tuple[QuestionFlow, ...]:
    return tuple(flow for flow in all_flows() if flow.reminder_stages)


def sweep_is_enabled() -> bool:
    """Whether the sweep loop should run at all.

    Any registered flow enables it — not just flows with reminder stages or
    live lead capture — because the tick also owns retention housekeeping:
    lead rows captured while a flow's capture was on must still be purged
    after it is switched off. With no flows registered nothing can ever have
    been captured, so staying dormant is safe.
    """
    return bool(all_flows())


def _cta_url(flow: QuestionFlow) -> str:
    origin = (s.PUBLIC_FRONTEND_ORIGIN or f"https://{s.APP_DOMAIN}").rstrip("/")
    return f"{origin}{flow.reminder_cta_path}"


def _due_users(
    db: Session, flow: QuestionFlow, stage_index: int, now: datetime
) -> list[tuple[int, str, str]]:
    """Accounts owed this stage's nudge right now, as plain (id, email, name) rows.

    Deliberately NOT ORM instances: the claim below commits, which would expire
    every loaded `User` and turn the loop into one re-SELECT per recipient — and
    make an account deleted mid-tick abort the whole sweep. Three scalars read
    once are all the send needs.

    Every clause is a guard rather than a filter:
      * is_verified        — unverified accounts are owned by the verification
                             mail; adding a second stream to an address that has
                             not confirmed itself is how a domain gets a
                             reputation problem.
      * created_at <= cutoff        — the stage's delay has actually elapsed.
      * created_at >= flow epoch    — the BLAST GUARD (see question_flows.py):
                             without it, shipping a flow would mail the entire
                             pre-existing user base on the first tick.
      * no response                 — completing the flow removes an account from
                             every future stage, with nothing to cancel.
      * no sent-record              — the durable at-most-once backstop.
      * not suppressed              — courtesy, not a legal gate (these are
                             transactional-by-basis), but a real one. It is a
                             clause rather than a post-filter for a reason: a
                             suppressed account skipped in Python would still
                             consume a slot in the ORDER BY id LIMIT batch, and
                             a handful of low-id suppressed accounts would
                             starve everyone behind them forever.
      * flow.exclude_predicate      — same starvation argument, for a
                             product-specific reason to skip an account (e.g. it
                             already got a funnel-specific welcome email of its
                             own). Same clause-not-post-filter treatment.
    """
    stage = flow.reminder_stages[stage_index]
    cutoff = now - stage.after

    already_answered = (
        db.query(QuestionFlowResponse.id)
        .filter(QuestionFlowResponse.user_id == User.id)
        .filter(QuestionFlowResponse.flow_key == flow.key)
        .exists()
    )
    already_reminded = (
        db.query(QuestionFlowReminder.id)
        .filter(QuestionFlowReminder.user_id == User.id)
        .filter(QuestionFlowReminder.flow_key == flow.key)
        .filter(QuestionFlowReminder.stage == stage_index)
        .exists()
    )
    # Suppressions are stored already normalized, so only the user side needs
    # lowering — which also leaves the unique index on the suppression column
    # usable:
    is_suppressed = (
        db.query(MarketingSuppression.id)
        .filter(MarketingSuppression.email == func.lower(User.email))
        .exists()
    )

    query = (
        db.query(User.id, User.email, User.name)
        .filter(User.is_verified.is_(True))
        .filter(User.created_at <= cutoff)
        .filter(User.created_at >= flow.remind_accounts_created_after)
        .filter(~already_answered)
        .filter(~already_reminded)
        .filter(~is_suppressed)
    )
    if flow.exclude_predicate is not None:
        query = query.filter(~flow.exclude_predicate(db))

    return [
        (row.id, row.email, row.name)
        for row in query.order_by(User.id.asc()).limit(REMINDER_BATCH_SIZE).all()
    ]


def _claim(db: Session, user_id: int, flow: QuestionFlow, stage_index: int) -> bool:
    """Inserts and commits the sent-record BEFORE sending. False = someone else won."""
    record = QuestionFlowReminder(user_id=user_id, flow_key=flow.key, stage=stage_index)
    try:
        db.add(record)
        db.commit()
        return True
    except IntegrityError:
        # The unique constraint fired: a concurrent worker (or an earlier tick
        # racing this one) already owns this send:
        db.rollback()
        return False
    except Exception:
        db.rollback()
        logger.warning("Could not claim reminder stage %d of %s.", stage_index, flow.key,
                       exc_info=True)
        return False


def _send(user_id: int, email: str, name: str, flow: QuestionFlow) -> None:
    """Sends one reminder. A failure is logged and FORFEITED, never retried."""
    # The working stop link: the same HASH_KEY-signed unsubscribe page the
    # suppression clause above reads, so "stop these" is a real, auth-free
    # control rather than a decorative line:
    stop_url = marketing_service.build_unsubscribe_page_url(user_id)
    try:
        send_templated_email(
            template_type=EmailTemplateType.QUESTION_FLOW_REMINDER,
            recipient=email,
            name=name,
            flow_title=flow.title,
            cta_url=_cta_url(flow),
            stop_url=stop_url,
            # RFC 8058: the same stop link promoted to a header so the mail
            # client's native Unsubscribe control works. The page URL only (no
            # List-Unsubscribe-Post) — it is a human confirm page, not a
            # one-click POST target, matching email_sender._unsubscribe_headers:
            headers={"List-Unsubscribe": f"<{stop_url}>"},
        )
    except Exception:
        # Degraded-but-handled: the claim stands, so this nudge is skipped rather
        # than becoming a duplicate on the next tick:
        logger.warning("Question flow reminder send failed (flow=%s, user=%s).",
                       flow.key, user_id, exc_info=True)


def run_sweep(
    db: Session, *, now: datetime | None = None, send_reminders: bool = True
) -> int:
    """One full pass over every flow and stage. Returns the number sent.

    Session-first and synchronous so it is directly callable from a test; the
    loop below is the only thing that knows about asyncio. `send_reminders`
    is False when no email provider is wired — housekeeping still runs.
    """
    now = now or datetime.now(UTC)
    sent = 0

    for flow in _reminder_flows() if send_reminders else ():
        # One email per account per flow per tick. Stages are walked most-advanced
        # first so the message an account gets is the one matching how overdue it
        # actually is, rather than the oldest nudge in the sequence:
        emailed: set[int] = set()
        for stage_index in reversed(range(len(flow.reminder_stages))):
            for user_id, email, name in _due_users(db, flow, stage_index, now):
                if not _claim(db, user_id, flow, stage_index):
                    continue
                if user_id in emailed:
                    # Superseded by the more advanced stage already sent this
                    # tick. The claim above is the point: it records the decision
                    # durably, so this stage never fires on a later tick either:
                    continue
                _send(user_id, email, name, flow)
                emailed.add(user_id)
                sent += 1

    # Retention housekeeping rides on the same tick. Unconditional by design —
    # rows captured while a flow had lead capture on must still be collected
    # after it is switched off — and one cheap indexed DELETE when the table is
    # empty, which is the dormant case:
    leads.purge_expired_leads(db, now=now)

    return sent


def _run_one_sweep() -> None:
    """One tick, under a per-tick advisory lock (sync; DB + provider I/O)."""
    if not sweep_is_enabled():
        return
    # With no provider wired, sending would burn queries every hour just to log
    # failures — so only the reminder portion is skipped. Retention housekeeping
    # (lead purge) needs no provider and always rides the tick:
    send_reminders = provider_is_configured()
    if not send_reminders:
        logger.debug("Question flow sweep: no email provider; housekeeping only.")

    is_postgres = engine.dialect.name == "postgresql"
    with SessionLocal() as db:
        if is_postgres:
            acquired = db.execute(
                text("SELECT pg_try_advisory_lock(:lock_id)"), {"lock_id": _LOCK_ID}
            ).scalar()
            if not acquired:
                # Another worker/instance is sweeping this tick:
                return
        try:
            sent = run_sweep(db, send_reminders=send_reminders)
            if sent:
                logger.info("Question flow sweep sent %d reminder(s).", sent)
        finally:
            if is_postgres:
                db.execute(text("SELECT pg_advisory_unlock(:lock_id)"), {"lock_id": _LOCK_ID})
                db.commit()


async def _sweep_loop(interval_seconds: int) -> None:
    while True:
        # Sleeping first so boot is never delayed and workers desynchronise:
        await asyncio.sleep(interval_seconds)
        try:
            await asyncio.to_thread(_run_one_sweep)
        except Exception:
            logger.exception("Question flow sweep failed; continuing.")


def start_question_flow_sweep_loop() -> asyncio.Task | None:
    """Starts the sweep as a background task, or returns None when there is nothing to do.

    Called from the app lifespan (inside the running event loop). Cancel the
    returned task on shutdown. Returns None unless some registered flow declares
    reminder stages or opts into lead capture — a clone whose product registers
    neither pays nothing for this feature.
    """
    if not sweep_is_enabled():
        return None
    logger.info("Starting question flow sweep loop (every %ds).", SWEEP_INTERVAL_SECONDS)
    return asyncio.create_task(_sweep_loop(SWEEP_INTERVAL_SECONDS))
