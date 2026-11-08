"""Pre-account answer capture — a DORMANT module, off unless a flow opts in.

Nothing here is WRITABLE unless some registered flow sets `lead_capture=True`.
Every public entry point (capture, resume) asks `lead_capture_enabled` first and
raises the ordinary "no such flow" 404 when it is off, so a dormant surface is
indistinguishable from an absent one and an instance that never opts in has no
lead surface to attack and no rows to protect.

The HOUSEKEEPING paths (`purge_expired_leads`, `delete_leads_for_email`) are
deliberately NOT gated on that switch. Rows outlive the switch that created
them: a flow enabled for a week and then turned off would otherwise leave real
personal data with no purge path and no conversion delete, i.e. exactly the
orphaned-PII case the 30-day retention promise exists to prevent. Deleting is
always safe; not deleting is not.

It exists for one ordering: a visitor answers questions BEFORE creating an
account. Those answers can only be keyed by the address they typed, so the row
is address-keyed and its lawful basis is Art 6(1)(b) — steps taken at the data
subject's request prior to entering a contract. It is NOT a marketing list and
must never be used as one; separate consent (marketing_consent /
marketing_suppressions) governs that entirely.

Two mechanisms keep the store from becoming a shadow CRM:
  * conversion DELETES the row (the answers move to question_flow_responses,
    where they belong to an account that can see and erase them);
  * unconverted rows are purged after LEAD_RETENTION_DAYS by the sweep.

Resume tokens follow the marketing_service pattern exactly — an HMAC over
HASH_KEY, so no new secret and no new env var (`l1.<lead_id>.<sig>`).
"""

import hashlib
import hmac
import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any, Optional

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from infrastructure.settings import settings as s
from platform.exceptions import FlowNotFoundException, InvalidResumeTokenException
from platform.models import QuestionFlowLead
from platform.question_flows import get_flow, lead_capture_enabled
from platform.services.marketing_service import masked_email


logger = logging.getLogger(__name__)


# How long an unconverted lead may sit before the sweep deletes it. A code
# constant, not an env var: it is a retention promise made in the privacy policy,
# so it belongs in code review, not in a dashboard field:
LEAD_RETENTION_DAYS = 30

# Partial answers are unvalidated by design, so the size cap is the only bound
# on what a public endpoint can write:
MAX_LEAD_ANSWERS_BYTES = 16 * 1024

_TOKEN_VERSION = "l1"
_TOKEN_PURPOSE = "question-flow-lead-resume"


def _normalize(email: str) -> str:
    return (email or "").strip().lower()


def _require_enabled(flow_key: str) -> None:
    """The dormancy gate every public lead path passes through first."""
    if not lead_capture_enabled(flow_key):
        raise FlowNotFoundException()


# -- Resume tokens (HASH_KEY-derived, no new secret) ---------------------------

def _signature(lead_id: int) -> str:
    message = f"{_TOKEN_PURPOSE}:{_TOKEN_VERSION}:{lead_id}".encode()
    return hmac.new(s.HASH_KEY.encode(), message, hashlib.sha256).hexdigest()[:32]


def mint_resume_token(lead_id: int) -> str:
    return f"{_TOKEN_VERSION}.{lead_id}.{_signature(lead_id)}"


def _verify_resume_token(token: str) -> Optional[int]:
    parts = (token or "").split(".")
    if len(parts) != 3 or parts[0] != _TOKEN_VERSION:
        return None
    try:
        lead_id = int(parts[1])
    except ValueError:
        return None
    # Comparing BYTES: `compare_digest` on str raises TypeError for any
    # non-ASCII character, and a caller-supplied token can contain anything —
    # that would turn a bad token into a 500 and break the uniform 404:
    if not hmac.compare_digest(parts[2].encode(), _signature(lead_id).encode()):
        return None
    return lead_id


def build_resume_url(lead_id: int, resume_path: str) -> str:
    """The link a resume email would carry (frontend page, token in the query).

    When that email is actually wired, its stop link cannot come from
    `marketing_service.build_unsubscribe_page_url` — a lead has no user id. The
    address-keyed producer is `marketing_service.build_email_unsubscribe_page_url`:
    """
    origin = (s.PUBLIC_FRONTEND_ORIGIN or f"https://{s.APP_DOMAIN}").rstrip("/")
    return f"{origin}{resume_path}?token={mint_resume_token(lead_id)}"


# -- Capture / resume ----------------------------------------------------------

def _capped(answers: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    if answers is None:
        return None
    size = len(json.dumps(answers, separators=(",", ":"), default=str).encode("utf-8"))
    if size > MAX_LEAD_ANSWERS_BYTES:
        # Dropping the payload rather than raising: the endpoint is always-202
        # (enumeration-safe), so a failure here must not become a signal:
        logger.warning("Discarding oversized lead answers (%d bytes).", size)
        return None
    return answers


def capture_lead(
    db: Session,
    flow_key: str,
    email: str,
    version: int,
    answers: Optional[dict[str, Any]] = None,
) -> QuestionFlowLead:
    """Upserts the one lead row for (address, flow). Last write wins.

    Upsert rather than append: unlike a completed snapshot, a partial answer set
    has no history worth keeping, and one row per address is what makes both the
    retention purge and the conversion delete a single, obviously-complete
    statement.
    """
    _require_enabled(flow_key)
    email = _normalize(email)
    payload = _capped(answers)

    existing = (
        db.query(QuestionFlowLead)
        .filter(QuestionFlowLead.email == email)
        .filter(QuestionFlowLead.flow_key == flow_key)
        .first()
    )

    try:
        if existing is not None:
            existing.version = version
            existing.answers = payload
            db.commit()
            db.refresh(existing)
            return existing

        lead = QuestionFlowLead(email=email, flow_key=flow_key, version=version, answers=payload)
        db.add(lead)
        db.commit()
        db.refresh(lead)
        return lead

    except IntegrityError:
        # Another request created the row between the read and the insert; the
        # unique constraint made that safe, so re-read and apply this write:
        db.rollback()
        lead = (
            db.query(QuestionFlowLead)
            .filter(QuestionFlowLead.email == email)
            .filter(QuestionFlowLead.flow_key == flow_key)
            .first()
        )
        if lead is None:
            raise
        lead.version = version
        lead.answers = payload
        db.commit()
        db.refresh(lead)
        return lead

    except Exception:
        db.rollback()
        raise


def resume_lead(db: Session, flow_key: str, token: str) -> dict:
    """Resolves a resume token back to its (masked) address, version and answers.

    Every failure — bad signature, unknown id, purged row, wrong flow — is the
    same 404, so the endpoint can't be used to test which addresses exist. (A
    CONVERTED lead is simply a purged row here: conversion deletes it.)
    """
    _require_enabled(flow_key)

    lead_id = _verify_resume_token(token)
    if lead_id is None:
        raise InvalidResumeTokenException()

    lead = (
        db.query(QuestionFlowLead)
        .filter(QuestionFlowLead.id == lead_id)
        .filter(QuestionFlowLead.flow_key == flow_key)
        .first()
    )
    if lead is None:
        raise InvalidResumeTokenException()

    return {
        "email_masked": masked_email(lead.email),
        "version": lead.version,
        "answers": lead.answers or {},
    }


# -- Lifecycle (sweep + conversion) --------------------------------------------

def purge_expired_leads(db: Session, *, now: Optional[datetime] = None) -> int:
    """Deletes unconverted leads older than LEAD_RETENTION_DAYS. Returns the count.

    Runs UNCONDITIONALLY, including when no flow currently opts into lead
    capture: the retention promise is about rows that exist, not about the
    switch's current position, and a flow toggled off must not strand the rows
    it captured while it was on. On a dormant instance the table is empty, so
    this is one cheap indexed DELETE per tick.
    """
    cutoff = (now or datetime.now(UTC)) - timedelta(days=LEAD_RETENTION_DAYS)
    try:
        deleted = (
            db.query(QuestionFlowLead)
            .filter(QuestionFlowLead.converted_user_id.is_(None))
            .filter(QuestionFlowLead.created_at < cutoff)
            .delete(synchronize_session=False)
        )
        db.commit()
    except Exception:
        db.rollback()
        raise

    if deleted:
        logger.info("Purged %d expired question flow lead(s).", deleted)
    return deleted


def delete_leads_for_email(db: Session, email: str) -> int:
    """Removes every lead row for an address. Returns the count.

    Called at account conversion. Deletes across ALL flow keys — and regardless
    of whether any flow currently opts into capture — because the point is that
    this address is now an account: keeping a lead row for a flow whose capture
    was later switched off would leave orphaned personal data with no purge path.
    """
    email = _normalize(email)
    if not email:
        return 0

    try:
        deleted = (
            db.query(QuestionFlowLead)
            .filter(QuestionFlowLead.email == email)
            .delete(synchronize_session=False)
        )
        db.commit()
    except Exception:
        db.rollback()
        raise

    return deleted


def on_account_created(db: Session, user) -> None:
    """Conversion hook: the address now has an account, so its leads are deleted.

    FAIL-OPEN, and that is the whole design constraint. This runs inside the
    signup path after the account is committed; a failure here means some stale
    pre-account rows survive until the retention purge collects them, which is a
    housekeeping miss. Letting it raise would mean a user who typed a valid email
    and password cannot create an account because of housekeeping — never an
    acceptable trade, so every failure is swallowed at WARNING.
    """
    try:
        deleted = delete_leads_for_email(db, getattr(user, "email", ""))
        if deleted:
            logger.info("Converted %d question flow lead(s) for user %s.", deleted, user.id)
    except Exception:
        # Never blocking signup; the row is picked up by the retention purge:
        logger.warning("Question flow lead conversion failed for user %s.",
                       getattr(user, "id", None), exc_info=True)


def flow_title(flow_key: str) -> str:
    """Convenience for email copy; empty when the flow is no longer registered."""
    flow = get_flow(flow_key)
    return flow.title if flow is not None else ""
