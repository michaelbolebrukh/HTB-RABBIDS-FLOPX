"""Self-healing sender composition: Forward Email primary, Brevo bridge.

Realizes the "Brevo until Forward Email approves outbound, then full-stack
Forward Email" plan as code, with no config change at the transition:

- Forward Email send uses the mailbox's own credential (one provider, one
  credential for the whole inbox; no tracking pixels).
- A brand-new instance whose domain Forward Email has not yet approved for
  outbound gets bridged through Brevo (already present platform-wide for
  transactional email), logged at WARNING as a known-degraded bridge.
- The moment Forward Email approves the domain, sends silently upgrade to it
  - the primary simply stops raising OutboundNotApprovedError.

The fallback is optional: with no Brevo configured, an unapproved domain
fails closed with a clear message rather than half-working.
"""
import logging

from platform.exceptions import InboxSendException
from platform.services.inbox_service.forwardemail import OutboundNotApprovedError
from platform.services.inbox_service.ports import MailAddress, MailSender, OutgoingMessage


logger = logging.getLogger(__name__)


class BridgingSender:
    """MailSender that prefers Forward Email and bridges via a fallback."""

    def __init__(self, primary: MailSender, fallback: MailSender):
        self._primary = primary
        self._fallback = fallback

    def is_configured(self) -> bool:
        # Primary reuses the mailbox credential, so it is configured whenever
        # the inbox is; the fallback only matters during the bridge window:
        return self._primary.is_configured() or self._fallback.is_configured()

    def send(self, message: OutgoingMessage, sender: MailAddress) -> None:
        try:
            self._primary.send(message, sender)
            return
        except OutboundNotApprovedError:
            pass

        if not self._fallback.is_configured():
            logger.error(
                "Inbox send blocked: %s is not yet approved for Forward Email "
                "outbound and no fallback sender is configured.",
                sender.email,
            )
            raise InboxSendException

        logger.warning(
            "Forward Email outbound not yet approved for %s; bridging this send "
            "via the fallback provider until approval lands.",
            sender.email,
        )
        self._fallback.send(message, sender)
