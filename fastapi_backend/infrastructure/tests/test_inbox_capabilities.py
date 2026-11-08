"""Inbox gating: per-account capability grants, and config gates."""

import pytest

from platform.models import User
from platform.enums import Role
from platform.exceptions import CapabilityException, InboxNotConfiguredException
from infrastructure.dependencies import verify_capability
from infrastructure.settings import settings as s
from platform.services import inbox_service


def test_admin_holds_all_capabilities():
    verify_capability(User(role=Role.ADMIN, capabilities=None), "inbox:read")
    verify_capability(User(role=Role.ADMIN, capabilities=[]), "anything:at-all")


def test_plain_account_needs_the_explicit_grant():
    granted = User(role=Role.USER, capabilities=["inbox:read"])
    verify_capability(granted, "inbox:read")

    with pytest.raises(CapabilityException):
        verify_capability(granted, "inbox:write")
    with pytest.raises(CapabilityException):
        verify_capability(User(role=Role.USER, capabilities=None), "inbox:read")


def test_inbox_unconfigured_by_default(monkeypatch):
    for key in ("INBOX_IMAP_EMAIL", "INBOX_IMAP_PASSWORD"):
        monkeypatch.setattr(s, key, None)

    assert inbox_service.inbox_is_configured() is False
    assert inbox_service.inbox_can_send() is False
    with pytest.raises(InboxNotConfiguredException):
        inbox_service.list_folders()


def test_mailbox_credential_enables_full_stack_send(monkeypatch):
    # Forward Email sends on the mailbox credential, so the mailbox pair alone
    # configures both reading AND sending - no separate send config, no Brevo:
    monkeypatch.setattr(s, "INBOX_IMAP_EMAIL", "hello@instance.io")
    monkeypatch.setattr(s, "INBOX_IMAP_PASSWORD", "mailbox-pass")
    monkeypatch.setattr(s, "BREVO_API_KEY", None)

    assert inbox_service.inbox_is_configured() is True
    assert inbox_service.inbox_can_send() is True


def test_configured_inbox_stays_quiet_at_startup(monkeypatch, caplog):
    monkeypatch.setattr(s, "INBOX_IMAP_EMAIL", "hello@instance.io")
    monkeypatch.setattr(s, "INBOX_IMAP_PASSWORD", "mailbox-pass")
    monkeypatch.setattr(s, "BREVO_API_KEY", None)

    with caplog.at_level("ERROR"):
        inbox_service.validate_inbox_config()

    assert not caplog.records
