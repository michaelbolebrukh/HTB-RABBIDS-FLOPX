"""Instance-branded renewal + refund receipt emails from the Stripe webhook.

Covers the handlers added alongside the existing order-confirmation receipt:
`_handle_invoice_paid` (subscription renewals) and the refund-email branch of
`_handle_charge_refunded`, plus the `_date` receipt-date helper.
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from infrastructure.database import Base
import platform.models  # noqa: F401 — registers tables on Base.metadata
import product.models  # noqa: F401
from platform.models import User, Purchase
from platform.enums import Role, PurchaseStatus, ProductType
from platform.services.payment_services import webhook_service as wh
from platform.services.payment_services.notification_service import _date


class _FakeItem:
    def __init__(self, name):
        self.name = name
        self.requires_shipping = False


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(autocommit=False, autoflush=False, bind=engine)()
    try:
        yield session
    finally:
        session.close()


def _user(db):
    u = User(name="Alex", email="alex@example.com", role=Role.USER, stripe_customer_id="cus_1")
    db.add(u)
    db.flush()
    return u


def _purchase(db, user_id, *, session="cs_1", sub=None, pi=None):
    p = Purchase(
        user_id=user_id,
        item_key="ultra",
        item_type=ProductType.SUBSCRIPTION.value,
        stripe_checkout_session_id=session,
        stripe_subscription_id=sub,
        stripe_payment_intent_id=pi,
        amount_total_minor=1500,
        currency="GBP",
        status=PurchaseStatus.PAID,
    )
    db.add(p)
    db.flush()
    return p


# --- invoice.paid → renewal receipt ---------------------------------------

def test_invoice_paid_sends_renewal_for_cycle(db, monkeypatch):
    u = _user(db)
    _purchase(db, u.id, sub="sub_1")
    monkeypatch.setattr(wh, "get_catalog_item", lambda k: _FakeItem("Ultra"))
    sent = {}
    monkeypatch.setattr(wh, "send_subscription_renewal", lambda **kw: sent.update(kw))

    wh._handle_invoice_paid(db, {
        "billing_reason": "subscription_cycle", "subscription": "sub_1",
        "amount_paid": 1500, "currency": "gbp", "number": "INV-001", "created": 1752374400,
    })

    assert sent["recipient_email"] == "alex@example.com"
    assert sent["plan_name"] == "Ultra"
    assert sent["amount_minor"] == 1500
    assert sent["reference"] == "INV-001"  # human invoice number preferred


def test_invoice_paid_skips_initial_invoice(db, monkeypatch):
    # subscription_create is covered by the welcome email; no double-send:
    u = _user(db)
    _purchase(db, u.id, sub="sub_1")
    called = {"n": 0}
    monkeypatch.setattr(wh, "send_subscription_renewal", lambda **kw: called.update(n=called["n"] + 1))
    wh._handle_invoice_paid(db, {"billing_reason": "subscription_create", "subscription": "sub_1"})
    assert called["n"] == 0


def test_invoice_paid_skips_unmirrored_subscription(db, monkeypatch):
    # No local Purchase for this subscription -> belongs to a sibling instance:
    called = {"n": 0}
    monkeypatch.setattr(wh, "send_subscription_renewal", lambda **kw: called.update(n=called["n"] + 1))
    wh._handle_invoice_paid(db, {"billing_reason": "subscription_cycle", "subscription": "sub_unknown"})
    assert called["n"] == 0


# --- charge.refunded → refund receipt -------------------------------------

def test_charge_refunded_emails_customer(db, monkeypatch):
    u = _user(db)
    _purchase(db, u.id, session="cs_9", pi="pi_9")
    monkeypatch.setattr(wh, "get_catalog_item", lambda k: _FakeItem("Quant Quiz Ticket"))
    sent = {}
    monkeypatch.setattr(wh, "send_refund_confirmation", lambda **kw: sent.update(kw))

    wh._handle_charge_refunded(db, {
        "id": "ch_9", "payment_intent": "pi_9", "refunded": True,
        "amount_refunded": 500, "currency": "gbp",
        "refunds": {"data": [{"created": 1752374400}]},
    })

    assert sent["recipient_email"] == "alex@example.com"
    assert sent["item_name"] == "Quant Quiz Ticket"
    assert sent["amount_minor"] == 500                 # amount REFUNDED, not the order total
    assert sent["reference"] == "cs_9"                 # original order reference
    # Purchase status also reconciled:
    assert db.query(Purchase).first().status == PurchaseStatus.REFUNDED


def test_charge_refunded_unmatched_sends_nothing(db, monkeypatch):
    called = {"n": 0}
    monkeypatch.setattr(wh, "send_refund_confirmation", lambda **kw: called.update(n=called["n"] + 1))
    wh._handle_charge_refunded(db, {"id": "ch_x", "payment_intent": "pi_missing", "refunded": True})
    assert called["n"] == 0


# --- receipt date helper ---------------------------------------------------

def test_date_formats_epoch_without_zero_pad():
    assert _date(1752374400) == "13 July 2025"
    assert _date(1738368000) == "1 February 2025"  # no leading zero on the day


def test_date_falls_back_to_now_without_raising():
    assert _date(None)  # non-empty, no exception


# -- Webhook subscription <-> handler parity ----------------------------------

def test_subscribed_events_match_handlers():
    # The set Stripe is asked to send must equal the set the backend handles; a handler
    # added without subscribing (or vice versa) silently drops or wastes events:
    assert wh.SUBSCRIBED_WEBHOOK_EVENTS == tuple(wh._HANDLERS)
    assert "invoice.paid" in wh.SUBSCRIBED_WEBHOOK_EVENTS  # the renewal-receipt event


def test_stripe_ops_skill_events_stay_in_lockstep():
    # The portable stripe-ops skill can't import the backend, so it keeps its own copy of
    # the event list. Guard that copy against drift from the canonical handler map (skip
    # cleanly in a clone that dropped the skill tree):
    import ast
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[3]
    skill = repo_root / ".cursor" / "skills" / "stripe-ops" / "tools" / "stripe.py"
    if not skill.exists():
        pytest.skip("stripe-ops skill not present in this checkout")

    tree = ast.parse(skill.read_text())
    events = None
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "EXPECTED_WEBHOOK_EVENTS" for t in node.targets
        ):
            events = [e.value for e in node.value.elts]
            break
    assert events is not None, "EXPECTED_WEBHOOK_EVENTS not found in stripe-ops skill"
    assert set(events) == set(wh._HANDLERS), (
        "stripe-ops EXPECTED_WEBHOOK_EVENTS drifted from webhook_service._HANDLERS"
    )
