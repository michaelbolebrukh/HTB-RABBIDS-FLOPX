"""Admin inbox orchestration over the two mail ports.

Composition happens here and only here: today every instance pairs the
Forward Email mailbox with the Brevo sender, so the wiring is code, not
config (config-minimalism: a provider env var would be a liability with a
single real pairing). Swapping a provider = swapping one constructor below.
"""
import logging
import re

from infrastructure.settings import settings as s
# Re-exporting the inbox capability strings from the platform registry so the
# package surface keeps serving them (see platform/capabilities.py):
from platform.capabilities import INBOX_READ, INBOX_WRITE  # noqa: F401
from platform.exceptions import (
    InboxNotConfiguredException,
    InboxSendNotConfiguredException,
)
from platform.services.inbox_service.bridging_sender import BridgingSender
from platform.services.inbox_service.brevo_sender import BrevoInboxSender
from platform.services.inbox_service.forwardemail import ForwardEmailMailbox, ForwardEmailSender
from platform.services.inbox_service.ports import (
    AttachmentContent,
    MailAddress,
    MailboxProvider,
    MailSender,
    MessageDetail,
    MessagePage,
    OutgoingAttachment,
    OutgoingMessage,
)
from platform.services.inbox_service.recipients import (
    forward_subject,
    reply_recipients,
    reply_subject,
    reply_threading,
)
from platform.services.inbox_service.sanitize import (
    resolve_cid_images,
    prepare_outbound_html,
    sanitize_email_html,
)


logger = logging.getLogger(__name__)

_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"[ \t]+")

# The composition point - the only lines that know which providers are in use.
# The whole inbox runs on Forward Email (receive + send on one credential);
# Brevo, already present platform-wide, bridges outbound only while a new
# instance's domain awaits Forward Email's outbound approval (see BridgingSender):
_mailbox: MailboxProvider = ForwardEmailMailbox()
_sender: MailSender = BridgingSender(
    primary=ForwardEmailSender(),
    fallback=BrevoInboxSender(),
)


def get_mailbox() -> MailboxProvider:
    return _mailbox


def get_sender() -> MailSender:
    return _sender


# ---- Configuration gates (fail closed, loudly) ------------------------------

def inbox_is_configured() -> bool:
    """Whether the mailbox side of the inbox is wired for this instance"""
    return get_mailbox().is_configured()


def inbox_can_send() -> bool:
    """Whether the outbound side is wired. Forward Email sends on the mailbox
    credential, so this holds whenever the inbox is configured; the composer
    reads it to decide whether to offer sending at all."""
    return inbox_is_configured() and get_sender().is_configured()


def validate_inbox_config() -> None:
    """Startup consistency check - mirrors the Stripe gate's fail-loud intent.

    With Forward Email as the full-stack provider, sending reuses the mailbox
    credential, so a configured inbox is always send-capable and there is no
    half-configured state to alarm on at boot. The one runtime bridge (a domain
    still awaiting Forward Email outbound approval) is handled and logged by the
    sender per send, not at startup. This guard stays as a cheap invariant: a
    configured inbox whose sender reports unconfigured would mean a miswired
    composition, which must page rather than lurk.
    """
    if inbox_is_configured() and not get_sender().is_configured():
        logger.error(
            "Inbox misconfiguration: the inbox is configured but no mail sender "
            "is available - replies and composition are disabled."
        )


def _require_configured() -> None:
    if not inbox_is_configured():
        raise InboxNotConfiguredException


def _mailbox_identity() -> MailAddress:
    return MailAddress(email=s.INBOX_IMAP_EMAIL or "", name=s.SENDER_NAME or "")


# ---- Read paths --------------------------------------------------------------

def list_folders() -> list[str]:
    _require_configured()
    return get_mailbox().list_folders()


def list_messages(folder: str, page: int, search: str | None) -> MessagePage:
    _require_configured()
    return get_mailbox().list_messages(folder=folder, page=page, search=search)


def get_message(message_id: str) -> tuple[MessageDetail, str, dict]:
    """Returns (detail, safe_html, suggested_reply) for the reading pane.

    safe_html is sanitized and has inline images resolved; suggested_reply
    prefills the composer with reply-all recipients, subject, and threading.
    """
    _require_configured()
    mailbox = get_mailbox()
    detail = mailbox.get_message(message_id)

    html = detail.html or ""
    if not html and detail.text:
        # Plain-text mail renders as escaped preformatted text:
        html = "<pre style=\"white-space:pre-wrap;font-family:inherit\">" + (
            detail.text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        ) + "</pre>"
    safe_html = sanitize_email_html(html)
    if any(att.cid for att in detail.attachments):
        safe_html = resolve_cid_images(
            safe_html,
            detail.attachments,
            get_bytes=lambda index: mailbox.get_attachment(detail.id, index).data,
        )

    to, cc = reply_recipients(detail, own_email=s.INBOX_IMAP_EMAIL or "")
    suggested_reply = {
        "to": to,
        "cc": cc,
        "subject": reply_subject(detail.subject),
    }
    return detail, safe_html, suggested_reply


def get_attachment(message_id: str, index: int) -> AttachmentContent:
    _require_configured()
    return get_mailbox().get_attachment(message_id, index)


# ---- Mutations ----------------------------------------------------------------

def set_read(message_id: str, read: bool) -> None:
    # Read-state is shared mailbox state and part of *reading*, so it sits
    # under inbox:read at the router (unlike folder moves and deletes):
    _require_configured()
    get_mailbox().set_read(message_id, read)


def set_flagged(message_id: str, flagged: bool) -> None:
    # Starring is shared triage state, same access class as read-state:
    _require_configured()
    get_mailbox().set_flagged(message_id, flagged)


def move_message(message_id: str, folder: str) -> None:
    _require_configured()
    get_mailbox().move(message_id, folder)


def delete_message(message_id: str) -> None:
    _require_configured()
    get_mailbox().delete(message_id)


# Batch actions that change where a message lives (write-gated at the router);
# the rest are shared triage state (read-gated), mirroring the single routes.
# "trash" moves to the Trash folder (recoverable); "delete" is the permanent
# removal used only from within Trash, matching how mail clients separate the
# two so an ordinary delete is always undoable:
BATCH_WRITE_ACTIONS = {"archive", "spam", "inbox", "trash", "delete"}

_BATCH_ACTIONS = {
    "archive": lambda mid: move_message(mid, "Archive"),
    "spam": lambda mid: move_message(mid, "Spam"),
    "inbox": lambda mid: move_message(mid, "INBOX"),
    "trash": lambda mid: move_message(mid, "Trash"),
    "delete": lambda mid: delete_message(mid),
    "read": lambda mid: set_read(mid, True),
    "unread": lambda mid: set_read(mid, False),
    "star": lambda mid: set_flagged(mid, True),
    "unstar": lambda mid: set_flagged(mid, False),
}


def apply_batch(ids: list[str], action: str) -> list[dict]:
    """Applies one action to many messages, reporting per-id success.

    Partial failure is expected (another session may have moved or deleted a
    message mid-batch), so a failed id is marked instead of aborting the rest.
    """
    _require_configured()
    apply = _BATCH_ACTIONS[action]
    results = []
    for message_id in ids:
        try:
            apply(message_id)
            results.append({"id": message_id, "ok": True})
        except Exception:
            logger.warning("Batch %s failed for one message.", action)
            results.append({"id": message_id, "ok": False})
    return results


# ---- Send paths -----------------------------------------------------------------

def _text_fallback(html: str) -> str:
    """Plain-text alternative derived from the composed HTML"""
    text = re.sub(r"(?i)</(p|div|li|h[1-6]|blockquote|tr)>", "\n", html)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = _TAG_RE.sub("", text)
    text = text.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    return "\n".join(_WHITESPACE_RE.sub(" ", line).strip() for line in text.splitlines()).strip()


def _send(message: OutgoingMessage) -> None:
    if not inbox_can_send():
        raise InboxSendNotConfiguredException
    sender_identity = _mailbox_identity()
    get_sender().send(message, sender_identity)
    # Filing the Sent copy is best effort: the mail is already on the wire,
    # so a filing hiccup must not surface as a send failure -
    try:
        get_mailbox().append("Sent Mail", message, sender_identity)
    except Exception:
        logger.exception("Sent-copy filing failed after a successful send.")


def send_message(
    to: list[MailAddress],
    cc: list[MailAddress],
    bcc: list[MailAddress],
    subject: str,
    html: str,
    attachments: list[OutgoingAttachment],
) -> None:
    """Compose-new path"""
    _require_configured()
    safe_html = prepare_outbound_html(html)
    _send(OutgoingMessage(
        to=to,
        cc=cc,
        bcc=bcc,
        subject=subject,
        html=safe_html,
        text=_text_fallback(safe_html),
        attachments=attachments,
    ))


def save_draft(
    to: list[MailAddress],
    cc: list[MailAddress],
    bcc: list[MailAddress],
    subject: str,
    html: str,
    attachments: list[OutgoingAttachment],
    replaces_id: str | None = None,
) -> str:
    """Files (or refiles) a draft into the mailbox's Drafts folder.

    Updates are delete-and-refile because the storage API appends immutable
    messages; the returned id is the draft's current identity."""
    _require_configured()
    mailbox = get_mailbox()
    safe_html = prepare_outbound_html(html)
    draft_id = mailbox.append("Drafts", OutgoingMessage(
        to=to,
        cc=cc,
        bcc=bcc,
        subject=subject,
        html=safe_html,
        text=_text_fallback(safe_html),
        attachments=attachments,
    ), _mailbox_identity())
    if replaces_id:
        try:
            mailbox.delete(replaces_id)
        except Exception:
            logger.warning("Superseded draft could not be removed (id %s).", replaces_id)
    return draft_id


def send_reply(
    original_id: str,
    to: list[MailAddress],
    cc: list[MailAddress],
    bcc: list[MailAddress],
    subject: str,
    html: str,
    attachments: list[OutgoingAttachment],
) -> None:
    """Reply path: recipients/subject arrive from the (editable) composer
    prefill; threading headers are always assembled server-side from the
    original so a client cannot break or forge the thread."""
    _require_configured()
    original = get_mailbox().get_message(original_id)
    in_reply_to, references = reply_threading(original)

    safe_html = prepare_outbound_html(html)
    _send(OutgoingMessage(
        to=to,
        cc=cc,
        bcc=bcc,
        subject=subject or reply_subject(original.subject),
        html=safe_html,
        text=_text_fallback(safe_html),
        in_reply_to=in_reply_to,
        references=references,
        attachments=attachments,
    ))


def send_forward(
    original_id: str,
    to: list[MailAddress],
    cc: list[MailAddress],
    bcc: list[MailAddress],
    html: str,
    attachments: list[OutgoingAttachment],
) -> None:
    """Forward path: the composer supplies the quoted body; the original's
    attachments are refetched and reattached server-side."""
    import base64

    _require_configured()
    mailbox = get_mailbox()
    original = mailbox.get_message(original_id)

    forwarded_attachments = list(attachments)
    for meta in original.attachments:
        content = mailbox.get_attachment(original_id, meta.index)
        forwarded_attachments.append(OutgoingAttachment(
            filename=content.filename,
            content_b64=base64.b64encode(content.data).decode(),
            content_type=content.content_type,
            cid=meta.cid,
        ))

    safe_html = prepare_outbound_html(html)
    _send(OutgoingMessage(
        to=to,
        cc=cc,
        bcc=bcc,
        subject=forward_subject(original.subject),
        html=safe_html,
        text=_text_fallback(safe_html),
        attachments=forwarded_attachments,
    ))
