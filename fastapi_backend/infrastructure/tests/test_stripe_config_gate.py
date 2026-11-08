"""Stripe fail-closed gate: partial config = fully dormant + loud startup error."""

import logging

import pytest

from infrastructure.settings import settings as s
from platform.exceptions import PaymentsNotConfiguredException
from platform.services.payment_services import stripe_gateway


def _set_keys(monkeypatch, secret, webhook):
    monkeypatch.setattr(s, "STRIPE_SECRET_KEY", secret)
    monkeypatch.setattr(s, "STRIPE_WEBHOOK_SECRET", webhook)


def test_partial_config_is_not_configured(monkeypatch):
    # Only the API key: checkout could take money fulfillment can't record,
    # so the whole feature must read as unconfigured:
    _set_keys(monkeypatch, "sk_test_x", None)
    assert stripe_gateway.stripe_is_configured() is False
    with pytest.raises(PaymentsNotConfiguredException):
        stripe_gateway.get_stripe()

    # Only the webhook secret is equally dormant:
    _set_keys(monkeypatch, None, "whsec_x")
    assert stripe_gateway.stripe_is_configured() is False
    with pytest.raises(PaymentsNotConfiguredException):
        stripe_gateway.get_stripe()


def test_full_config_is_configured(monkeypatch):
    _set_keys(monkeypatch, "sk_test_x", "whsec_x")
    assert stripe_gateway.stripe_is_configured() is True
    assert stripe_gateway.get_stripe() is not None


def test_provisioning_accessor_needs_only_the_api_key(monkeypatch):
    # The bootstrap creates the webhook endpoint, so it runs pre-webhook-secret:
    _set_keys(monkeypatch, "sk_test_x", None)
    assert stripe_gateway.get_stripe_for_provisioning() is not None

    _set_keys(monkeypatch, None, "whsec_x")
    with pytest.raises(PaymentsNotConfiguredException):
        stripe_gateway.get_stripe_for_provisioning()


def test_validate_alerts_on_partial_config_only(monkeypatch, caplog):
    with caplog.at_level(logging.ERROR):
        _set_keys(monkeypatch, "sk_test_x", None)
        stripe_gateway.validate_stripe_config()
    assert any("PARTIALLY configured" in r.message and "STRIPE_WEBHOOK_SECRET" in r.message for r in caplog.records)

    caplog.clear()
    with caplog.at_level(logging.ERROR):
        _set_keys(monkeypatch, None, None)
        stripe_gateway.validate_stripe_config()
        _set_keys(monkeypatch, "sk_test_x", "whsec_x")
        stripe_gateway.validate_stripe_config()
    assert not caplog.records
