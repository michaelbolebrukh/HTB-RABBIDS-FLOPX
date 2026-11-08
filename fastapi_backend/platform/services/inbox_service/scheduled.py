"""Schedule-send: outbox rows + the dispatcher that fires them on time.

A scheduled message is the composer payload frozen as provider-neutral JSON
plus a send_at instant. The dispatcher (daemon thread started with the inbox
monitors) claims due rows optimistically, replays them through the normal
send paths (so threading, sanitization, Sent filing, and the Brevo bridge all
apply identically), and deletes the row on success. Failures stay visible as
status=failed with an ERROR log - a scheduled send can be late, never
silently lost.
"""
import logging
import time
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from platform.models import ScheduledMessage
from platform.services.inbox_service.ports import MailAddress, OutgoingAttachment


logger = logging.getLogger(__name__)

# 5s keeps the undo-send window honest: a message leaves at most one tick
# after its (send_at = now + undo window) instant, matching the 5s undo
# window so a sent message dispatches promptly once it closes. The due-row
# query is indexed and almost always empty, so the tighter loop costs nothing:
DISPATCH_INTERVAL_SECONDS = 5


def _addresses(items: list[dict]) -> list[MailAddress]:
    return [MailAddress(email=a.get("email", ""), name=a.get("name", "")) for a in items or []]


def _attachments(items: list[dict]) -> list[OutgoingAttachment]:
    return [
        OutgoingAttachment(
            filename=a.get("filename", "attachment"),
            content_b64=a.get("content_base64", ""),
            content_type=a.get("content_type", "application/octet-stream"),
            cid=a.get("cid"),
        )
        for a in items or []
    ]


def create_scheduled(db: Session, user_id: int, payload: dict, send_at: datetime) -> ScheduledMessage:
    row = ScheduledMessage(user_id=user_id, payload=payload, send_at=send_at)
    db.add(row)
    try:
        db.commit()
    except Exception:
        db.rollback()
        raise
    db.refresh(row)
    return row


def list_scheduled(db: Session) -> list[ScheduledMessage]:
    return (
        db.query(ScheduledMessage)
        .filter(ScheduledMessage.status.in_(["pending", "failed"]))
        .order_by(ScheduledMessage.send_at.asc())
        .limit(200)
        .all()
    )


def cancel_scheduled(db: Session, scheduled_id: int) -> bool:
    # Only a not-yet-claimed row can be cancelled; a row mid-send stays:
    deleted = (
        db.query(ScheduledMessage)
        .filter(ScheduledMessage.id == scheduled_id, ScheduledMessage.status == "pending")
        .delete()
    )
    try:
        db.commit()
    except Exception:
        db.rollback()
        raise
    return bool(deleted)


def _dispatch_one(row: ScheduledMessage) -> None:
    """Replays one payload through the normal send paths"""
    from platform.services.inbox_service import service as svc

    payload = row.payload or {}
    kind = payload.get("kind", "new")
    common = dict(
        to=_addresses(payload.get("to")),
        cc=_addresses(payload.get("cc")),
        bcc=_addresses(payload.get("bcc")),
        html=payload.get("html", ""),
        attachments=_attachments(payload.get("attachments")),
    )
    if kind == "reply":
        svc.send_reply(original_id=payload.get("original_id", ""),
                       subject=payload.get("subject", ""), **common)
    elif kind == "forward":
        svc.send_forward(original_id=payload.get("original_id", ""), **common)
    else:
        svc.send_message(subject=payload.get("subject", "(no subject)"), **common)


def dispatch_due(session_factory) -> int:
    """Sends every due pending row; returns how many were dispatched"""
    dispatched = 0
    db: Session = session_factory()
    try:
        now = datetime.now(timezone.utc)
        due_ids = [
            row_id for (row_id,) in db.query(ScheduledMessage.id)
            .filter(ScheduledMessage.status == "pending", ScheduledMessage.send_at <= now)
            .order_by(ScheduledMessage.send_at.asc())
            .limit(20)
            .all()
        ]
        for row_id in due_ids:
            # Optimistic claim: exactly one worker flips pending -> sending:
            claimed = (
                db.query(ScheduledMessage)
                .filter(ScheduledMessage.id == row_id, ScheduledMessage.status == "pending")
                .update({"status": "sending"})
            )
            db.commit()
            if not claimed:
                continue
            row = db.query(ScheduledMessage).get(row_id)
            try:
                _dispatch_one(row)
                db.delete(row)
                db.commit()
                dispatched += 1
                logger.info("Scheduled message %s dispatched.", row_id)
            except Exception:
                row.status = "failed"
                db.commit()
                logger.exception("Scheduled message %s FAILED to send.", row_id)
                _notify_failure(db, row)
    finally:
        db.close()
    return dispatched


def _notify_failure(db: Session, row: ScheduledMessage) -> None:
    """Pushes a failure alert so a broken scheduled send is never silent"""
    from platform.services.notification_service import push

    try:
        subject = (row.payload or {}).get("subject") or "(no subject)"
        push.notify_inbox_readers(db, "Scheduled send failed", subject, url="/inbox")
    except Exception:
        logger.warning("Could not push the scheduled-send failure alert.", exc_info=True)


def run_dispatcher_loop(session_factory) -> None:
    """Daemon loop: fire due scheduled messages once a minute"""
    while True:
        time.sleep(DISPATCH_INTERVAL_SECONDS)
        try:
            dispatch_due(session_factory)
        except Exception:
            logger.exception("Scheduled-send dispatcher iteration crashed.")
