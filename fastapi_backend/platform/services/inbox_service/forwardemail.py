"""Forward Email implementation of the MailboxProvider port.

Talks to https://api.forwardemail.net/v1/messages|folders with the mailbox's
own credentials (INBOX_IMAP_EMAIL / INBOX_IMAP_PASSWORD) over Basic auth. The
API returns fully parsed messages (nodemailer shape), so this adapter is pure
translation - no MIME parsing happens on our side.

Every payload shape below was verified against a live mailbox (2026-07-11);
fixtures in infrastructure/tests/fixtures/ pin them.
"""
import base64
import logging
import re
from typing import Any, Optional

import httpx

from infrastructure.settings import settings as s
from platform.exceptions import (
    InboxAttachmentNotFoundException,
    InboxMessageNotFoundException,
    InboxSendException,
    InboxUnavailableException,
)
from platform.services.inbox_service.ports import (
    AttachmentContent,
    AttachmentMeta,
    MailAddress,
    MessageDetail,
    MessagePage,
    MessageSummary,
    OutgoingMessage,
)


logger = logging.getLogger(__name__)


class OutboundNotApprovedError(Exception):
    """Forward Email has not yet approved this domain for outbound SMTP.

    A signal (not an HTTP error) so a composite sender can bridge via a
    fallback provider until approval lands, then upgrade automatically.
    """

_BASE_URL = "https://api.forwardemail.net"
_TIMEOUT_SECONDS = 15.0
_SNIPPET_LENGTH = 140

# IMAP flag Forward Email maps onto is_unread:
_SEEN_FLAG = "\\Seen"
_FLAGGED_FLAG = "\\Flagged"

_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")


def _snippet_from(text: Optional[str], html: Optional[str]) -> str:
    """Derives a short plain-text preview from whichever body the list payload has"""
    source = text if isinstance(text, str) and text.strip() else None
    if source is None and isinstance(html, str):
        source = _TAG_RE.sub(" ", html)
    if not source:
        return ""
    return _WHITESPACE_RE.sub(" ", source).strip()[:_SNIPPET_LENGTH]


def _addresses_from(node: Any) -> list[MailAddress]:
    """Maps a nodemailer address object ({value: [{address, name}]}) to the port type"""
    if not isinstance(node, dict):
        return []
    return [
        MailAddress(email=entry.get("address") or "", name=entry.get("name") or "")
        for entry in (node.get("value") or [])
        if isinstance(entry, dict) and entry.get("address")
    ]


def _first_address(node: Any) -> MailAddress:
    addresses = _addresses_from(node)
    return addresses[0] if addresses else MailAddress(email="", name="")


def _references_from(headers: Any) -> list[str]:
    """Splits the References header into individual message ids"""
    raw = (headers or {}).get("references") if isinstance(headers, dict) else None
    if isinstance(raw, list):
        raw = " ".join(str(item) for item in raw)
    if not isinstance(raw, str):
        return []
    return [token for token in raw.split() if token]


def _attachment_bytes(content: Any) -> bytes:
    """Decodes the two content encodings the API emits (Buffer dict / base64 str)"""
    if isinstance(content, dict) and content.get("type") == "Buffer":
        return bytes(content.get("data") or [])
    if isinstance(content, str):
        try:
            return base64.b64decode(content)
        except Exception:
            return b""
    return b""


class ForwardEmailMailbox:
    """MailboxProvider backed by Forward Email's messages API"""

    def is_configured(self) -> bool:
        return bool(s.INBOX_IMAP_EMAIL and s.INBOX_IMAP_PASSWORD)

    # ---- HTTP plumbing -----------------------------------------------------

    def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        try:
            response = httpx.request(
                method,
                f"{_BASE_URL}{path}",
                auth=(s.INBOX_IMAP_EMAIL, s.INBOX_IMAP_PASSWORD),
                timeout=_TIMEOUT_SECONDS,
                **kwargs,
            )
        except httpx.HTTPError:
            # Never logging params or bodies - they can carry message content:
            logger.exception("Forward Email mailbox API unreachable (%s %s).", method, path)
            raise InboxUnavailableException

        if response.status_code == 404:
            raise InboxMessageNotFoundException
        if response.status_code in (401, 403):
            # A rejected credential is an incident, not noise: the mailbox is
            # down for everyone until it is rotated back into sync. ERROR so
            # it alerts (Sentry watches ERROR-level logs):
            logger.error(
                "Forward Email rejected the mailbox credential (%s %s -> %s) - "
                "the inbox credential is invalid or was rotated out from under us.",
                method, path, response.status_code,
            )
            raise InboxUnavailableException
        if response.status_code == 429:
            logger.warning(
                "Forward Email rate-limited the mailbox API (%s %s).", method, path,
            )
            raise InboxUnavailableException
        if response.status_code >= 400:
            logger.warning(
                "Forward Email mailbox API error (%s %s -> %s).",
                method, path, response.status_code,
            )
            raise InboxUnavailableException
        return response

    # ---- MailboxProvider ---------------------------------------------------

    def list_folders(self) -> list[str]:
        payload = self._request("GET", "/v1/folders").json()
        return [f.get("path") for f in payload if isinstance(f, dict) and f.get("path")]

    def list_messages(self, folder: str, page: int, search: Optional[str]) -> MessagePage:
        params: dict[str, Any] = {"folder": folder, "page": page}
        if search:
            params["search"] = search
        response = self._request("GET", "/v1/messages", params=params)

        messages = []
        for item in response.json():
            if not isinstance(item, dict):
                continue
            nodemailer = item.get("nodemailer") or {}
            html = nodemailer.get("html")
            message_id = nodemailer.get("messageId")
            messages.append(MessageSummary(
                id=item.get("id") or "",
                folder=item.get("folder_path") or folder,
                subject=item.get("subject") or "",
                sender=_first_address(nodemailer.get("from")),
                date=item.get("internal_date"),
                is_unread=bool(item.get("is_unread")),
                is_flagged=bool(item.get("is_flagged")),
                has_attachments=bool(item.get("has_attachment"))
                or bool(nodemailer.get("attachments")),
                snippet=_snippet_from(nodemailer.get("text"), html if isinstance(html, str) else None),
                message_id_header=message_id if isinstance(message_id, str) else None,
                references=_references_from(nodemailer.get("headers")),
            ))

        return MessagePage(
            messages=messages,
            page=int(response.headers.get("X-Page-Current", page) or page),
            page_count=int(response.headers.get("X-Page-Count", 1) or 1),
        )

    def get_message(self, message_id: str) -> MessageDetail:
        item = self._request("GET", f"/v1/messages/{message_id}").json()
        nodemailer = item.get("nodemailer") or {}
        headers = nodemailer.get("headers") or {}

        attachments = []
        for index, att in enumerate(nodemailer.get("attachments") or []):
            if not isinstance(att, dict):
                continue
            attachments.append(AttachmentMeta(
                index=index,
                filename=att.get("filename") or f"attachment-{index}",
                content_type=att.get("contentType") or "application/octet-stream",
                size=int(att.get("size") or 0),
                cid=att.get("cid"),
                is_inline=(att.get("contentDisposition") == "inline") or bool(att.get("cid")),
            ))

        html = nodemailer.get("html")
        text = nodemailer.get("text")
        return MessageDetail(
            id=item.get("id") or message_id,
            folder=item.get("folder_path") or "",
            subject=item.get("subject") or "",
            sender=_first_address(nodemailer.get("from")),
            to=_addresses_from(nodemailer.get("to")),
            cc=_addresses_from(nodemailer.get("cc")),
            reply_to=_addresses_from(nodemailer.get("replyTo")),
            date=item.get("internal_date"),
            is_unread=bool(item.get("is_unread")),
            is_flagged=bool(item.get("is_flagged")),
            html=html if isinstance(html, str) else None,
            text=text if isinstance(text, str) else None,
            attachments=attachments,
            message_id_header=nodemailer.get("messageId"),
            references=_references_from(headers),
        )

    def get_attachment(self, message_id: str, index: int) -> AttachmentContent:
        item = self._request("GET", f"/v1/messages/{message_id}").json()
        attachments = (item.get("nodemailer") or {}).get("attachments") or []
        if index < 0 or index >= len(attachments):
            raise InboxAttachmentNotFoundException
        att = attachments[index] or {}
        data = _attachment_bytes(att.get("content"))
        if not data:
            raise InboxAttachmentNotFoundException
        return AttachmentContent(
            filename=att.get("filename") or f"attachment-{index}",
            content_type=att.get("contentType") or "application/octet-stream",
            data=data,
        )

    def _update_flags(self, message_id: str, add: set[str], remove: set[str]) -> None:
        """Read-modify-write of the IMAP flag set (PUT replaces it wholesale,
        so a blind write would clobber sibling flags like \Flagged)."""
        item = self._request("GET", f"/v1/messages/{message_id}").json()
        current = set(item.get("flags") or [])
        desired = sorted((current | add) - remove)
        self._request("PUT", f"/v1/messages/{message_id}", json={"flags": desired})

    def set_read(self, message_id: str, read: bool) -> None:
        # Forward Email derives is_unread from the IMAP \Seen flag; the
        # is_unread field itself is not writable (verified live):
        if read:
            self._update_flags(message_id, add={_SEEN_FLAG}, remove=set())
        else:
            self._update_flags(message_id, add=set(), remove={_SEEN_FLAG})

    def set_flagged(self, message_id: str, flagged: bool) -> None:
        if flagged:
            self._update_flags(message_id, add={_FLAGGED_FLAG}, remove=set())
        else:
            self._update_flags(message_id, add=set(), remove={_FLAGGED_FLAG})

    def move(self, message_id: str, folder: str) -> None:
        self._request("PUT", f"/v1/messages/{message_id}", json={"folder": folder})

    def delete(self, message_id: str) -> None:
        self._request("DELETE", f"/v1/messages/{message_id}")

    def append(self, folder: str, message: OutgoingMessage, sender: MailAddress) -> str:
        """Files a message into a folder (Sent copies, drafts) and returns its id"""
        body: dict[str, Any] = {
            "folder": folder,
            "from": f"{sender.name} <{sender.email}>" if sender.name else sender.email,
            "to": [addr.email for addr in message.to],
            "subject": message.subject,
            "html": message.html,
            "text": message.text,
        }
        if message.cc:
            body["cc"] = [addr.email for addr in message.cc]
        if message.bcc:
            body["bcc"] = [addr.email for addr in message.bcc]
        if message.attachments:
            body["attachments"] = _outgoing_attachments(message)
        created = self._request("POST", "/v1/messages", json=body).json()
        return created.get("id") or ""


def _outgoing_attachments(message: OutgoingMessage) -> list[dict[str, Any]]:
    """Maps port attachments into Forward Email / nodemailer attachment objects"""
    return [
        {
            "filename": att.filename,
            "content": att.content_b64,
            "encoding": "base64",
            "contentType": att.content_type,
            **({"cid": att.cid} if att.cid else {}),
        }
        for att in message.attachments
    ]


class ForwardEmailSender:
    """MailSender backed by Forward Email's outbound API (POST /v1/emails).

    Uses the mailbox's own credentials, so send needs no config beyond the
    INBOX_IMAP_* pair the mailbox already requires - the whole inbox runs on
    one provider and one credential. Delivery verified live (2026-07-11):
    DKIM aligns with the mailbox domain, threading headers and attachments/
    inline images pass through. A domain awaiting Forward Email's outbound
    approval raises OutboundNotApprovedError so a composite can bridge.
    """

    _NOT_APPROVED = "not approved for outbound"

    def is_configured(self) -> bool:
        return bool(s.INBOX_IMAP_EMAIL and s.INBOX_IMAP_PASSWORD)

    def send(self, message: OutgoingMessage, sender: MailAddress) -> None:
        payload: dict[str, Any] = {
            "from": f"{sender.name} <{sender.email}>" if sender.name else sender.email,
            "to": [addr.email for addr in message.to],
            "subject": message.subject,
            "html": message.html,
            "text": message.text,
        }
        if message.cc:
            payload["cc"] = [addr.email for addr in message.cc]
        if message.bcc:
            payload["bcc"] = [addr.email for addr in message.bcc]
        if message.in_reply_to:
            payload["inReplyTo"] = message.in_reply_to
        if message.references:
            payload["references"] = " ".join(message.references)
        if message.attachments:
            payload["attachments"] = _outgoing_attachments(message)

        try:
            response = httpx.post(
                f"{_BASE_URL}/v1/emails",
                auth=(s.INBOX_IMAP_EMAIL, s.INBOX_IMAP_PASSWORD),
                json=payload,
                timeout=_TIMEOUT_SECONDS,
            )
        except httpx.HTTPError:
            logger.exception("Forward Email outbound request failed to reach the API.")
            raise InboxSendException

        if response.status_code < 300:
            return

        # Awaiting Forward Email's per-domain outbound approval is a distinct,
        # recoverable state (a fallback provider can bridge), not a send error:
        if response.status_code == 403 and self._NOT_APPROVED in (response.text or "").lower():
            raise OutboundNotApprovedError
        logger.error("Forward Email rejected an inbox send (status %s).", response.status_code)
        raise InboxSendException
