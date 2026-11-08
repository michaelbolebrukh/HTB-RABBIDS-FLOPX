"""Organisations and the capabilities that membership confers.

Companies are DATA, not authorization. Gating stays the one primitive —
`require_capability(...)` — and no gate ever asks about an organisation. What an
organisation does is CONFER: it declares `granted_capabilities`, and while it is
active its members' effective grants include them. "HRT is a sponsor" therefore
becomes feature:preview + feature:early-access for its people, derived from a
relation instead of hand-set on each account.

Conferral is COMPUTED at check time and never materialised onto `users`. That is
why this module has no sync step: `users.capabilities` keeps its exact meaning
(grants held DIRECTLY by the account) and nothing here ever writes to it, so a
conferred grant stays distinguishable from a direct one without a second column.
Revocation is `remove_member` deleting one row — there is no copy to reconcile
and no way for the two to drift.

`effective_capabilities` is THE definition of the effective set; every reader
goes through it (User.has_capability, verify_capability, UserResponse) so the
rule cannot be reimplemented inconsistently.
"""

from typing import Iterable, Optional

from sqlalchemy.orm import Session

from platform.capabilities import ALL_CAPABILITIES, CONFERRABLE_CAPABILITIES
from platform.exceptions import UnknownCapabilityException
from platform.models import Organisation, OrganisationMember, User


def _validated_capabilities(capabilities: Optional[Iterable[str]]) -> list[str]:
    """Validates grant strings an organisation may confer, preserving order.

    Two guards, not one. The typo guard is the same rule agents follow when
    granting by hand: an unrecognised string would be stored happily and
    silently gate nothing, so a write carrying one is refused outright rather
    than half-applied. On top of that, only CONFERRABLE_CAPABILITIES may be
    conferred - membership applies a grant to an unbounded, later-growing set of
    accounts, so operator capabilities stay one-account-at-a-time decisions."""
    validated: list[str] = []
    for capability in capabilities or []:
        if capability not in ALL_CAPABILITIES:
            raise UnknownCapabilityException(capability)
        if capability not in CONFERRABLE_CAPABILITIES:
            raise UnknownCapabilityException(
                f"{capability} (not conferrable by an organisation)"
            )
        if capability not in validated:
            validated.append(capability)
    return validated


def effective_capabilities(user: User) -> set[str]:
    """Every capability this account effectively holds.

    `direct ∪ granted-by-each-ACTIVE-organisation-it-belongs-to`. Pure — it
    performs no writes — and it is the single definition of the effective set:
    User.has_capability, verify_capability and UserResponse all resolve through
    it, so conferral cannot mean one thing at a gate and another in a response.

    An INACTIVE organisation confers nothing, which is how a lapsed sponsorship
    stops conferring without deleting the relation or its member rows.

    Admin is deliberately NOT folded in here: admin is an identity that passes
    every check (applied by the callers), not a set of grants."""
    granted = set(user.capabilities or [])

    for membership in (user.organisation_memberships or []):
        organisation = membership.organisation
        if organisation is not None and organisation.is_active:
            # The writers refuse a non-conferrable string, but a row written
            # past them (a raw database edit) must still confer nothing it
            # could not have been given through the service.
            granted |= set(organisation.granted_capabilities or []) & CONFERRABLE_CAPABILITIES

    return granted


def get_organisation(db: Session, slug: str) -> Optional[Organisation]:
    """Looks an organisation up by its stable slug"""
    return db.query(Organisation).filter(Organisation.slug == slug).first()


def get_organisation_by_id(db: Session, organisation_id: int) -> Optional[Organisation]:
    """Looks an organisation up by primary key, for callers holding an id rather
    than the slug (the slug stays the identifier a client should key off)"""
    return db.query(Organisation).filter(Organisation.id == organisation_id).first()


def create_organisation(
    db: Session,
    slug: str,
    name: str,
    *,
    website_url: Optional[str] = None,
    logo_url: Optional[str] = None,
    granted_capabilities: Iterable[str] = (),
) -> Organisation:
    """Creates an organisation, idempotently on slug.

    Re-running with the same slug returns the existing row rather than raising
    or duplicating, so an agent-operated one-off is safe to replay. Every
    granted capability is validated before anything is written."""
    validated = _validated_capabilities(granted_capabilities)

    existing = get_organisation(db, slug)
    if existing is not None:
        return existing

    organisation = Organisation(
        slug=slug,
        name=name,
        website_url=website_url,
        logo_url=logo_url,
        granted_capabilities=validated,
    )
    db.add(organisation)
    try:
        db.commit()
    except Exception:
        db.rollback()
        raise
    db.refresh(organisation)
    return organisation


def set_granted_capabilities(db: Session, organisation: Organisation, capabilities: Iterable[str]) -> Organisation:
    """Replaces what this organisation confers on its members.

    Validated against the registry first. There is no sync step to run
    afterwards: every member's effective set changes on their next check,
    because conferral is computed rather than copied onto their row."""
    organisation.granted_capabilities = _validated_capabilities(capabilities)
    db.add(organisation)
    try:
        db.commit()
    except Exception:
        db.rollback()
        raise
    db.refresh(organisation)
    return organisation


def set_active(db: Session, organisation: Organisation, is_active: bool) -> Organisation:
    """Activates or deactivates an organisation.

    Deactivating stops conferral for every member at once while keeping the
    relation and its history intact — the reversible form of revocation."""
    organisation.is_active = is_active
    db.add(organisation)
    try:
        db.commit()
    except Exception:
        db.rollback()
        raise
    db.refresh(organisation)
    return organisation


def get_membership(db: Session, organisation: Organisation, user: User) -> Optional[OrganisationMember]:
    """Looks up one account's membership of one organisation"""
    return (
        db.query(OrganisationMember)
        .filter(
            OrganisationMember.organisation_id == organisation.id,
            OrganisationMember.user_id == user.id,
        )
        .first()
    )


def add_member(
    db: Session,
    organisation: Organisation,
    user: User,
    *,
    title: Optional[str] = None,
) -> OrganisationMember:
    """Adds an account to an organisation, idempotently on (organisation, user).

    The unique constraint keeps the pair to a single row; re-running updates the
    title when one is supplied rather than raising. From here the member's
    effective grants include the organisation's, with nothing to sync."""
    existing = get_membership(db, organisation, user)
    if existing is not None:
        if title is not None and existing.title != title:
            existing.title = title
            db.add(existing)
            try:
                db.commit()
            except Exception:
                db.rollback()
                raise
            db.refresh(existing)
        return existing

    membership = OrganisationMember(
        organisation_id=organisation.id,
        user_id=user.id,
        title=title,
    )
    db.add(membership)
    try:
        db.commit()
    except Exception:
        db.rollback()
        raise
    db.refresh(membership)
    # Refreshing the user so an already-loaded instance sees the new membership
    # on its next capability check instead of a collection loaded before it:
    db.refresh(user)
    return membership


def remove_member(db: Session, organisation: Organisation, user: User) -> None:
    """Removes an account from an organisation. This is the WHOLE of revocation.

    Deleting the row suffices because the conferred grants were never copied
    onto the account: the member's next check simply stops seeing them. Grants
    held DIRECTLY on the user are untouched — a different column, which nothing
    in this module ever writes."""
    membership = get_membership(db, organisation, user)
    if membership is None:
        return

    db.delete(membership)
    try:
        db.commit()
    except Exception:
        db.rollback()
        raise
    # Refreshing so an already-loaded instance stops seeing the membership:
    db.refresh(user)


def members_of(db: Session, organisation: Organisation) -> list[User]:
    """Every account belonging to this organisation.

    The query capabilities cannot answer ("list everyone at HRT"), and one of
    the reasons organisations are a relation rather than a grant string."""
    return (
        db.query(User)
        .join(OrganisationMember, OrganisationMember.user_id == User.id)
        .filter(OrganisationMember.organisation_id == organisation.id)
        .all()
    )
