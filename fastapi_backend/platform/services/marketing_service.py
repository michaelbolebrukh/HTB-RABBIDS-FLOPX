"""Marketing-consent + suppression via signed, auth-free unsubscribe tokens.

Unsubscribing from marketing email must work without a login (PECR/GDPR,
CAN-SPAM, and Gmail/Yahoo bulk-sender rules all require it), so authorization
is carried by an HMAC token bound to one recipient and one purpose. Worst case a
leaked link lets someone flip that single recipient's marketing state - nothing
else. Tokens deliberately never expire: links in old emails must keep working.

Two token flavours share one verify path:
  * user token  (`v1.<user_id>.<sig>`)   - for a registered user, minted by the app.
  * email token (`e1.<b64url-email>.<sig>`) - for ANY address, so bulk/announcement
    mail sent to non-users (e.g. via the mailbox-ops skill) still carries a
    working unsubscribe. The token algorithm is intentionally simple and derived
    only from HASH_KEY, so an ops tool can mint the identical token client-side
    (see .cursor/skills/mailbox-ops/tools/compliance.py) - no shared endpoint call.

Suppression is address-keyed (`marketing_suppressions`), so it covers non-users;
for a registered user it is mirrored to `User.marketing_consent` and back, keeping
the app's existing consent reads correct while this table stays the address-level
source of truth any sender consults.

Everything derives from settings every instance already has (HASH_KEY,
APP_DOMAIN), so a fresh startup clone needs no extra configuration.
"""

import base64
import hashlib
import hmac
from typing import Optional
from urllib.parse import quote

from sqlalchemy import func
from sqlalchemy.orm import Session

from platform.models import MarketingSuppression, User
from platform.exceptions import InvalidUnsubscribeTokenException
from infrastructure.constant_time import constant_time_equals
from infrastructure.settings import settings as s


_TOKEN_VERSION = "v1"
_TOKEN_PURPOSE = "marketing-unsub"

_EMAIL_TOKEN_VERSION = "e1"
_EMAIL_TOKEN_PURPOSE = "marketing-unsub-email"

# Frontend page path that hosts the human confirm step (platform route):
UNSUBSCRIBE_PAGE_PATH = "/unsubscribe"


def _normalize(email: str) -> str:
    return (email or "").strip().lower()


# -- Token minting / verification ----------------------------------------------

def _signature(user_id: int) -> str:
    message = f"{_TOKEN_PURPOSE}:{_TOKEN_VERSION}:{user_id}".encode()
    return hmac.new(s.HASH_KEY.encode(), message, hashlib.sha256).hexdigest()[:32]


def mint_unsubscribe_token(user_id: int) -> str:
    return f"{_TOKEN_VERSION}.{user_id}.{_signature(user_id)}"


def _email_signature(email: str) -> str:
    message = f"{_EMAIL_TOKEN_PURPOSE}:{_EMAIL_TOKEN_VERSION}:{_normalize(email)}".encode()
    return hmac.new(s.HASH_KEY.encode(), message, hashlib.sha256).hexdigest()[:32]


def _b64url(raw: str) -> str:
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def _b64url_decode(token_part: str) -> Optional[str]:
    try:
        padded = token_part + "=" * (-len(token_part) % 4)
        return base64.urlsafe_b64decode(padded.encode()).decode()
    except Exception:
        return None


def mint_email_unsubscribe_token(email: str) -> str:
    email = _normalize(email)
    return f"{_EMAIL_TOKEN_VERSION}.{_b64url(email)}.{_email_signature(email)}"


def _verify_user_token(token: str) -> Optional[int]:
    parts = (token or "").split(".")
    if len(parts) != 3 or parts[0] != _TOKEN_VERSION:
        return None
    try:
        user_id = int(parts[1])
    except ValueError:
        return None
    if not constant_time_equals(parts[2], _signature(user_id)):
        return None
    return user_id


def _verify_email_token(token: str) -> Optional[str]:
    parts = (token or "").split(".")
    if len(parts) != 3 or parts[0] != _EMAIL_TOKEN_VERSION:
        return None
    email = _b64url_decode(parts[1])
    if email is None:
        return None
    email = _normalize(email)
    if not constant_time_equals(parts[2], _email_signature(email)):
        return None
    return email


def _email_for_token(db: Session, token: str) -> Optional[str]:
    """Resolves either token flavour to the target address (user token -> that
    user's email; email token -> the address it carries)."""
    user_id = _verify_user_token(token)
    if user_id is not None:
        user = db.query(User).filter(User.id == user_id).first()
        return _normalize(user.email) if user is not None else None
    return _verify_email_token(token)


# -- Suppression store ---------------------------------------------------------

def is_suppressed(db: Session, email: str) -> bool:
    email = _normalize(email)
    return (
        db.query(MarketingSuppression.id)
        .filter(MarketingSuppression.email == email)
        .first()
        is not None
    )


def filter_suppressed(db: Session, emails: list[str]) -> tuple[list[str], list[str]]:
    """Splits addresses into (allowed, suppressed) in one query - the sender
    passes the whole recipient batch and never re-mails a suppressed address."""
    normalized = {_normalize(e) for e in emails if _normalize(e)}
    if not normalized:
        return [], []
    suppressed = {
        row.email
        for row in db.query(MarketingSuppression.email)
        .filter(MarketingSuppression.email.in_(normalized))
        .all()
    }
    allowed = [e for e in normalized if e not in suppressed]
    return sorted(allowed), sorted(suppressed)


def suppress_email(db: Session, email: str, reason: str = "unsubscribe") -> None:
    """Adds an address to the do-not-send list and mirrors it down to a
    registered user's stored consent so the app's existing reads stay correct."""
    email = _normalize(email)
    if not email:
        return
    existing = (
        db.query(MarketingSuppression).filter(MarketingSuppression.email == email).first()
    )
    if existing is None:
        db.add(MarketingSuppression(email=email, reason=reason))
    user = db.query(User).filter(func.lower(User.email) == email).first()
    if user is not None:
        user.marketing_consent = False
    db.commit()


def unsuppress_email(db: Session, email: str) -> None:
    """Removes an address from the do-not-send list (resubscribe) and restores a
    registered user's stored consent."""
    email = _normalize(email)
    if not email:
        return
    db.query(MarketingSuppression).filter(MarketingSuppression.email == email).delete()
    user = db.query(User).filter(func.lower(User.email) == email).first()
    if user is not None:
        user.marketing_consent = True
    db.commit()


def list_suppressions(db: Session, *, limit: int = 1000, offset: int = 0) -> list[str]:
    rows = (
        db.query(MarketingSuppression.email)
        .order_by(MarketingSuppression.email)
        .offset(offset)
        .limit(limit)
        .all()
    )
    return [r.email for r in rows]


# -- Token-driven state changes (used by the public unsubscribe endpoints) -----

def apply_unsubscribe_token(db: Session, token: str, *, suppressed: bool) -> str:
    """Flips suppression for whichever recipient a valid token points at, and
    returns the masked address for the confirmation response.

    Invalid tokens raise the same exception regardless, so responses never reveal
    whether an account or address exists.
    """
    email = _email_for_token(db, token)
    if email is None:
        raise InvalidUnsubscribeTokenException()
    if suppressed:
        suppress_email(db, email, reason="unsubscribe")
    else:
        unsuppress_email(db, email)
    return masked_email(email)


def masked_email(email: str) -> str:
    """Reassures the clicker which account was affected without exposing it."""
    local, _, domain = email.partition("@")
    safe_local = (local[0] + "***") if local else "***"
    return f"{safe_local}@{domain}" if domain else safe_local


# -- URL builders (email footers / RFC 8058 headers) ---------------------------

def build_unsubscribe_page_url(user_id: int) -> str:
    """Human-facing link for email footers - the frontend confirm page."""
    token = quote(mint_unsubscribe_token(user_id))
    return f"https://{s.APP_DOMAIN}{UNSUBSCRIBE_PAGE_PATH}?token={token}"


def build_one_click_unsubscribe_url(user_id: int) -> Optional[str]:
    """RFC 8058 one-click target (mail clients POST here directly).

    Needs the backend's public origin; when that isn't configured the header
    is simply omitted and the footer page link remains the unsubscribe path.
    """
    if not s.PUBLIC_BACKEND_ORIGIN:
        return None
    token = quote(mint_unsubscribe_token(user_id))
    origin = s.PUBLIC_BACKEND_ORIGIN.rstrip("/")
    return f"{origin}/marketing/unsubscribe/one-click?token={token}"


def build_email_unsubscribe_page_url(email: str) -> str:
    """Footer link for a bulk recipient identified by address (may be a non-user)."""
    token = quote(mint_email_unsubscribe_token(email))
    return f"https://{s.APP_DOMAIN}{UNSUBSCRIBE_PAGE_PATH}?token={token}"


def build_email_one_click_unsubscribe_url(email: str) -> Optional[str]:
    if not s.PUBLIC_BACKEND_ORIGIN:
        return None
    token = quote(mint_email_unsubscribe_token(email))
    origin = s.PUBLIC_BACKEND_ORIGIN.rstrip("/")
    return f"{origin}/marketing/unsubscribe/one-click?token={token}"
