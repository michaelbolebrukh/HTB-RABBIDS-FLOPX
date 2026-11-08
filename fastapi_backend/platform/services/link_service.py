"""Managed short-link registry: stable key → mutable destination.

Public scans hit resolve(); operators mint and retarget via create/update.
Destination validation is the open-redirect boundary — relative same-origin
paths and absolute http(s) only.
"""

from __future__ import annotations

import re
from typing import Optional
from urllib.parse import urlparse

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from platform.exceptions import (
    InvalidManagedLinkDestinationException,
    InvalidManagedLinkKeyException,
    ManagedLinkExistsException,
    ManagedLinkNotFoundException,
)
from platform.models import ManagedLink
from platform.schemas import ManagedLinkCreateRequest, ManagedLinkUpdateRequest


# Keys that would collide with static App Router siblings or confuse operators:
_RESERVED_KEYS = frozenset({
    "manage",
    "new",
    "create",
    "edit",
    "admin",
    "api",
    "settings",
    "list",
    "index",
})

_KEY_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")


def _normalize_key(raw: str) -> str:
    key = (raw or "").strip().lower()
    if not _KEY_RE.fullmatch(key) or key in _RESERVED_KEYS:
        raise InvalidManagedLinkKeyException()
    return key


def _normalize_label(raw: Optional[str]) -> Optional[str]:
    if raw is None:
        return None
    label = raw.strip()
    return label or None


def _normalize_destination(raw: str) -> str:
    dest = (raw or "").strip()
    if not dest:
        raise InvalidManagedLinkDestinationException()
    # Absolute and relative destinations share the same hygiene bar — whitespace
    # and C0 controls (including CRLF) never belong in a redirect target:
    if "\\" in dest or any(c.isspace() for c in dest) or any(ord(c) < 32 for c in dest):
        raise InvalidManagedLinkDestinationException()

    # Same-origin relative path: must start with a single slash, never // (protocol-relative):
    if dest.startswith("/"):
        if dest.startswith("//"):
            raise InvalidManagedLinkDestinationException()
        return dest

    # Bare host or host/path (no scheme) is accepted as https — operators should
    # not have to type the scheme for every external target:
    if "://" not in dest:
        host = dest.split("/", 1)[0]
        if "." not in host or host.startswith("."):
            raise InvalidManagedLinkDestinationException()
        dest = f"https://{dest}"

    parsed = urlparse(dest)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise InvalidManagedLinkDestinationException()
    if parsed.username or parsed.password:
        raise InvalidManagedLinkDestinationException()
    return dest


def list_links(db: Session, *, include_inactive: bool = True) -> list[ManagedLink]:
    q = db.query(ManagedLink)
    if not include_inactive:
        q = q.filter(ManagedLink.is_active.is_(True))
    return q.order_by(ManagedLink.created_at.desc()).all()


def get_link(db: Session, key: str) -> ManagedLink:
    normalized = _normalize_key(key)
    row = db.query(ManagedLink).filter(ManagedLink.key == normalized).first()
    if row is None:
        raise ManagedLinkNotFoundException()
    return row


def resolve_link(db: Session, key: str) -> ManagedLink:
    # Reserved / malformed keys look like missing links publicly (no enumeration):
    try:
        normalized = _normalize_key(key)
    except InvalidManagedLinkKeyException:
        raise ManagedLinkNotFoundException() from None
    row = db.query(ManagedLink).filter(ManagedLink.key == normalized).first()
    # Inactive keys look like missing publicly (no existence oracle via 410):
    if row is None or not row.is_active:
        raise ManagedLinkNotFoundException()
    # Fail closed if a stored destination no longer passes the allowlist
    # (e.g. poisoned row) — never hand an open-redirect shape to clients:
    try:
        row.destination = _normalize_destination(row.destination)
    except InvalidManagedLinkDestinationException:
        raise ManagedLinkNotFoundException() from None
    return row


def create_link(db: Session, data: ManagedLinkCreateRequest) -> ManagedLink:
    key = _normalize_key(data.key)
    destination = _normalize_destination(data.destination)
    label = _normalize_label(data.label)

    row = ManagedLink(key=key, destination=destination, label=label, is_active=True)
    db.add(row)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise ManagedLinkExistsException() from None
    db.refresh(row)
    return row


def update_link(db: Session, key: str, data: ManagedLinkUpdateRequest) -> ManagedLink:
    row = get_link(db, key)
    if data.destination is not None:
        row.destination = _normalize_destination(data.destination)
    if data.label is not None:
        # Empty string clears the label:
        row.label = _normalize_label(data.label)
    if data.is_active is not None:
        row.is_active = data.is_active
    db.commit()
    db.refresh(row)
    return row


def delete_link(db: Session, key: str) -> None:
    row = get_link(db, key)
    db.delete(row)
    db.commit()
