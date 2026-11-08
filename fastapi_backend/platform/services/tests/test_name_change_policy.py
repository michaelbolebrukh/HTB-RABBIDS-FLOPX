"""Name-change allowance policy: N changes per rolling window.

compute_name_change_policy is pure over (settings, user window columns, now),
so these exercise it with a fake user and monkeypatched settings - no DB.
"""
from datetime import datetime, timedelta, UTC
from types import SimpleNamespace

import pytest

from infrastructure.settings import settings as s
from platform.services.user_service import compute_name_change_policy


NOW = datetime(2026, 7, 22, 12, 0, 0, tzinfo=UTC)


def _user(window_start=None, count=0):
    return SimpleNamespace(name_changed_at=window_start, name_change_count=count)


@pytest.fixture
def policy_settings(monkeypatch):
    def _set(allowance, interval):
        monkeypatch.setattr(s, "NAME_CHANGE_ALLOWANCE", allowance)
        monkeypatch.setattr(s, "NAME_CHANGE_INTERVAL_DAYS", interval)
    return _set


def test_unlimited_when_interval_none(policy_settings):
    policy_settings(1, None)
    p = compute_name_change_policy(_user(), NOW)
    assert p["mode"] == "unlimited"
    assert p["remaining"] is None and p["reset_at"] is None


def test_disabled_when_interval_zero(policy_settings):
    policy_settings(3, 0)
    p = compute_name_change_policy(_user(), NOW)
    assert p["mode"] == "disabled"
    assert p["remaining"] == 0 and p["reset_at"] is None


def test_fresh_window_reports_full_allowance(policy_settings):
    policy_settings(3, 30)
    p = compute_name_change_policy(_user(), NOW)
    assert p["mode"] == "limited"
    assert p["allowance"] == 3 and p["remaining"] == 3
    # A change now would open a window closing one interval later:
    assert p["reset_at"] == NOW + timedelta(days=30)


def test_partial_window_decrements_remaining(policy_settings):
    policy_settings(3, 30)
    started = NOW - timedelta(days=10)
    p = compute_name_change_policy(_user(window_start=started, count=2), NOW)
    assert p["remaining"] == 1
    assert p["reset_at"] == started + timedelta(days=30)


def test_exhausted_window_reports_zero(policy_settings):
    policy_settings(3, 30)
    started = NOW - timedelta(days=5)
    p = compute_name_change_policy(_user(window_start=started, count=3), NOW)
    assert p["remaining"] == 0
    assert p["reset_at"] == started + timedelta(days=30)


def test_lapsed_window_resets_to_full(policy_settings):
    policy_settings(3, 30)
    started = NOW - timedelta(days=40)  # past the window
    p = compute_name_change_policy(_user(window_start=started, count=3), NOW)
    assert p["remaining"] == 3
    assert p["reset_at"] == NOW + timedelta(days=30)
