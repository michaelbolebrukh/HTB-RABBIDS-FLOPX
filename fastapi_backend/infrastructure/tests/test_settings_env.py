import pytest
from pydantic import ValidationError

from infrastructure.settings import Settings


# Minimal required fields so Settings() constructs; each case overrides ENV.
#
# _env_file=None is what makes these cases hermetic. Settings declares
# env_file=".env" (model_config), and pydantic-settings reads that FILE as its own
# source, independently of os.environ - so monkeypatch cannot reach it. Any machine
# that has run scripts/dev-setup.sh has a fastapi_backend/.env pinning
# ENV=development, which silently outranked the code default and made this module
# assert the developer's local config instead of the behaviour it exists to pin.
# The rootdir conftest already isolates the suite from os.environ; this is the same
# intent applied to the other source Settings reads.
_BASE = dict(HASH_KEY="x" * 32, APP_DOMAIN="example.com",
             DATABASE_URL="sqlite:///./test.db", EMAIL_PROVIDER="SENDGRID",
             _env_file=None)


def _env(value):
    return Settings(**_BASE, ENV=value).ENV


@pytest.mark.parametrize("given,expected", [
    ("production", "production"),
    ("development", "development"),
    ("Production", "production"),      # case-insensitive
    (" Development ", "development"),  # trimmed
])
def test_valid_env_values_normalized(given, expected):
    assert _env(given) == expected


def test_unset_env_defaults_to_production(monkeypatch):
    # Isolating from the ambient environment on purpose: this asserts the CODE
    # default, and the test bootstrap sets ENV=development for the suite, so
    # reading it from the environment would assert the harness instead.
    monkeypatch.delenv("ENV", raising=False)
    assert Settings(**_BASE).ENV == "production"


@pytest.mark.parametrize("bad", ["prod", "PROD", "staging", "prd", "test", ""])
def test_invalid_env_values_rejected(bad):
    # A near-miss like 'prod' must fail fast rather than silently behave as
    # production and mint a stray error-tracking environment tag:
    with pytest.raises(ValidationError):
        _env(bad)


# --- INBOX_IMAP_EMAIL derivation -------------------------------------------
# The admin inbox reads the instance business mailbox, so the address is
# redundant with CONTACT_EMAIL: it is derived from it when a mailbox password
# is set, leaving the FE-minted password the only per-instance inbox value.


def test_inbox_email_derived_from_contact_when_password_set():
    s = Settings(**_BASE, INBOX_IMAP_PASSWORD="mailbox-secret")
    # CONTACT_EMAIL defaults to hello@<domain>:
    assert s.CONTACT_EMAIL == "hello@example.com"
    assert s.INBOX_IMAP_EMAIL == "hello@example.com"


def test_inbox_email_follows_explicit_contact_email():
    s = Settings(**_BASE, CONTACT_EMAIL="Team@Example.com", INBOX_IMAP_PASSWORD="p")
    assert s.INBOX_IMAP_EMAIL == "team@example.com"


def test_explicit_inbox_email_wins_over_derivation():
    s = Settings(**_BASE, INBOX_IMAP_EMAIL="OPS@other.io", INBOX_IMAP_PASSWORD="p")
    assert s.INBOX_IMAP_EMAIL == "ops@other.io"


def test_inbox_email_not_derived_without_password():
    # No password means the inbox is dormant regardless, so no address is minted:
    assert Settings(**_BASE).INBOX_IMAP_EMAIL is None


# Rename policy: None (empty env value) = unlimited self-serve renames, 0 =
# renames only via support, N > 0 = once per N days:
@pytest.mark.parametrize("given,expected", [
    ("", None),
    ("  ", None),
    ("0", 0),
    ("14", 14),
])
def test_name_change_interval_parsing(given, expected):
    assert Settings(**_BASE, NAME_CHANGE_INTERVAL_DAYS=given).NAME_CHANGE_INTERVAL_DAYS == expected


def test_name_change_interval_defaults_to_monthly():
    assert Settings(**_BASE).NAME_CHANGE_INTERVAL_DAYS == 30
