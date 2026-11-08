"""Provider adapters against the payload shapes verified live (2026-07-11).

The fixture dicts below mirror real Forward Email responses captured from the
test mailbox, reduced to the fields the adapter reads.
"""
import base64

import pytest

import platform.services.inbox_service.brevo_sender as brevo_mod
import platform.services.inbox_service.forwardemail as fe_mod
from infrastructure.settings import settings as s
from platform.exceptions import InboxAttachmentNotFoundException, InboxSendException
from platform.services.inbox_service.brevo_sender import BrevoInboxSender
from platform.services.inbox_service.forwardemail import ForwardEmailMailbox
from platform.services.inbox_service.ports import MailAddress, OutgoingAttachment, OutgoingMessage


LIST_ITEM = {
    "id": "6a51a834fa365ad7637f09ea",
    "folder_path": "INBOX",
    "subject": "Inbox test 2: erdem mailbox",
    "internal_date": "2026-07-11T02:19:20.000Z",
    "is_unread": True,
    "has_attachment": 0,
    "nodemailer": {
        "from": {"value": [{"address": "hello@quant-soc.com", "name": "Quant Soc"}]},
        "text": "  First   line of the\nbody  ",
        "html": False,
    },
}

DETAIL_ITEM = {
    "id": "6a51a834fa365ad7637f09ea",
    "folder_path": "INBOX",
    "subject": "Inbox test 2: erdem mailbox",
    "internal_date": "2026-07-11T02:19:20.000Z",
    "is_unread": False,
    "nodemailer": {
        "from": {"value": [{"address": "hello@quant-soc.com", "name": "Quant Soc"}]},
        "to": {"value": [{"address": "erdem@gunseli.org", "name": ""}]},
        "cc": {"value": [{"address": "carol@c.com", "name": "Carol"}]},
        "headers": {"references": "<root@x> <mid@x>"},
        "messageId": "<202607110219.23383726979@smtp-relay.mailin.fr>",
        "html": "<p><b>bold</b> body</p>",
        "text": "bold body",
        "attachments": [
            {
                "filename": "dot.png", "contentType": "image/png", "size": 68,
                "cid": "i1@p", "contentDisposition": "inline",
                "content": {"type": "Buffer", "data": [1, 2, 3]},
            },
            {
                "filename": "note.txt", "contentType": "text/plain", "size": 5,
                "content": base64.b64encode(b"hello").decode(),
            },
        ],
    },
}


class StubResponse:
    def __init__(self, payload, status_code=200, headers=None):
        self._payload = payload
        self.status_code = status_code
        self.headers = headers or {}

    def json(self):
        return self._payload


@pytest.fixture
def mailbox(monkeypatch):
    monkeypatch.setattr(s, "INBOX_IMAP_EMAIL", "erdem@gunseli.org")
    monkeypatch.setattr(s, "INBOX_IMAP_PASSWORD", "mailbox-pass")
    return ForwardEmailMailbox()


def _patch_request(monkeypatch, response, calls):
    def stub(method, url, **kwargs):
        calls.append({"method": method, "url": url, **kwargs})
        return response

    monkeypatch.setattr(fe_mod.httpx, "request", stub)


def test_list_messages_maps_summary_and_pagination(mailbox, monkeypatch):
    calls = []
    _patch_request(monkeypatch, StubResponse(
        [LIST_ITEM], headers={"X-Page-Current": "2", "X-Page-Count": "5"}), calls)

    page = mailbox.list_messages(folder="INBOX", page=2, search="erdem")

    assert calls[0]["params"] == {"folder": "INBOX", "page": 2, "search": "erdem"}
    assert page.page == 2 and page.page_count == 5
    message = page.messages[0]
    assert message.sender == MailAddress(email="hello@quant-soc.com", name="Quant Soc")
    assert message.is_unread is True
    assert message.snippet == "First line of the body"


def test_get_message_maps_addresses_threading_and_attachments(mailbox, monkeypatch):
    _patch_request(monkeypatch, StubResponse(DETAIL_ITEM), [])

    detail = mailbox.get_message("6a51a834fa365ad7637f09ea")

    assert detail.to[0].email == "erdem@gunseli.org"
    assert detail.cc[0].name == "Carol"
    assert detail.message_id_header == "<202607110219.23383726979@smtp-relay.mailin.fr>"
    assert detail.references == ["<root@x>", "<mid@x>"]
    inline, regular = detail.attachments
    assert inline.cid == "i1@p" and inline.is_inline is True
    assert regular.is_inline is False


def test_get_attachment_decodes_both_content_encodings(mailbox, monkeypatch):
    _patch_request(monkeypatch, StubResponse(DETAIL_ITEM), [])

    assert mailbox.get_attachment("m", 0).data == b"\x01\x02\x03"   # Buffer dict
    assert mailbox.get_attachment("m", 1).data == b"hello"          # base64 str
    with pytest.raises(InboxAttachmentNotFoundException):
        mailbox.get_attachment("m", 9)


def test_flag_updates_preserve_sibling_flags(mailbox, monkeypatch):
    calls = []
    # GETs return the current flag set; PUTs must merge, never clobber:
    _patch_request(monkeypatch, StubResponse({"flags": ["\\Flagged"]}), calls)
    mailbox.set_read("m1", read=True)
    puts = [c for c in calls if c["method"] == "PUT"]
    assert puts[0]["json"] == {"flags": ["\\Flagged", "\\Seen"]}

    calls.clear()
    _patch_request(monkeypatch, StubResponse({"flags": ["\\Flagged", "\\Seen"]}), calls)
    mailbox.set_read("m1", read=False)
    puts = [c for c in calls if c["method"] == "PUT"]
    assert puts[0]["json"] == {"flags": ["\\Flagged"]}

    calls.clear()
    _patch_request(monkeypatch, StubResponse({"flags": ["\\Seen"]}), calls)
    mailbox.set_flagged("m1", flagged=True)
    puts = [c for c in calls if c["method"] == "PUT"]
    assert puts[0]["json"] == {"flags": ["\\Flagged", "\\Seen"]}


def test_append_files_a_full_copy(mailbox, monkeypatch):
    calls = []
    _patch_request(monkeypatch, StubResponse({"id": "new-id"}), calls)

    mailbox.append("Sent Mail",
        OutgoingMessage(
            to=[MailAddress(email="bob@b.com")],
            cc=[MailAddress(email="carol@c.com")],
            subject="Re: hi", html="<p>x</p>", text="x",
            attachments=[OutgoingAttachment(filename="a.txt", content_b64="QQ==",
                                            content_type="text/plain")],
        ),
        sender=MailAddress(email="erdem@gunseli.org", name="Erdem"),
    )

    body = calls[0]["json"]
    assert body["folder"] == "Sent Mail"
    assert body["from"] == "Erdem <erdem@gunseli.org>"
    assert body["to"] == ["bob@b.com"] and body["cc"] == ["carol@c.com"]
    assert body["attachments"][0]["encoding"] == "base64"


def test_brevo_sender_maps_the_full_payload(monkeypatch):
    monkeypatch.setattr(s, "BREVO_API_KEY", "brevo-key")
    calls = []

    def stub_post(url, json=None, headers=None, timeout=None):
        calls.append({"url": url, "json": json, "headers": headers})
        return StubResponse({}, status_code=201)

    monkeypatch.setattr(brevo_mod.httpx, "post", stub_post)

    BrevoInboxSender().send(
        OutgoingMessage(
            to=[MailAddress(email="bob@b.com")],
            cc=[MailAddress(email="carol@c.com")],
            subject="Re: hi", html="<p>x</p>", text="x",
            in_reply_to="<orig@x>", references=["<root@x>", "<orig@x>"],
            attachments=[OutgoingAttachment(filename="dot.png", content_b64="AA==",
                                            content_type="image/png", cid="dot.png")],
        ),
        sender=MailAddress(email="erdem@gunseli.org", name="Erdem"),
    )

    payload = calls[0]["json"]
    assert payload["sender"] == {"email": "erdem@gunseli.org", "name": "Erdem"}
    assert payload["cc"] == [{"email": "carol@c.com"}]
    assert payload["headers"] == {"In-Reply-To": "<orig@x>", "References": "<root@x> <orig@x>"}
    assert payload["attachment"] == [{"name": "dot.png", "content": "AA=="}]
    assert calls[0]["headers"] == {"api-key": "brevo-key"}


def test_brevo_sender_raises_on_rejection(monkeypatch):
    monkeypatch.setattr(s, "BREVO_API_KEY", "brevo-key")
    monkeypatch.setattr(brevo_mod.httpx, "post",
                        lambda *a, **k: StubResponse({}, status_code=403))

    with pytest.raises(InboxSendException):
        BrevoInboxSender().send(
            OutgoingMessage(to=[MailAddress(email="b@b.com")], subject="s", html="h", text="t"),
            sender=MailAddress(email="erdem@gunseli.org"),
        )
