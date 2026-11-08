"""Provider-neutral contracts for the admin inbox.

Every mail action the inbox performs goes through one of two narrow ports so
providers stay swappable independently:

- MailboxProvider - where messages LIVE (list/read/flag/move/delete/append).
  Current implementation: Forward Email's messages API (forwardemail.py).
- MailSender - how messages LEAVE (send with threading headers/attachments).
  Current implementation: Brevo's transactional API (brevo_sender.py); a
  Forward Email outbound implementation can replace it per instance once that
  domain's outbound access is approved, with zero changes above the port.

The dataclasses are the inbox's own vocabulary - adapters translate provider
payloads into them at the boundary, and nothing above the ports may depend on
a provider-specific field or endpoint.
"""
from dataclasses import dataclass, field
from typing import Optional, Protocol


@dataclass(frozen=True)
class MailAddress:
    email: str
    name: str = ""


@dataclass(frozen=True)
class AttachmentMeta:
    # Index within the message's attachment list - stable for one message
    # revision and used as the download handle:
    index: int
    filename: str
    content_type: str
    size: int
    # cid marks an inline image referenced from the HTML body:
    cid: Optional[str] = None
    is_inline: bool = False


@dataclass(frozen=True)
class AttachmentContent:
    filename: str
    content_type: str
    data: bytes


@dataclass(frozen=True)
class MessageSummary:
    id: str
    folder: str
    subject: str
    sender: MailAddress
    date: Optional[str]
    is_unread: bool
    is_flagged: bool
    has_attachments: bool
    snippet: str
    # RFC 5322 threading identity, when the provider parsed it; clients group
    # summaries into conversations from these:
    message_id_header: Optional[str] = None
    references: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class MessageDetail:
    id: str
    folder: str
    subject: str
    sender: MailAddress
    to: list[MailAddress]
    cc: list[MailAddress]
    reply_to: list[MailAddress]
    date: Optional[str]
    is_unread: bool
    # Sanitization happens in the service layer, so adapters return the body
    # exactly as the provider parsed it:
    html: Optional[str]
    text: Optional[str]
    attachments: list[AttachmentMeta]
    # RFC 5322 threading identity of THIS message (used to build replies):
    message_id_header: Optional[str]
    references: list[str] = field(default_factory=list)
    is_flagged: bool = False


@dataclass(frozen=True)
class MessagePage:
    messages: list[MessageSummary]
    page: int
    page_count: int


@dataclass(frozen=True)
class OutgoingAttachment:
    filename: str
    content_b64: str
    content_type: str
    # A cid makes the attachment an inline image the HTML references:
    cid: Optional[str] = None


@dataclass(frozen=True)
class OutgoingMessage:
    to: list[MailAddress]
    subject: str
    html: str
    text: str
    cc: list[MailAddress] = field(default_factory=list)
    bcc: list[MailAddress] = field(default_factory=list)
    in_reply_to: Optional[str] = None
    references: list[str] = field(default_factory=list)
    attachments: list[OutgoingAttachment] = field(default_factory=list)


class MailboxProvider(Protocol):
    """Where the instance mailbox lives; one implementation per storage provider"""

    def is_configured(self) -> bool: ...

    def list_folders(self) -> list[str]: ...

    def list_messages(self, folder: str, page: int, search: Optional[str]) -> MessagePage: ...

    def get_message(self, message_id: str) -> MessageDetail: ...

    def get_attachment(self, message_id: str, index: int) -> AttachmentContent: ...

    def set_read(self, message_id: str, read: bool) -> None: ...

    def set_flagged(self, message_id: str, flagged: bool) -> None: ...

    def move(self, message_id: str, folder: str) -> None: ...

    def delete(self, message_id: str) -> None: ...

    def append(self, folder: str, message: OutgoingMessage, sender: MailAddress) -> str: ...


class MailSender(Protocol):
    """How outbound inbox mail leaves; one implementation per delivery provider"""

    def is_configured(self) -> bool: ...

    def send(self, message: OutgoingMessage, sender: MailAddress) -> None: ...
