"""Web-push engine — the reusable notification channel.

Generalized from the inbox-only origin: the same VAPID + subscription + delivery
machinery now serves both operational inbox pings and user-facing broadcasts. The
wire payload supports the full Web Notification API so a broadcast can use every
capability a PWA notification has (action buttons, big image, deep-link, etc.);
the service worker (`next-pwa/app/sw.ts`) mirrors this shape.

Config-minimalism: the VAPID keypair is generated once at first use and stored in
platform_keys — no per-instance env var. Each browser is one push_subscriptions
row; a gone endpoint (404/410) is pruned on delivery. Payloads never carry private
message bodies.
"""

from __future__ import annotations

import enum
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from cryptography.hazmat.primitives import serialization
from py_vapid import Vapid
from py_vapid.utils import b64urlencode
from pywebpush import WebPushException, webpush
from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session, joinedload, selectinload

from infrastructure.settings import settings as s
from platform.capabilities import INBOX_READ
from platform.enums import Role
from platform.models import Organisation, OrganisationMember, PlatformKey, PushSubscription, User


logger = logging.getLogger(__name__)

_VAPID_KEY = "vapid_private_pem"
_TTL_SECONDS = 3600


class Audience(str, enum.Enum):
    """Who a notification reaches. Inbox pings stay restricted to accounts that
    can read the inbox now that end users can also subscribe; broadcasts reach
    every opted-in device."""
    INBOX_READERS = "inbox_readers"
    ALL_USERS = "all_users"


@dataclass
class PushNotification:
    """The full Web Notification API surface. `to_payload()` is the wire shape the
    service worker consumes; unset fields are omitted so the SW applies its defaults.

    `url` is the deep link opened on click; `actions` are buttons, each with its
    own optional `url` (carried in `data.actions` and resolved on notificationclick).
    """
    title: str
    body: str = ""
    url: str = "/"
    icon: Optional[str] = None
    badge: Optional[str] = None
    image: Optional[str] = None
    tag: Optional[str] = None
    renotify: bool = False
    require_interaction: bool = False
    silent: bool = False
    vibrate: Optional[list[int]] = None
    timestamp: Optional[int] = None
    lang: Optional[str] = None
    dir: Optional[str] = None
    actions: list[dict[str, str]] = field(default_factory=list)

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"title": self.title, "body": self.body, "url": self.url}
        for key, value in (
            ("icon", self.icon), ("badge", self.badge), ("image", self.image),
            ("tag", self.tag), ("lang", self.lang), ("dir", self.dir),
            ("timestamp", self.timestamp), ("vibrate", self.vibrate),
        ):
            if value is not None:
                payload[key] = value
        if self.renotify:
            payload["renotify"] = True
        if self.require_interaction:
            payload["requireInteraction"] = True
        if self.silent:
            payload["silent"] = True
        if self.actions:
            # Notification `actions` take {action,title,icon}; a per-action deep
            # link rides in `data.actions` since the API drops unknown keys:
            payload["actions"] = [
                {k: a[k] for k in ("action", "title", "icon") if a.get(k)}
                for a in self.actions
            ]
            action_urls = {a["action"]: a["url"] for a in self.actions if a.get("action") and a.get("url")}
            if action_urls:
                payload["actionUrls"] = action_urls
        return payload


# -- VAPID keypair (self-provisioning) -----------------------------------------

def _generate_private_pem() -> str:
    vapid = Vapid()
    vapid.generate_keys()
    return vapid.private_pem().decode()


def get_vapid_keys(db: Session) -> tuple[str, str]:
    """Returns (private_pem, public application server key), creating once.

    A concurrent first boot across workers is settled by the primary key: the
    loser's insert fails and it re-reads the winner's row."""
    row = db.query(PlatformKey).filter(PlatformKey.key == _VAPID_KEY).first()
    if row is None:
        row = PlatformKey(key=_VAPID_KEY, value=_generate_private_pem())
        db.add(row)
        try:
            db.commit()
        except Exception:
            db.rollback()
            row = db.query(PlatformKey).filter(PlatformKey.key == _VAPID_KEY).first()
            if row is None:
                raise

    vapid = Vapid.from_pem(row.value.encode())
    public = b64urlencode(vapid.public_key.public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint,
    ))
    return row.value, public


def _raw_private_key(private_pem: str) -> str:
    """pywebpush wants the raw base64url EC private value, not the PEM."""
    vapid = Vapid.from_pem(private_pem.encode())
    value = vapid.private_key.private_numbers().private_value
    return b64urlencode(value.to_bytes(32, "big"))


# -- subscriptions -------------------------------------------------------------

def subscribe(db: Session, user_id: int, endpoint: str, p256dh: str, auth: str) -> None:
    """Upserts one browser's subscription (endpoint is the unique handle)."""
    row = db.query(PushSubscription).filter(PushSubscription.endpoint == endpoint).first()
    if row is None:
        row = PushSubscription(user_id=user_id, endpoint=endpoint, p256dh=p256dh, auth=auth)
        db.add(row)
    else:
        row.user_id = user_id
        row.p256dh = p256dh
        row.auth = auth
    try:
        db.commit()
    except Exception:
        db.rollback()
        raise
    logger.info("Push subscription registered (user %s).", user_id)


def unsubscribe(db: Session, endpoint: str) -> None:
    db.query(PushSubscription).filter(PushSubscription.endpoint == endpoint).delete()
    try:
        db.commit()
    except Exception:
        db.rollback()
        raise


def _subscriptions_for(db: Session, audience: Audience) -> list[PushSubscription]:
    q = db.query(PushSubscription)
    if audience == Audience.INBOX_READERS:
        # Inbox pings must not reach the end users who opted into announcements.
        # SQL narrows to candidates — admins (implicit holders of every
        # capability), accounts carrying explicit grants, or members of ANY
        # organisation — then Python applies has_capability(INBOX_READ), since
        # JSON containment is not dialect-portable and the candidate set is
        # tiny (single digits). The membership arm is load-bearing: conferred
        # grants live on the organisation, not on users.capabilities, so
        # without it a member whose inbox:read is org-conferred would silently
        # stop receiving inbox pings. The Python filter after it is unchanged —
        # has_capability already resolves the effective set:
        candidates = (
            q.join(User, User.id == PushSubscription.user_id)
            .options(
                joinedload(PushSubscription.user)
                .selectinload(User.organisation_memberships)
                .selectinload(OrganisationMember.organisation)
            )
            .filter(
                or_(
                    User.role == Role.ADMIN,
                    User.capabilities.isnot(None),
                    # Only orgs that are ACTIVE and actually confer something can
                    # make a member an inbox reader; an unqualified any() would
                    # drag in every member of every organisation:
                    User.organisation_memberships.any(
                        OrganisationMember.organisation.has(
                            and_(
                                Organisation.is_active.is_(True),
                                Organisation.granted_capabilities.isnot(None),
                            )
                        )
                    ),
                )
            )
            .all()
        )
        return [sub for sub in candidates if sub.user.has_capability(INBOX_READ)]
    return q.all()


def _subscriptions_for_emails(db: Session, emails: list[str]) -> list[PushSubscription]:
    """Every device belonging to the given users, by email (case-insensitive).

    The universal targeting escape hatch: any cohort too bespoke for a built-in
    filter (e.g. 'users above 500 XP who stalled last week') is computed upstream
    into an explicit email list and passed here."""
    normalized = {e.strip().lower() for e in emails if e and e.strip()}
    if not normalized:
        return []
    return (
        db.query(PushSubscription)
        .join(User, User.id == PushSubscription.user_id)
        .filter(func.lower(User.email).in_(normalized))
        .all()
    )


# -- delivery ------------------------------------------------------------------

def _deliver(db: Session, subscriptions: list[PushSubscription], payload_json: str) -> int:
    """Pushes one payload to each subscription; returns the delivered count.

    Fail-open per device: an unreachable push service only logs, and a gone
    endpoint (404/410) deletes its row so the pool stays clean."""
    if not subscriptions:
        return 0
    private_pem, _public = get_vapid_keys(db)
    raw_key = _raw_private_key(private_pem)
    claims_sub = f"mailto:{s.CONTACT_EMAIL or s.INBOX_IMAP_EMAIL or 'admin@localhost'}"

    sent, dead_ids = 0, []
    for sub in subscriptions:
        try:
            webpush(
                subscription_info={
                    "endpoint": sub.endpoint,
                    "keys": {"p256dh": sub.p256dh, "auth": sub.auth},
                },
                data=payload_json,
                vapid_private_key=raw_key,
                # pywebpush mutates claims per delivery, so a fresh dict each time:
                vapid_claims={"sub": claims_sub},
                ttl=_TTL_SECONDS,
            )
            sent += 1
        except WebPushException as error:
            status = getattr(getattr(error, "response", None), "status_code", None)
            if status in (404, 410):
                dead_ids.append(sub.id)
            else:
                logger.warning("Push delivery failed (status %s).", status)
        except Exception:
            logger.warning("Push delivery failed unexpectedly.", exc_info=True)

    if dead_ids:
        db.query(PushSubscription).filter(PushSubscription.id.in_(dead_ids)).delete(
            synchronize_session=False,
        )
        try:
            db.commit()
        except Exception:
            db.rollback()
        logger.info("Pruned %s dead push subscriptions.", len(dead_ids))
    return sent


def send(db: Session, notification: PushNotification, *, audience: Audience) -> int:
    """Delivers a full-featured notification to an audience; returns sent count."""
    return _deliver(db, _subscriptions_for(db, audience), json.dumps(notification.to_payload()))


def send_broadcast(db: Session, notification: PushNotification,
                   *, emails: list[str] | None = None) -> int:
    """Fan out an announcement and return the delivered device count.

    The code/ops entry point behind `POST /notifications/broadcast`: build a
    PushNotification with whatever capabilities the announcement needs and call
    this. `emails` targets only those users' devices (the flexible, AI-computed
    cohort path); omit it to reach every opted-in device."""
    if emails is not None:
        subs = _subscriptions_for_emails(db, emails)
        count = _deliver(db, subs, json.dumps(notification.to_payload()))
        logger.info("Targeted broadcast to %s of %s requested address(es): %r",
                    count, len({e.strip().lower() for e in emails if e.strip()}),
                    notification.title[:80])
        return count
    count = send(db, notification, audience=Audience.ALL_USERS)
    logger.info("Broadcast push sent to %s device(s): %r", count, notification.title[:80])
    return count


def notify_inbox_readers(db: Session, title: str, body: str = "", url: str = "/") -> int:
    """Operational ping to inbox-reader devices only (new mail, scheduled failure)."""
    return send(db, PushNotification(title=title, body=body, url=url, tag="ops"),
                audience=Audience.INBOX_READERS)
