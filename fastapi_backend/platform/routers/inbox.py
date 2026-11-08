from fastapi import APIRouter, Query, status as st
from starlette.requests import Request
from starlette.responses import Response

from infrastructure.rate_limiter import limiter
from infrastructure.dependencies import db_dependency, require_capability, verify_capability
from platform.capabilities import INBOX_CREDENTIALS, INBOX_READ, INBOX_WRITE
from platform.schemas import (
    InboxBatchRequest,
    InboxBatchResult,
    InboxConfigResponse,
    InboxConnectRequest,
    InboxConnectionGroup,
    InboxConnectionSettings,
    InboxDraftRequest,
    InboxDraftResponse,
    InboxForwardRequest,
    InboxMessageDetail,
    InboxMessagePage,
    InboxMoveRequest,
    InboxReplyRequest,
    InboxScheduledItem,
    InboxSendRequest,
    InboxUpdateMessageRequest,
)
from platform.services import inbox_service
from platform.utils.http_headers import content_disposition
from platform.services.inbox_service import MailAddress, OutgoingAttachment
from platform.services.inbox_service import connections as inbox_connections
from platform.services.inbox_service import scheduled as inbox_scheduled
from infrastructure.settings import settings as inbox_settings


router = APIRouter(prefix="/inbox", tags=["Inbox"])

# Mailbox content is PII-sensitive beyond "operational": reading needs the
# inbox:read capability, anything that sends or mutates needs inbox:write, and
# anything that exposes the mailbox credential needs inbox:credentials — granted
# to nobody, so effectively admin-only (admins hold all capabilities implicitly):
inbox_reader_dependency = require_capability(INBOX_READ)
inbox_writer_dependency = require_capability(INBOX_WRITE)
inbox_credentials_dependency = require_capability(INBOX_CREDENTIALS)


def _addresses(payload_addresses) -> list[MailAddress]:
    return [MailAddress(email=a.email, name=a.name) for a in payload_addresses]


def _attachments(payload_attachments) -> list[OutgoingAttachment]:
    return [
        OutgoingAttachment(
            filename=a.filename,
            content_b64=a.content_base64,
            content_type=a.content_type,
            cid=a.cid,
        )
        for a in payload_attachments
    ]


@router.get("/config", response_model=InboxConfigResponse, status_code=st.HTTP_200_OK)
def get_inbox_config(user: inbox_reader_dependency):
    """Whether the inbox is wired for this instance (visible to any inbox reader)"""
    return {
        "configured": inbox_service.inbox_is_configured(),
        "can_send": inbox_service.inbox_can_send(),
        "mailbox_email": inbox_settings.INBOX_IMAP_EMAIL or "",
    }


@router.get("/folders", response_model=list[str], status_code=st.HTTP_200_OK)
def list_folders(user: inbox_reader_dependency):
    return inbox_service.list_folders()


@router.get("/messages", response_model=InboxMessagePage, status_code=st.HTTP_200_OK)
@limiter.limit("120/minute")
def list_messages(
    user: inbox_reader_dependency,
    request: Request,
    response: Response,
    folder: str = Query(default="INBOX", max_length=255),
    page: int = Query(default=1, ge=1),
    search: str | None = Query(default=None, max_length=255),
):
    result = inbox_service.list_messages(folder=folder, page=page, search=search)
    return {
        "messages": [vars(m) | {"sender": vars(m.sender)} for m in result.messages],
        "page": result.page,
        "page_count": result.page_count,
    }


@router.get("/messages/{message_id}", response_model=InboxMessageDetail, status_code=st.HTTP_200_OK)
@limiter.limit("120/minute")
def get_message(user: inbox_reader_dependency, request: Request, response: Response, message_id: str):
    detail, safe_html, suggested_reply = inbox_service.get_message(message_id)
    return {
        "id": detail.id,
        "folder": detail.folder,
        "subject": detail.subject,
        "sender": vars(detail.sender),
        "to": [vars(a) for a in detail.to],
        "cc": [vars(a) for a in detail.cc],
        "date": detail.date,
        "is_unread": detail.is_unread,
        "is_flagged": detail.is_flagged,
        "html": safe_html,
        "attachments": [
            {
                "index": a.index,
                "filename": a.filename,
                "content_type": a.content_type,
                "size": a.size,
                "is_inline": a.is_inline,
            }
            for a in detail.attachments
        ],
        "suggested_reply": {
            "to": [vars(a) for a in suggested_reply["to"]],
            "cc": [vars(a) for a in suggested_reply["cc"]],
            "subject": suggested_reply["subject"],
        },
    }


@router.get("/messages/{message_id}/attachments/{index}", status_code=st.HTTP_200_OK)
@limiter.limit("60/minute")
def download_attachment(
    user: inbox_reader_dependency, request: Request, response: Response, message_id: str, index: int
):
    content = inbox_service.get_attachment(message_id, index)
    return Response(
        content=content.data,
        media_type=content.content_type,
        headers={"Content-Disposition": content_disposition("attachment", content.filename)},
    )


@router.patch("/messages/{message_id}", status_code=st.HTTP_204_NO_CONTENT)
@limiter.limit("120/minute")
def update_message(
    user: inbox_reader_dependency,
    request: Request,
    response: Response,
    message_id: str,
    payload: InboxUpdateMessageRequest,
):
    """Read-state and starring are shared triage state, part of reading, so
    they sit under inbox:read; moves and deletes are write-gated below."""
    if payload.is_unread is not None:
        inbox_service.set_read(message_id, read=not payload.is_unread)
    if payload.is_flagged is not None:
        inbox_service.set_flagged(message_id, payload.is_flagged)


@router.post("/messages/batch", response_model=list[InboxBatchResult], status_code=st.HTTP_200_OK)
@limiter.limit("30/minute")
def batch_messages(
    user: inbox_reader_dependency,
    request: Request,
    response: Response,
    payload: InboxBatchRequest,
):
    """One action across many messages; per-id results surface partial failures.

    Triage actions (read/unread/star) match the single-message read gate;
    destructive actions additionally require the write capability."""
    if payload.action in inbox_service.BATCH_WRITE_ACTIONS:
        verify_capability(user, INBOX_WRITE)
    return inbox_service.apply_batch(payload.ids, payload.action)


@router.post("/messages/{message_id}/move", status_code=st.HTTP_204_NO_CONTENT)
@limiter.limit("60/minute")
def move_message(
    user: inbox_writer_dependency,
    request: Request,
    response: Response,
    message_id: str,
    payload: InboxMoveRequest,
):
    inbox_service.move_message(message_id, payload.folder)


@router.delete("/messages/{message_id}", status_code=st.HTTP_204_NO_CONTENT)
@limiter.limit("60/minute")
def delete_message(user: inbox_writer_dependency, request: Request, response: Response, message_id: str):
    inbox_service.delete_message(message_id)


@router.post("/messages", status_code=st.HTTP_201_CREATED)
@limiter.limit("30/minute")
def send_message(
    user: inbox_writer_dependency, db: db_dependency,
    request: Request, response: Response, payload: InboxSendRequest,
):
    if payload.send_at is not None:
        inbox_scheduled.create_scheduled(db, user.id, _scheduled_payload("new", None, payload), payload.send_at)
        return {"scheduled": True}
    undo = _undoable_send(db, user.id, "new", None, payload)
    if undo is not None:
        return undo
    inbox_service.send_message(
        to=_addresses(payload.to),
        cc=_addresses(payload.cc),
        bcc=_addresses(payload.bcc),
        subject=payload.subject,
        html=payload.html,
        attachments=_attachments(payload.attachments),
    )
    return {"scheduled": False}


@router.post("/messages/{message_id}/reply", status_code=st.HTTP_201_CREATED)
@limiter.limit("30/minute")
def reply_to_message(
    user: inbox_writer_dependency,
    request: Request,
    response: Response,
    message_id: str,
    payload: InboxReplyRequest,
    db: db_dependency,
):
    if payload.send_at is not None:
        inbox_scheduled.create_scheduled(db, user.id, _scheduled_payload("reply", message_id, payload), payload.send_at)
        return {"scheduled": True}
    undo = _undoable_send(db, user.id, "reply", message_id, payload)
    if undo is not None:
        return undo
    inbox_service.send_reply(
        original_id=message_id,
        to=_addresses(payload.to),
        cc=_addresses(payload.cc),
        bcc=_addresses(payload.bcc),
        subject=payload.subject,
        html=payload.html,
        attachments=_attachments(payload.attachments),
    )
    return {"scheduled": False}


@router.post("/messages/{message_id}/forward", status_code=st.HTTP_201_CREATED)
@limiter.limit("30/minute")
def forward_message(
    user: inbox_writer_dependency,
    request: Request,
    response: Response,
    message_id: str,
    payload: InboxForwardRequest,
    db: db_dependency,
):
    if payload.send_at is not None:
        inbox_scheduled.create_scheduled(db, user.id, _scheduled_payload("forward", message_id, payload), payload.send_at)
        return {"scheduled": True}
    undo = _undoable_send(db, user.id, "forward", message_id, payload)
    if undo is not None:
        return undo
    inbox_service.send_forward(
        original_id=message_id,
        to=_addresses(payload.to),
        cc=_addresses(payload.cc),
        bcc=_addresses(payload.bcc),
        html=payload.html,
        attachments=_attachments(payload.attachments),
    )
    return {"scheduled": False}


def _undoable_send(db, user_id: int, kind: str, original_id, payload):
    """Files an immediate send into the outbox with a short undo window.

    Returns the response dict when undo is active, or None to send
    synchronously (window disabled). Cancelling = DELETE /inbox/scheduled/{id}
    before the dispatcher claims the row."""
    from datetime import datetime, timedelta, timezone

    undo_seconds = inbox_settings.INBOX_UNDO_SEND_SECONDS
    if undo_seconds <= 0:
        return None
    row = inbox_scheduled.create_scheduled(
        db, user_id, _scheduled_payload(kind, original_id, payload),
        datetime.now(timezone.utc) + timedelta(seconds=undo_seconds),
    )
    return {"scheduled": False, "undo_id": row.id, "undo_seconds": undo_seconds}


def _scheduled_payload(kind: str, original_id, payload) -> dict:
    """Freezes a composer payload as provider-neutral JSON for the outbox"""
    return {
        "kind": kind,
        "original_id": original_id,
        "to": [a.model_dump() for a in payload.to],
        "cc": [a.model_dump() for a in payload.cc],
        "bcc": [a.model_dump() for a in payload.bcc],
        "subject": getattr(payload, "subject", "") or "",
        "html": payload.html,
        "attachments": [a.model_dump() for a in payload.attachments],
    }


# ---- Drafts ------------------------------------------------------------------

@router.post("/drafts", response_model=InboxDraftResponse, status_code=st.HTTP_201_CREATED)
@limiter.limit("60/minute")
def save_draft(user: inbox_writer_dependency, request: Request, response: Response, payload: InboxDraftRequest):
    draft_id = inbox_service.save_draft(
        to=_addresses(payload.to),
        cc=_addresses(payload.cc),
        bcc=_addresses(payload.bcc),
        subject=payload.subject,
        html=payload.html,
        attachments=_attachments(payload.attachments),
    )
    return {"id": draft_id}


@router.put("/drafts/{draft_id}", response_model=InboxDraftResponse, status_code=st.HTTP_200_OK)
@limiter.limit("60/minute")
def update_draft(
    user: inbox_writer_dependency, request: Request, response: Response,
    draft_id: str, payload: InboxDraftRequest,
):
    new_id = inbox_service.save_draft(
        to=_addresses(payload.to),
        cc=_addresses(payload.cc),
        bcc=_addresses(payload.bcc),
        subject=payload.subject,
        html=payload.html,
        attachments=_attachments(payload.attachments),
        replaces_id=draft_id,
    )
    return {"id": new_id}


# ---- Scheduled sends -----------------------------------------------------------

@router.get("/scheduled", response_model=list[InboxScheduledItem], status_code=st.HTTP_200_OK)
def list_scheduled(user: inbox_writer_dependency, db: db_dependency):
    # Expose the whole frozen composer payload so the client can re-open a
    # pending row in the composer to edit it (cancel + recompose):
    return [
        {
            "id": row.id,
            "subject": (row.payload or {}).get("subject", ""),
            "to": (row.payload or {}).get("to", []),
            "send_at": row.send_at,
            "status": row.status,
            "kind": (row.payload or {}).get("kind", "new"),
            "original_id": (row.payload or {}).get("original_id"),
            "cc": (row.payload or {}).get("cc", []),
            "bcc": (row.payload or {}).get("bcc", []),
            "html": (row.payload or {}).get("html", ""),
            "attachments": (row.payload or {}).get("attachments", []),
        }
        for row in inbox_scheduled.list_scheduled(db)
    ]


@router.delete("/scheduled/{scheduled_id}", status_code=st.HTTP_204_NO_CONTENT)
def cancel_scheduled(user: inbox_writer_dependency, db: db_dependency, scheduled_id: int):
    inbox_scheduled.cancel_scheduled(db, scheduled_id)


# ---- Mail-client connectors (audited credential grants) ---------------------
# Anything that EXPOSES the mailbox password (reveal, Apple profile) is gated
# inbox:credentials — granted to nobody, so effectively admin-only — so an
# account with inbox:write can operate the mailbox in-app but cannot extract
# its password to set up an external client. The audit list (no secret) stays
# inbox:write. Every grant is still recorded.

@router.get("/connections", response_model=list[InboxConnectionGroup], status_code=st.HTTP_200_OK)
def list_connections(user: inbox_writer_dependency, db: db_dependency):
    """One aggregated row per admin (SQL group-by), latest activity first"""
    return inbox_connections.list_connection_groups(db)


@router.post("/connections/reveal", response_model=InboxConnectionSettings, status_code=st.HTTP_200_OK)
@limiter.limit("10/minute")
def reveal_connection_settings(
    user: inbox_credentials_dependency,
    db: db_dependency,
    request: Request,
    response: Response,
    payload: InboxConnectRequest,
):
    """Reveals the mailbox connection material in place (inbox:credentials, audited)"""
    return inbox_connections.reveal_settings(db, user, payload.connector)


@router.post("/connections/apple-profile", status_code=st.HTTP_200_OK)
@limiter.limit("10/minute")
def download_apple_profile(
    user: inbox_credentials_dependency,
    db: db_dependency,
    request: Request,
    response: Response,
):
    """One-tap Apple configuration profile with the mailbox account embedded"""
    profile = inbox_connections.build_apple_profile(db, user)
    return Response(
        content=profile,
        media_type="application/x-apple-aspen-config",
        headers={"Content-Disposition": 'attachment; filename="mailbox.mobileconfig"'},
    )
