"""Forward Email sender + the Brevo bridge that covers the approval window."""

import pytest

import platform.services.inbox_service.forwardemail as fe_mod
from infrastructure.settings import settings as s
from platform.exceptions import InboxSendException
from platform.services.inbox_service.bridging_sender import BridgingSender
from platform.services.inbox_service.forwardemail import (
    ForwardEmailSender,
    OutboundNotApprovedError,
)
from platform.services.inbox_service.ports import MailAddress, OutgoingAttachment, OutgoingMessage


SENDER = MailAddress(email="erdem@gunseli.org", name="Erdem")


def _message():
    return OutgoingMessage(
        to=[MailAddress(email="bob@b.com")],
        cc=[MailAddress(email="carol@c.com")],
        subject="Re: hi", html="<p>x</p>", text="x",
        in_reply_to="<orig@x>", references=["<root@x>", "<orig@x>"],
        attachments=[OutgoingAttachment(filename="dot.png", content_b64="AA==",
                                        content_type="image/png", cid="dot@p")],
    )


class StubResponse:
    def __init__(self, status_code=200, text=""):
        self.status_code = status_code
        self.text = text


@pytest.fixture(autouse=True)
def _mailbox_creds(monkeypatch):
    monkeypatch.setattr(s, "INBOX_IMAP_EMAIL", "erdem@gunseli.org")
    monkeypatch.setattr(s, "INBOX_IMAP_PASSWORD", "mailbox-pass")


def test_forward_email_sender_maps_the_nodemailer_payload(monkeypatch):
    calls = []

    def stub_post(url, auth=None, json=None, timeout=None):
        calls.append({"url": url, "auth": auth, "json": json})
        return StubResponse(200)

    monkeypatch.setattr(fe_mod.httpx, "post", stub_post)
    ForwardEmailSender().send(_message(), SENDER)

    payload = calls[0]["json"]
    assert calls[0]["url"].endswith("/v1/emails")
    assert calls[0]["auth"] == ("erdem@gunseli.org", "mailbox-pass")  # mailbox credential
    assert payload["from"] == "Erdem <erdem@gunseli.org>"
    assert payload["cc"] == ["carol@c.com"]
    assert payload["inReplyTo"] == "<orig@x>"
    assert payload["references"] == "<root@x> <orig@x>"
    assert payload["attachments"][0]["cid"] == "dot@p"


def test_forward_email_not_approved_raises_signal(monkeypatch):
    monkeypatch.setattr(fe_mod.httpx, "post", lambda *a, **k: StubResponse(
        403, '{"message":"Domain is not approved for outbound SMTP access"}'))
    with pytest.raises(OutboundNotApprovedError):
        ForwardEmailSender().send(_message(), SENDER)


def test_forward_email_other_403_is_a_send_error(monkeypatch):
    monkeypatch.setattr(fe_mod.httpx, "post", lambda *a, **k: StubResponse(403, "forbidden"))
    with pytest.raises(InboxSendException):
        ForwardEmailSender().send(_message(), SENDER)


class _StubSender:
    def __init__(self, configured=True, raises=None):
        self._configured = configured
        self._raises = raises
        self.sent = False

    def is_configured(self):
        return self._configured

    def send(self, message, sender):
        if self._raises:
            raise self._raises
        self.sent = True


def test_bridge_uses_forward_email_when_approved():
    primary, fallback = _StubSender(), _StubSender()
    BridgingSender(primary, fallback).send(_message(), SENDER)
    assert primary.sent and not fallback.sent


def test_bridge_falls_back_to_brevo_while_unapproved(caplog):
    primary = _StubSender(raises=OutboundNotApprovedError())
    fallback = _StubSender(configured=True)
    with caplog.at_level("WARNING"):
        BridgingSender(primary, fallback).send(_message(), SENDER)
    assert fallback.sent
    assert any("bridging" in r.message.lower() for r in caplog.records)


def test_bridge_fails_closed_when_unapproved_and_no_fallback():
    primary = _StubSender(raises=OutboundNotApprovedError())
    fallback = _StubSender(configured=False)
    with pytest.raises(InboxSendException):
        BridgingSender(primary, fallback).send(_message(), SENDER)


def test_bridge_is_configured_whenever_primary_is():
    # Primary reuses the mailbox credential, so a configured mailbox is always
    # send-capable even with no Brevo fallback:
    assert BridgingSender(_StubSender(configured=True), _StubSender(configured=False)).is_configured()
    assert not BridgingSender(_StubSender(configured=False), _StubSender(configured=False)).is_configured()


def test_outbound_signature_chrome_reaches_forward_email_payload(monkeypatch):
    """Compose-style signature HTML must keep bgcolor chrome after sanitize→wire."""
    from platform.services.inbox_service.sanitize import sanitize_email_html

    signature_html = (
        '<p>Body</p><p></p>'
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0">'
        "<tr>"
        '<td bgcolor="#0b64f4" width="4" height="48"></td>'
        '<td bgcolor="#111827">'
        '<img src="https://www.quant-soc.com/logos/email-signature-dark.png" '
        'alt="QuantSoc" width="48" height="48">'
        '<a href="tel:+15551212">+15551212</a>'
        "</td>"
        "</tr>"
        "</table>"
    )
    # Same single sanitize the service applies before _send:
    wire_html = sanitize_email_html(signature_html)

    calls = []

    def stub_post(url, auth=None, json=None, timeout=None):
        calls.append(json or {})
        return StubResponse(200)

    monkeypatch.setattr(fe_mod.httpx, "post", stub_post)
    ForwardEmailSender().send(
        OutgoingMessage(
            to=[MailAddress(email="bob@b.com")],
            subject="sig chrome",
            html=wire_html,
            text="Body",
        ),
        SENDER,
    )

    payload_html = calls[0]["html"]
    assert 'bgcolor="#0b64f4"' in payload_html
    assert 'bgcolor="#111827"' in payload_html
    assert "https://www.quant-soc.com/logos/email-signature-dark.png" in payload_html
    assert 'href="tel:+15551212"' in payload_html
    assert "<svg" not in payload_html.lower()
