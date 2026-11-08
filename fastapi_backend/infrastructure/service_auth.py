"""Service auth — a scoped, revocable machine credential for agent/ops actions,
distinct from a human session and from HASH_KEY.

A stateless bearer (`Authorization: Bearer <token>`) checked against `SERVICE_TOKENS`
(a comma-separated list, so one agent's token can be revoked without rotating the
rest). A match resolves to the persisted **service-account `User` row** (seeded by
`infrastructure/seeder.py` as `service@<APP_DOMAIN>`), which passes only the
capability grants it actually holds — the same rule as any account, applied by
`require_capability` in `infrastructure/dependencies.py`:

- **Auditable** — every service action is logged by the token's fingerprint (a hash
  prefix), never the token itself, so which credential acted stays visible even
  though actions attribute to the account's user id.
- **Revocable** — drop a token from the list; user sessions and unsubscribe signing
  are untouched.
- **Stateless** — no per-request state, so any number of agents use it concurrently
  with no races (unlike an email-code login).
- **Contained** — deliberately NOT signed with HASH_KEY: a leak here exposes only
  what the account's grants reach, never auth-token forgery or unsubscribe/
  suppression signing.

Destructive operations still carry their own confirm gates on top of this — the
credential grants reach, not a bypass of per-action safety.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Optional, TYPE_CHECKING

from sqlalchemy.orm import Session, selectinload

from infrastructure.constant_time import constant_time_equals
from infrastructure.settings import settings as s

if TYPE_CHECKING:
    from platform.models import User


logger = logging.getLogger(__name__)

# A configured token shorter than this is ignored as too weak to be real (guards
# against an accidental placeholder silently becoming a live credential):
_MIN_TOKEN_LEN = 16


def configured_tokens() -> list[str]:
    raw = s.SERVICE_TOKENS or ""
    return [t.strip() for t in raw.split(",") if len(t.strip()) >= _MIN_TOKEN_LEN]


def _fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()[:12]


def match_token(presented: Optional[str]) -> Optional[str]:
    """Returns the matched token's fingerprint iff `presented` equals a configured token.

    Compares against every token in constant time and does not early-exit, so the
    response time never leaks which token matched (or how far a guess got). The
    fingerprint (sha256(token)[:12]) is an attribution handle, never the secret."""
    if not presented:
        return None
    fingerprint: Optional[str] = None
    for token in configured_tokens():
        if constant_time_equals(presented, token):
            fingerprint = _fingerprint(token)
    return fingerprint


def service_account_email() -> str:
    """The reserved address of the shared service account, derived from APP_DOMAIN.

    Under org control by construction (the instance's own domain), so the
    email-code flow can never become a third-party account-takeover vector."""
    return f"service@{s.APP_DOMAIN}".strip().lower()


def get_service_account(db: Session) -> Optional["User"]:
    """Loads the persisted service-account User row a matched token resolves to.

    Failing closed when the row is missing (SERVICE_TOKENS set but the seeder
    has not run yet): logging loudly and returning None — never auto-creating
    an account at request time."""
    # Importing locally to keep this module free of model imports at load time:
    from platform.models import User, OrganisationMember

    # Eager-loading memberships + their organisations for the same reason the
    # human auth path does: the capability check resolves conferred grants, and a
    # lazy load here would be one query per membership on every service call:
    user = (
        db.query(User)
        .options(
            selectinload(User.organisation_memberships)
            .selectinload(OrganisationMember.organisation)
        )
        .filter(User.email == service_account_email())
        .first()
    )
    if user is None:
        logger.error(
            "Service token matched but the service account '%s' is not seeded; "
            "failing closed (a restart runs the seeder).",
            service_account_email(),
        )
    return user


def bearer_from_authorization(header: Optional[str]) -> Optional[str]:
    """Extracts the token from an `Authorization: Bearer <token>` header."""
    if not header:
        return None
    parts = header.split(" ", 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip() or None
    return None
