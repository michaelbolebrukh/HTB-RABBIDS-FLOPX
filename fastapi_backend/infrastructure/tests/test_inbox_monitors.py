"""Reliability monitors: boot probe, full-loop canary, and error escalation."""

import pytest

import platform.services.inbox_service.forwardemail as fe_mod
import platform.services.inbox_service.monitors as monitors
from infrastructure.settings import settings as s
from platform.exceptions import InboxUnavailableException
from platform.services.inbox_service.forwardemail import ForwardEmailMailbox
from platform.services.inbox_service.ports import (
    MailAddress,
    MessagePage,
    MessageSummary,
)


@pytest.fixture(autouse=True)
def _fast_polling(monkeypatch):
    monkeypatch.setattr(monitors, "_ARRIVAL_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(monitors, "_ARRIVAL_POLL_SECONDS", 0.01)
    monkeypatch.setattr(s, "INBOX_IMAP_EMAIL", "hello@instance.io")
    monkeypatch.setattr(s, "INBOX_IMAP_PASSWORD", "mailbox-pass")


def _summary(marker):
    return MessageSummary(id="m1", folder="INBOX", subject=f"Inbox canary {marker}",
                          sender=MailAddress(email="hello@instance.io"), date=None,
                          is_unread=True, is_flagged=False, has_attachments=False, snippet="")


class StubMailbox:
    def __init__(self, arrives=True, folders_raise=False):
        self.arrives = arrives
        self.folders_raise = folders_raise
        self.deleted = []

    def list_folders(self):
        if self.folders_raise:
            raise InboxUnavailableException
        return ["INBOX"]

    def list_messages(self, folder, page, search):
        # Models "the heartbeat with this unique marker arrived". The generic
        # prefix sweep at the start of a run finds no pre-existing stragglers in
        # this stub (that path is covered by test_canary_sweep_deletes_stragglers):
        if self.arrives and search and search != monitors._CANARY_SUBJECT_PREFIX:
            return MessagePage(messages=[_summary(search)], page=1, page_count=1)
        return MessagePage(messages=[], page=1, page_count=1)

    def delete(self, message_id):
        self.deleted.append(message_id)


class StubSender:
    def __init__(self, fails=False):
        self.fails = fails
        self.sent = []

    def send(self, message, sender):
        if self.fails:
            raise InboxUnavailableException
        self.sent.append(message)


def test_credential_rejection_logs_error(monkeypatch, caplog):
    class Resp:
        status_code = 401
        headers = {}

    monkeypatch.setattr(fe_mod.httpx, "request", lambda *a, **k: Resp())
    with caplog.at_level("ERROR"):
        with pytest.raises(InboxUnavailableException):
            ForwardEmailMailbox().list_folders()
    assert any("rejected the mailbox credential" in r.message for r in caplog.records)


def test_boot_probe_ok_and_failing(monkeypatch, caplog):
    monkeypatch.setattr(monitors.svc, "get_mailbox", lambda: StubMailbox())
    with caplog.at_level("INFO"):
        assert monitors.run_boot_probe() is True
    assert any("boot probe OK" in r.message for r in caplog.records)

    caplog.clear()
    monkeypatch.setattr(monitors.svc, "get_mailbox", lambda: StubMailbox(folders_raise=True))
    with caplog.at_level("ERROR"):
        assert monitors.run_boot_probe() is False
    assert any("boot probe FAILED" in r.message for r in caplog.records)


def test_canary_full_loop_success_cleans_up(monkeypatch, caplog):
    mailbox, sender = StubMailbox(arrives=True), StubSender()
    monkeypatch.setattr(monitors.svc, "get_mailbox", lambda: mailbox)
    monkeypatch.setattr(monitors.svc, "get_sender", lambda: sender)

    with caplog.at_level("INFO"):
        assert monitors.run_canary_once() is True

    # Heartbeat addressed to the mailbox itself; arrival verified; deleted:
    assert sender.sent[0].to[0].email == "hello@instance.io"
    assert mailbox.deleted == ["m1"]
    assert any("canary OK" in r.message for r in caplog.records)


def test_canary_sweep_deletes_stragglers(monkeypatch):
    def _msg(mid, subject, sender="hello@instance.io"):
        return MessageSummary(id=mid, folder="INBOX", subject=subject,
                              sender=MailAddress(email=sender), date=None, is_unread=True,
                              is_flagged=False, has_attachments=False, snippet="")

    class SweepMailbox:
        def __init__(self):
            self.store = {
                "a": _msg("a", "Inbox canary canary-aaaaaaaaaaaa"),
                "b": _msg("b", "Inbox canary canary-bbbbbbbbbbbb"),
                "keep": _msg("keep", "Quarterly report", "customer@example.com"),
            }

        def list_messages(self, folder, page, search):
            msgs = [m for m in self.store.values() if search and search in m.subject]
            return MessagePage(messages=msgs, page=1, page_count=1)

        def delete(self, message_id):
            self.store.pop(message_id, None)

    mailbox = SweepMailbox()
    monkeypatch.setattr(monitors.svc, "get_mailbox", lambda: mailbox)

    # Both canary heartbeats are removed; the real customer email survives:
    assert monitors.sweep_canaries() == 2
    assert set(mailbox.store) == {"keep"}


def test_canary_send_leg_failure_alarms(monkeypatch, caplog):
    monkeypatch.setattr(monitors.svc, "get_mailbox", lambda: StubMailbox())
    monkeypatch.setattr(monitors.svc, "get_sender", lambda: StubSender(fails=True))
    with caplog.at_level("ERROR"):
        assert monitors.run_canary_once() is False
    assert any("send leg" in r.message for r in caplog.records)


def test_canary_non_arrival_alarms(monkeypatch, caplog):
    monkeypatch.setattr(monitors.svc, "get_mailbox", lambda: StubMailbox(arrives=False))
    monkeypatch.setattr(monitors.svc, "get_sender", lambda: StubSender())
    with caplog.at_level("ERROR"):
        assert monitors.run_canary_once() is False
    assert any("did not arrive" in r.message for r in caplog.records)
