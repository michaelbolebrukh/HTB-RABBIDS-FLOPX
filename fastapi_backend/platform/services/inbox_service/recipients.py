"""Pure recipient/threading assembly for replies and forwards.

The subtlest logic the inbox owns, so it lives in pure functions with a full
test matrix (test_inbox_recipients.py) and no provider or HTTP dependencies.
"""
from platform.services.inbox_service.ports import MailAddress, MessageDetail


_REPLY_PREFIX = "Re:"
_FORWARD_PREFIX = "Fwd:"


def _dedupe(addresses: list[MailAddress], exclude: set[str]) -> list[MailAddress]:
    seen: set[str] = set()
    result = []
    for addr in addresses:
        key = addr.email.lower()
        if not key or key in exclude or key in seen:
            continue
        seen.add(key)
        result.append(addr)
    return result


def reply_recipients(original: MessageDetail, own_email: str) -> tuple[list[MailAddress], list[MailAddress]]:
    """Reply-all semantics: (to, cc) for a reply to `original`.

    To = Reply-To when set, else the sender, plus the original To line;
    Cc = the original Cc line; the mailbox's own address drops out everywhere
    so a reply never emails ourselves.
    """
    own = {own_email.lower()}

    primary = original.reply_to or ([original.sender] if original.sender.email else [])
    to = _dedupe(primary + original.to, exclude=own)
    cc = _dedupe(original.cc, exclude=own | {addr.email.lower() for addr in to})

    # Replying to a message we sent ourselves (e.g. from Sent Mail) would
    # otherwise produce an empty To line - falling back to the original To:
    if not to:
        to = _dedupe(original.to, exclude=set())
    return to, cc


def _prefixed(subject: str, prefix: str) -> str:
    """Idempotent subject prefixing: never stacks Re: Re: Re:"""
    stripped = subject.strip()
    if stripped.lower().startswith(prefix.lower()):
        return stripped
    return f"{prefix} {stripped}".strip()


def reply_subject(original_subject: str) -> str:
    return _prefixed(original_subject, _REPLY_PREFIX)


def forward_subject(original_subject: str) -> str:
    return _prefixed(original_subject, _FORWARD_PREFIX)


def reply_threading(original: MessageDetail) -> tuple[str | None, list[str]]:
    """(In-Reply-To, References) headers for a reply, per RFC 5322 threading"""
    in_reply_to = original.message_id_header
    references = list(original.references)
    if in_reply_to and in_reply_to not in references:
        references.append(in_reply_to)
    return in_reply_to, references
