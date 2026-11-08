"""Partnership records: the STATE of an organisation's agreement with us.

The whole of what the backend knows. A partnership names an organisation, a
season, a workflow status, and some opaque labels — no tiers, no benefits, no
prices, nothing that would need a migration when the offering is redesigned. The
catalogue lives on the frontend; this module records decisions about it.

Two properties the rest of the system depends on:

- WRITES ARE VALIDATED, READS ARE COMPUTED. Every status written is checked
  against PARTNERSHIP_STATUSES (the typo guard — an unrecognised state would be
  stored happily and read as nonsense forever), while expiry is derived from
  `ends_on` at read time on the model. Nothing flips a status on a timer, so a
  status is always something somebody decided.
- HISTORY IS APPEND-ONLY. A renewal is a new row for the new season; there is no
  delete and no overwrite of a past season, so "when did HRT first sign" stays
  answerable from the table.

Authorization lives entirely at the router: these functions take an organisation
or a partnership and do the work. Conferral, membership and grants are the
organisation module's concern (organisation_service) — a partnership confers
nothing by itself.
"""

from datetime import date
from typing import Optional

from sqlalchemy.orm import Session, joinedload

from platform.exceptions import UnknownPartnershipStatusException
from platform.models import Organisation, Partnership
from platform.partnerships import DEFAULT_PARTNERSHIP_STATUS, PARTNERSHIP_STATUSES


# The columns a PATCH may touch. `organisation_id` is deliberately absent: a
# partnership belongs to the organisation it was created for, and re-pointing it
# would silently rewrite another organisation's history:
UPDATABLE_FIELDS: tuple[str, ...] = (
    "status",
    "season",
    "offering_label",
    "snapshot_key",
    "starts_on",
    "ends_on",
    "signed_on",
)


def _validated_status(status: Optional[str]) -> str:
    """Validates a status against the registry, defaulting when none is given.

    The same rule as the capability typo guard: an unknown string is refused
    outright rather than stored, because a status nothing recognises gates and
    means nothing but still looks like a decision.

    None means "the caller did not say", which is only ever true on the CREATE
    path — PartnershipUpdateRequest refuses an explicit null before it gets here,
    so a PATCH can never reach this branch and silently reset a row to draft."""
    if status is None:
        return DEFAULT_PARTNERSHIP_STATUS
    if status not in PARTNERSHIP_STATUSES:
        raise UnknownPartnershipStatusException(status, PARTNERSHIP_STATUSES)
    return status


def _commit(db: Session, instance: Partnership) -> Partnership:
    """Commits one partnership write, rolling back before re-raising"""
    db.add(instance)
    try:
        db.commit()
    except Exception:
        db.rollback()
        raise
    db.refresh(instance)
    return instance


def get_partnership(db: Session, partnership_id: int) -> Optional[Partnership]:
    """Looks a partnership up by id, with its organisation loaded.

    Eager-loaded because every response shape carries the organisation's slug
    and name; lazy-loading it would be the N+1 the database rule forbids."""
    return (
        db.query(Partnership)
        .options(joinedload(Partnership.organisation))
        .filter(Partnership.id == partnership_id)
        .first()
    )


def partnerships_for_organisation(db: Session, organisation: Organisation) -> list[Partnership]:
    """Every partnership this organisation has held, most recent first.

    Ordered by `starts_on` — NOT by `season`. Season is an opaque label the
    backend never parses, so sorting on it is a lexicographic accident that
    happens to look right for "2026/27" and stops being right the moment a season
    is written "Autumn 2026" or "FY27". A row with no start date has nothing to
    order by and sorts last; `id` (insertion order) breaks every remaining tie.

    An empty list is a perfectly normal answer — an organisation with no
    partnership row has simply never had one, which is what a reader treats as
    "prospect". Absence is not an error here."""
    return (
        db.query(Partnership)
        .options(joinedload(Partnership.organisation))
        .filter(Partnership.organisation_id == organisation.id)
        .order_by(Partnership.starts_on.desc().nullslast(), Partnership.id.desc())
        .all()
    )


def list_partnerships(db: Session) -> list[Partnership]:
    """Every partnership on the instance, for operator surfaces.

    Ordered by end date so the "expiring soon" question reads straight off the
    top of the list (open-ended rows sort last, having no date to expire)."""
    return (
        db.query(Partnership)
        .options(joinedload(Partnership.organisation))
        .order_by(Partnership.ends_on.is_(None), Partnership.ends_on.asc(), Partnership.id.asc())
        .all()
    )


def find_partnership_for_season(db: Session, organisation: Organisation, season: str) -> Optional[Partnership]:
    """The organisation's partnership for one season, if it has one.

    The seeder's idempotency key: seeding creates a row only when this returns
    None, so a status an agent later changed is never overwritten at boot."""
    return (
        db.query(Partnership)
        .filter(
            Partnership.organisation_id == organisation.id,
            Partnership.season == season,
        )
        .first()
    )


def create_partnership(
    db: Session,
    organisation: Organisation,
    *,
    season: str,
    status: Optional[str] = None,
    offering_label: Optional[str] = None,
    snapshot_key: Optional[str] = None,
    starts_on: Optional[date] = None,
    ends_on: Optional[date] = None,
    signed_on: Optional[date] = None,
) -> Partnership:
    """Creates a partnership for an organisation and season.

    Deliberately NOT idempotent on (organisation, season), unlike
    create_organisation: two agreements in one season are legitimate, so the
    caller that wants at-most-one — the seeder — asks
    find_partnership_for_season first and decides for itself."""
    partnership = Partnership(
        organisation_id=organisation.id,
        season=season,
        status=_validated_status(status),
        offering_label=offering_label,
        snapshot_key=snapshot_key,
        starts_on=starts_on,
        ends_on=ends_on,
        signed_on=signed_on,
    )
    return _commit(db, partnership)


def update_partnership(db: Session, partnership: Partnership, changes: dict) -> Partnership:
    """Applies a partial update to a partnership.

    PATCH semantics, taken literally: only the keys PRESENT in `changes` are
    written, so an absent field keeps its value and an explicit null clears it —
    the distinction a "mark this signed" call needs in order not to wipe the
    dates it did not mention. Unknown keys are ignored rather than half-applied
    (UPDATABLE_FIELDS is the allow-list), and the status is validated before
    anything is written."""
    for field in UPDATABLE_FIELDS:
        if field not in changes:
            continue
        value = changes[field]
        if field == "status":
            value = _validated_status(value)
        setattr(partnership, field, value)

    return _commit(db, partnership)
