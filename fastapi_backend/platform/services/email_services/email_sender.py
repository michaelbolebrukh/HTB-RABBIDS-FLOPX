import logging
from typing import Optional

from brevo import Brevo
from brevo.transactional_emails import (
    SendTransacEmailRequestReplyTo,
    SendTransacEmailRequestSender,
    SendTransacEmailRequestToItem,
)
from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail, Email, ReplyTo, Header, Category

from platform.exceptions import EmailSendException
from infrastructure.settings import settings as s


logger = logging.getLogger(__name__)


def set_sendgrid_data_residency(client: SendGridAPIClient) -> None:
    """Configures SendGrid client with regional data residency settings"""
    try:
        if s.SENDGRID_BASE_HOST:
            client.client.host = s.SENDGRID_BASE_HOST
            logger.debug(f"SendGrid host set to: {client.client.host}")
        else:
            logger.debug("SendGrid host not overridden; using SDK default")
    except Exception as e:
        logger.warning(f"Failed to configure SendGrid host: {e}")


# Initializing provider clients once at import:
_brevo_client: Optional[Brevo] = None
if s.EMAIL_PROVIDER == "BREVO" and s.BREVO_API_KEY:
    _brevo_client = Brevo(api_key=s.BREVO_API_KEY)

_sg_client: Optional[SendGridAPIClient] = None
if s.EMAIL_PROVIDER == "SENDGRID" and s.SENDGRID_API_KEY:
    _sg_client = SendGridAPIClient(s.SENDGRID_API_KEY)
    set_sendgrid_data_residency(_sg_client)


def provider_is_configured() -> bool:
    """Whether this instance can actually send mail at all.

    Read off the clients built above rather than re-testing the provider
    settings, so a caller that gates work on "is there any point sending?" (the
    question flow sweep) cannot drift from the condition that decides whether a
    client exists. `CONTACT_EMAIL` is included because `send_email` refuses
    without a sender address regardless of the provider:
    """
    return bool(s.CONTACT_EMAIL) and (_brevo_client is not None or _sg_client is not None)


def _unsubscribe_headers(one_click_url: Optional[str], page_url: Optional[str]) -> dict[str, str]:
    """RFC 8058 headers for marketing sends. Prefers the one-click POST target
    (what Gmail/Yahoo's native Unsubscribe button uses); falls back to the
    human confirm page when no public backend origin is configured."""
    target = one_click_url or page_url
    if not target:
        return {}
    headers = {"List-Unsubscribe": f"<{target}>"}
    if one_click_url:
        headers["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
    return headers


def send_email(
    recipient: str,
    subject: str,
    html_body: str,
    text_body: str,
    reply_to: Optional[str] = None,
    headers: Optional[dict[str, str]] = None,
    tags: Optional[list[str]] = None,
    unsubscribe_one_click_url: Optional[str] = None,
    unsubscribe_page_url: Optional[str] = None,
) -> None:
    """Sends an email via the configured provider with HTML and text versions.

    Optional `headers` set standards-based signals (e.g. List-Id, Auto-Submitted)
    and `tags` feed the provider's categorization/analytics. Marketing sends pass
    the unsubscribe URLs so the RFC 8058 headers are set; transactional sends
    omit them (exempt, and they must stay deliverable).
    """
    headers = {**(headers or {}), **_unsubscribe_headers(unsubscribe_one_click_url, unsubscribe_page_url)}
    try:
        if not s.CONTACT_EMAIL:
            raise EmailSendException("Missing CONTACT_EMAIL for sender")

        if s.EMAIL_PROVIDER == "BREVO":
            _send_brevo_email(recipient, subject, html_body, text_body, reply_to, headers, tags)
        elif s.EMAIL_PROVIDER == "SENDGRID":
            _send_sendgrid_email(recipient, subject, html_body, text_body, reply_to, headers, tags)
        else:
            raise EmailSendException("Unsupported email provider")

    except EmailSendException:
        raise
    except Exception as e:
        logger.exception(f"Unexpected {s.EMAIL_PROVIDER} email send failure: {e}")
        # Raising a generic exception to avoid exposing sensitive details:
        raise EmailSendException


def _send_brevo_email(
    recipient: str,
    subject: str,
    html_body: str,
    text_body: str,
    reply_to: Optional[str] = None,
    headers: Optional[dict[str, str]] = None,
    tags: Optional[list[str]] = None,
) -> None:
    """Sends an email via Brevo's transactional email API"""
    if not _brevo_client:
        raise EmailSendException("Brevo client is not initialized; missing BREVO_API_KEY")

    reply_to_address = reply_to or s.CONTACT_EMAIL

    _brevo_client.transactional_emails.send_transac_email(
        sender=SendTransacEmailRequestSender(email=s.CONTACT_EMAIL, name=s.SENDER_NAME or ""),
        to=[SendTransacEmailRequestToItem(email=recipient)],
        subject=subject,
        html_content=html_body,
        text_content=text_body,
        reply_to=SendTransacEmailRequestReplyTo(email=reply_to_address) if reply_to_address else None,
        headers=headers or None,
        tags=tags or None,
    )


def _send_sendgrid_email(
    recipient: str,
    subject: str,
    html_body: str,
    text_body: str,
    reply_to: Optional[str] = None,
    headers: Optional[dict[str, str]] = None,
    tags: Optional[list[str]] = None,
) -> None:
    """Sends an email via SendGrid API with HTML and text versions"""
    if not _sg_client:
        raise EmailSendException("SendGrid client is not initialized; missing SENDGRID_API_KEY")

    reply_to_address = reply_to or s.CONTACT_EMAIL

    message = Mail(
        from_email=Email(email=s.CONTACT_EMAIL, name=s.SENDER_NAME or ""),
        to_emails=recipient,
        subject=subject,
        html_content=html_body,
        plain_text_content=text_body,
    )
    if reply_to_address:
        message.reply_to = ReplyTo(email=reply_to_address)
    for key, value in (headers or {}).items():
        message.add_header(Header(key, value))
    for tag in (tags or []):
        message.add_category(Category(tag))

    response = _sg_client.send(message)

    status_code = getattr(response, "status_code", None)
    if not isinstance(status_code, int) or status_code < 200 or status_code >= 300:
        raise EmailSendException(f"Unexpected SendGrid response code: {status_code}")
