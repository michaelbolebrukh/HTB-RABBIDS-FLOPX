"""Admin inbox platform capability.

Architecture: two provider-neutral ports (ports.py). Forward Email implements
both the mailbox and the sender, so the whole inbox runs on one provider and
one credential (INBOX_IMAP_*); a Brevo sender bridges outbound only while a new
instance's domain awaits Forward Email's outbound approval (bridging_sender.py).
Composed in service.py; routers import from this package surface only.
"""
from platform.services.inbox_service.ports import (
    AttachmentContent,
    AttachmentMeta,
    MailAddress,
    MailboxProvider,
    MailSender,
    MessageDetail,
    MessagePage,
    MessageSummary,
    OutgoingAttachment,
    OutgoingMessage,
)
from platform.services.inbox_service.service import (
    BATCH_WRITE_ACTIONS,
    INBOX_READ,
    INBOX_WRITE,
    apply_batch,
    delete_message,
    get_attachment,
    get_mailbox,
    get_message,
    get_sender,
    inbox_can_send,
    inbox_is_configured,
    list_folders,
    list_messages,
    move_message,
    save_draft,
    send_forward,
    send_message,
    send_reply,
    set_flagged,
    set_read,
    validate_inbox_config,
)
