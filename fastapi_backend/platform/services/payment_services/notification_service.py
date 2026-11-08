"""Payment emails: customer receipts/welcomes and internal operator alerts.

Two audiences, one delivery path (`send_templated_email` + the generated
templates, so every message carries the brand and is not hand-assembled text):

- Customer emails go to the buyer: an order confirmation for one-time purchases
  and a welcome for subscriptions. These are transactional (sent regardless of
  marketing consent) and reply to the support inbox.
- Operator alerts go to the ops inbox (CONTACT_EMAIL). They carry standards-based
  categorization headers (List-Id, Auto-Submitted) plus provider tags so the
  recipient can filter them out of the main inbox with a single rule.

Best-effort by design: a send failure is logged and swallowed, and the send runs
off the caller's thread, so it can never slow or break webhook processing (which
must return 200 quickly so Stripe stops retrying).
"""
import logging
import threading
from datetime import datetime, timezone
from typing import Optional

from platform.enums import EmailTemplateType, ProductType
from platform.services.email_services import send_templated_email
from infrastructure.settings import settings as s


logger = logging.getLogger(__name__)


def _admin_recipient() -> Optional[str]:
    """Where internal alerts go: the operator/support inbox.

    Uses CONTACT_EMAIL (the ops address derived from APP_DOMAIN, e.g.
    hello@domain), matching how contact_service routes its admin notification.
    Deliberately NOT ADMIN_EMAIL: that is the admin user's personal login address
    (used to seed the admin account), not a notification destination.
    """
    return s.CONTACT_EMAIL


def _ops_headers(topic: str) -> dict[str, str]:
    """Standards-based signals that let the operator inbox filter automated alerts.

    There is no header that forces mail into a folder across every provider (that
    is always a recipient-side rule), but List-Id (RFC 2919) and Auto-Submitted
    (RFC 3834) are widely filterable, so one rule (e.g. Gmail `list:<domain>`)
    routes all payment alerts to a label out of the main inbox. `topic` scopes the
    List-Id (orders vs disputes) so they can be separated further if wanted.
    """
    domain = s.APP_DOMAIN or "localhost"
    return {
        "List-Id": f"{s.SENDER_NAME or 'Payments'} alerts <{topic}.payments.{domain}>",
        "Auto-Submitted": "auto-generated",
    }


def _money(amount_minor: int, currency: str) -> str:
    return f"{(amount_minor or 0) / 100:.2f} {(currency or 'gbp').upper()}"


def _date(created: Optional[int]) -> str:
    """Formats a Stripe unix timestamp (the checkout session's `created`) as a
    human date for the receipt; falls back to now if absent."""
    dt = (
        datetime.fromtimestamp(created, tz=timezone.utc)
        if created
        else datetime.now(timezone.utc)
    )
    return f"{dt.day} {dt.strftime('%B %Y')}"


def _dispatch(
    template_type: EmailTemplateType,
    recipient: Optional[str],
    *,
    reply_to: Optional[str] = None,
    headers: Optional[dict[str, str]] = None,
    tags: Optional[list[str]] = None,
    **variables,
) -> None:
    """Renders and sends a payment email off the caller's thread, best-effort.

    Called from inside the Stripe webhook handler, which must return quickly. A
    slow mail provider would otherwise add its latency to the webhook response, so
    the render+send runs in a fire-and-forget daemon thread. It touches no DB
    session (only plain values), so detaching from the request is safe.
    """
    if not recipient:
        return

    def _run() -> None:
        try:
            send_templated_email(
                template_type=template_type,
                recipient=recipient,
                reply_to=reply_to,
                headers=headers,
                tags=tags,
                **variables,
            )
        except Exception:
            logger.exception("Failed to send payment email: %s", template_type)

    threading.Thread(target=_run, name="payment-email", daemon=True).start()


# ============================================================================
# CUSTOMER EMAILS (to the buyer)
# ============================================================================

def send_order_confirmation(
    *,
    recipient_email: Optional[str],
    recipient_name: Optional[str],
    item_name: str,
    amount_minor: int,
    currency: str,
    quantity: int,
    reference: Optional[str],
    created: Optional[int] = None,
) -> None:
    """Emails the buyer a confirmation for a completed one-time purchase"""
    _dispatch(
        EmailTemplateType.PAYMENT_ORDER_CONFIRMATION,
        recipient_email,
        reply_to=s.CONTACT_EMAIL,
        tags=["payments", "order-confirmation"],
        name=recipient_name or "there",
        item_name=item_name,
        amount=_money(amount_minor, currency),
        quantity=str(quantity or 1),
        reference=reference or "unavailable",
        date=_date(created),
    )


def send_subscription_renewal(
    *,
    recipient_email: Optional[str],
    recipient_name: Optional[str],
    plan_name: str,
    amount_minor: int,
    currency: str,
    reference: Optional[str],
    created: Optional[int] = None,
) -> None:
    """Emails the buyer a receipt when a subscription renews (recurring cycle)"""
    _dispatch(
        EmailTemplateType.PAYMENT_SUBSCRIPTION_RENEWAL,
        recipient_email,
        reply_to=s.CONTACT_EMAIL,
        tags=["payments", "subscription-renewal"],
        name=recipient_name or "there",
        plan_name=plan_name,
        amount=_money(amount_minor, currency),
        reference=reference or "unavailable",
        date=_date(created),
    )


def send_refund_confirmation(
    *,
    recipient_email: Optional[str],
    recipient_name: Optional[str],
    item_name: str,
    amount_minor: int,
    currency: str,
    reference: Optional[str],
    created: Optional[int] = None,
) -> None:
    """Emails the buyer a confirmation when a refund is issued (full or partial);
    `amount_minor` is the amount refunded."""
    _dispatch(
        EmailTemplateType.PAYMENT_REFUND_CONFIRMATION,
        recipient_email,
        reply_to=s.CONTACT_EMAIL,
        tags=["payments", "refund-confirmation"],
        name=recipient_name or "there",
        item_name=item_name,
        amount=_money(amount_minor, currency),
        reference=reference or "unavailable",
        date=_date(created),
    )


def send_subscription_welcome(
    *,
    recipient_email: Optional[str],
    recipient_name: Optional[str],
    plan_name: str,
    amount_minor: int,
    currency: str,
    reference: Optional[str],
) -> None:
    """Emails the buyer a welcome when a subscription starts"""
    _dispatch(
        EmailTemplateType.PAYMENT_SUBSCRIPTION_WELCOME,
        recipient_email,
        reply_to=s.CONTACT_EMAIL,
        tags=["payments", "subscription-welcome"],
        name=recipient_name or "there",
        plan_name=plan_name,
        amount=_money(amount_minor, currency),
        reference=reference or "unavailable",
    )


# ============================================================================
# OPERATOR ALERTS (to the ops inbox)
# ============================================================================

def notify_admin_order(
    *,
    item_key: str,
    item_type: str,
    amount_minor: int,
    currency: str,
    quantity: int,
    user_id: Optional[str],
    ships: bool,
    session_id: Optional[str],
) -> None:
    """Alerts the operator that a new order or subscription completed.

    Subscriptions and one-time orders use distinct templates so each reads for
    what it is (a subscription has no quantity or shipping).
    """
    amount = _money(amount_minor, currency)
    if item_type == ProductType.SUBSCRIPTION.value:
        _dispatch(
            EmailTemplateType.PAYMENT_SUBSCRIPTION_NOTIFICATION,
            _admin_recipient(),
            headers=_ops_headers("orders"),
            tags=["payments", "ops-alert"],
            item_key=item_key,
            amount=amount,
            user_id=user_id or "unknown",
            session_id=session_id or "unknown",
        )
    else:
        _dispatch(
            EmailTemplateType.PAYMENT_ORDER_NOTIFICATION,
            _admin_recipient(),
            headers=_ops_headers("orders"),
            tags=["payments", "ops-alert"],
            item_key=item_key,
            item_type=item_type,
            amount=amount,
            quantity=quantity,
            user_id=user_id or "unknown",
            session_id=session_id or "unknown",
            ships="Yes (see the Stripe dashboard for the address)" if ships else "No",
        )


def notify_admin_dispute(
    *,
    charge_id: Optional[str],
    amount_minor: int,
    currency: str,
    reason: Optional[str],
    item_key: Optional[str],
    user_id: Optional[str],
) -> None:
    """Alerts the operator that a payment dispute was opened"""
    _dispatch(
        EmailTemplateType.PAYMENT_DISPUTE_NOTIFICATION,
        _admin_recipient(),
        headers=_ops_headers("disputes"),
        tags=["payments", "dispute", "ops-alert"],
        charge_id=charge_id or "unknown",
        amount=_money(amount_minor, currency),
        reason=reason or "unspecified",
        item_key=item_key or "unmatched",
        user_id=user_id or "unknown",
    )
