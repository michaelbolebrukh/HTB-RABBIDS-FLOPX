"""Brevo implementation of the MailSender port.

Sends inbox replies/compositions through the instance's existing Brevo account
(the same provider the platform's transactional email uses), From the mailbox
identity. Chosen over Forward Email outbound because Brevo sending rights are
already earned per instance during transactional email setup, while Forward
Email outbound sits behind a manual per-domain approval queue; once an
instance's domain IS approved there, a ForwardEmailSender can replace this
class behind the port with no changes above it.

Delivery facts verified live (2026-07-11): DKIM aligns with the mailbox domain
once the domain is authenticated in Brevo, In-Reply-To/References headers pass
through, attachments and inline (cid) images arrive intact.
"""
import logging

import httpx

from infrastructure.settings import settings as s
from platform.exceptions import InboxSendException
from platform.services.inbox_service.ports import MailAddress, OutgoingMessage


logger = logging.getLogger(__name__)

_SEND_URL = "https://api.brevo.com/v3/smtp/email"
_TIMEOUT_SECONDS = 15.0


class BrevoInboxSender:
    """MailSender backed by Brevo's transactional HTTP API"""

    def is_configured(self) -> bool:
        return bool(s.BREVO_API_KEY)

    def send(self, message: OutgoingMessage, sender: MailAddress) -> None:
        payload: dict = {
            "sender": {"email": sender.email, **({"name": sender.name} if sender.name else {})},
            "to": [{"email": addr.email} for addr in message.to],
            "subject": message.subject,
            "htmlContent": message.html,
            "textContent": message.text,
        }
        if message.cc:
            payload["cc"] = [{"email": addr.email} for addr in message.cc]
        if message.bcc:
            payload["bcc"] = [{"email": addr.email} for addr in message.bcc]

        headers: dict[str, str] = {}
        if message.in_reply_to:
            headers["In-Reply-To"] = message.in_reply_to
        if message.references:
            headers["References"] = " ".join(message.references)
        if headers:
            payload["headers"] = headers

        if message.attachments:
            # Brevo turns an attachment whose name the HTML references via
            # cid:<name> into a proper inline part (verified live), so both
            # regular files and pasted inline images travel this one field:
            payload["attachment"] = [
                {"name": att.filename, "content": att.content_b64}
                for att in message.attachments
            ]

        try:
            response = httpx.post(
                _SEND_URL,
                json=payload,
                headers={"api-key": s.BREVO_API_KEY},
                timeout=_TIMEOUT_SECONDS,
            )
        except httpx.HTTPError:
            logger.exception("Brevo send request failed to reach the API.")
            raise InboxSendException

        if response.status_code >= 300:
            # Logging status only - the response can echo recipient addresses:
            logger.error("Brevo rejected an inbox send (status %s).", response.status_code)
            raise InboxSendException
