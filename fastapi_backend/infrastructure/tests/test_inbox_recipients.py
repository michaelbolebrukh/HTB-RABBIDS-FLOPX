"""Recipient/threading assembly matrix - the subtlest logic the inbox owns."""

from platform.services.inbox_service.ports import MailAddress, MessageDetail
from platform.services.inbox_service.recipients import (
    forward_subject,
    reply_recipients,
    reply_subject,
    reply_threading,
)


OWN = "hello@instance.io"


def _detail(sender=None, to=(), cc=(), reply_to=(), subject="Hi",
            message_id="<orig@x>", references=()):
    return MessageDetail(
        id="m1", folder="INBOX", subject=subject,
        sender=sender or MailAddress(email="alice@a.com", name="Alice"),
        to=list(to), cc=list(cc), reply_to=list(reply_to),
        date=None, is_unread=False, html=None, text=None, attachments=[],
        message_id_header=message_id, references=list(references),
    )


def test_reply_targets_sender_and_keeps_other_recipients():
    original = _detail(
        to=[MailAddress(email=OWN), MailAddress(email="bob@b.com", name="Bob")],
        cc=[MailAddress(email="carol@c.com")],
    )
    to, cc = reply_recipients(original, own_email=OWN)

    assert [a.email for a in to] == ["alice@a.com", "bob@b.com"]  # self dropped
    assert [a.email for a in cc] == ["carol@c.com"]


def test_reply_to_header_beats_sender():
    original = _detail(reply_to=[MailAddress(email="support@a.com")], to=[MailAddress(email=OWN)])
    to, _ = reply_recipients(original, own_email=OWN)
    assert [a.email for a in to] == ["support@a.com"]


def test_reply_dedupes_across_lines_case_insensitively():
    original = _detail(
        sender=MailAddress(email="Alice@A.com"),
        to=[MailAddress(email="alice@a.com"), MailAddress(email=OWN)],
        cc=[MailAddress(email="ALICE@a.com"), MailAddress(email="dave@d.com")],
    )
    to, cc = reply_recipients(original, own_email=OWN)
    assert [a.email for a in to] == ["Alice@A.com"]  # one alice, self gone
    assert [a.email for a in cc] == ["dave@d.com"]   # cc drops to-line duplicates


def test_replying_to_own_sent_message_falls_back_to_original_to():
    original = _detail(sender=MailAddress(email=OWN), to=[MailAddress(email="bob@b.com")])
    to, _ = reply_recipients(original, own_email=OWN)
    assert [a.email for a in to] == ["bob@b.com"]


def test_subject_prefixes_are_idempotent_and_case_insensitive():
    assert reply_subject("Hello") == "Re: Hello"
    assert reply_subject("Re: Hello") == "Re: Hello"
    assert reply_subject("re: Hello") == "re: Hello"
    assert forward_subject("Hello") == "Fwd: Hello"
    assert forward_subject("fwd: Hello") == "fwd: Hello"


def test_threading_appends_original_id_to_references():
    original = _detail(message_id="<orig@x>", references=["<root@x>"])
    in_reply_to, references = reply_threading(original)
    assert in_reply_to == "<orig@x>"
    assert references == ["<root@x>", "<orig@x>"]

    # Already present = not duplicated:
    original = _detail(message_id="<orig@x>", references=["<orig@x>"])
    _, references = reply_threading(original)
    assert references == ["<orig@x>"]
