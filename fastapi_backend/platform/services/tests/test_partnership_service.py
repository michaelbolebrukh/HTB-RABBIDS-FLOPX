"""Partnership records: validated writes, computed reads, append-only history.

The three properties the rest of the design leans on:

- the status typo guard, the sibling of the capability one — an unrecognised
  state is refused rather than stored, because a status nothing recognises still
  looks like a decision;
- expiry is COMPUTED from `ends_on` per read, so it can never disagree with the
  stored status and no job ever flips one;
- a renewal is a NEW row. Seasons accumulate, nothing is overwritten, so "when
  did this organisation first sign" stays answerable from the table.
"""

from datetime import date, timedelta, UTC, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from infrastructure.database import Base
import platform.models  # noqa: F401 — registers the models on Base.metadata
from platform.exceptions import UnknownPartnershipStatusException
from platform.models import Organisation, Partnership
from platform.partnerships import PARTNERSHIP_STATUSES
from platform.services import organisation_service as orgs
from platform.services import partnership_service as ps


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(autocommit=False, autoflush=False, bind=engine)()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def organisation(db) -> Organisation:
    return orgs.create_organisation(db, "hudson-river-trading", "Hudson River Trading")


def _today() -> date:
    return datetime.now(UTC).date()


# -- status validation ---------------------------------------------------------

def test_every_registry_status_is_accepted(db, organisation):
    for index, status in enumerate(PARTNERSHIP_STATUSES):
        created = ps.create_partnership(db, organisation, season=f"20{index}0/{index}1", status=status)
        assert created.status == status


def test_unknown_status_is_refused_on_create(db, organisation):
    with pytest.raises(UnknownPartnershipStatusException):
        ps.create_partnership(db, organisation, season="2026/27", status="signd")

    # Refused OUTRIGHT, never half-written:
    assert db.query(Partnership).count() == 0


def test_unknown_status_is_refused_on_update(db, organisation):
    created = ps.create_partnership(db, organisation, season="2026/27", status="sent")

    with pytest.raises(UnknownPartnershipStatusException):
        ps.update_partnership(db, created, {"status": "countersigned"})

    db.refresh(created)
    assert created.status == "sent"


def test_status_defaults_to_draft(db, organisation):
    created = ps.create_partnership(db, organisation, season="2026/27")
    assert created.status == "draft"


# -- computed expiry -----------------------------------------------------------

def test_open_ended_partnership_never_expires(db, organisation):
    created = ps.create_partnership(db, organisation, season="2026/27", status="signed")
    assert created.ends_on is None
    assert created.days_remaining is None
    assert created.is_expired is False


def test_days_remaining_counts_down_to_the_end_date(db, organisation):
    created = ps.create_partnership(
        db, organisation, season="2026/27", status="signed", ends_on=_today() + timedelta(days=30),
    )
    assert created.days_remaining == 30
    assert created.is_expired is False


def test_a_past_end_date_reads_as_expired_without_touching_the_status(db, organisation):
    created = ps.create_partnership(
        db, organisation, season="2025/26", status="signed", ends_on=_today() - timedelta(days=1),
    )
    assert created.days_remaining == -1
    assert created.is_expired is True
    # The stored decision is untouched — moving to "ended" is an explicit act:
    assert created.status == "signed"


def test_the_last_day_is_not_yet_expired(db, organisation):
    created = ps.create_partnership(db, organisation, season="2026/27", ends_on=_today())
    assert created.days_remaining == 0
    assert created.is_expired is False


# -- history is append-only ----------------------------------------------------

def test_an_organisation_accumulates_seasons(db, organisation):
    ps.create_partnership(db, organisation, season="2025/26", status="ended",
                          starts_on=date(2025, 9, 1))
    ps.create_partnership(db, organisation, season="2026/27", status="signed",
                          starts_on=date(2026, 9, 1))

    seasons = [row.season for row in ps.partnerships_for_organisation(db, organisation)]
    # Most recent first, and the older one is still there:
    assert seasons == ["2026/27", "2025/26"]


def test_history_is_ordered_by_start_date_not_by_the_season_label(db, organisation):
    """Season is an opaque string the backend never parses, so it must not be
    what history sorts on — a lexicographic sort only looks right for labels
    that happen to sort like dates, and these deliberately do not."""
    ps.create_partnership(db, organisation, season="Autumn 2026", starts_on=date(2026, 9, 1))
    ps.create_partnership(db, organisation, season="FY25", starts_on=date(2025, 9, 1))

    seasons = [row.season for row in ps.partnerships_for_organisation(db, organisation)]
    # Sorted lexicographically, "FY25" would have come first:
    assert seasons == ["Autumn 2026", "FY25"]


def test_a_partnership_with_no_start_date_sorts_last(db, organisation):
    undated = ps.create_partnership(db, organisation, season="TBC")
    ps.create_partnership(db, organisation, season="2025/26", starts_on=date(2025, 9, 1))

    rows = ps.partnerships_for_organisation(db, organisation)
    # Nothing to order it by, and it was inserted first — so this is the
    # nullslast, not insertion order:
    assert rows[-1].id == undated.id


def test_find_partnership_for_season_is_the_seeder_idempotency_key(db, organisation):
    assert ps.find_partnership_for_season(db, organisation, "2026/27") is None

    created = ps.create_partnership(db, organisation, season="2026/27")
    found = ps.find_partnership_for_season(db, organisation, "2026/27")

    assert found is not None and found.id == created.id
    assert ps.find_partnership_for_season(db, organisation, "2027/28") is None


def test_an_organisation_with_no_partnership_reads_as_an_empty_list(db):
    prospect = orgs.create_organisation(db, "jane-street", "Jane Street")
    # Absence is a normal answer — the reader treats it as "prospect", not an error:
    assert ps.partnerships_for_organisation(db, prospect) == []


# -- partial updates -----------------------------------------------------------

def test_marking_signed_leaves_every_unmentioned_field_alone(db, organisation):
    created = ps.create_partnership(
        db, organisation, season="2026/27", status="sent",
        offering_label="Talent Partner", snapshot_key="hrt-2026-27",
        starts_on=date(2026, 9, 1), ends_on=date(2027, 8, 31),
    )

    ps.update_partnership(db, created, {"status": "signed", "signed_on": date(2026, 7, 28)})

    db.refresh(created)
    assert created.status == "signed" and created.signed_on == date(2026, 7, 28)
    # The one-field call cannot blank what it never mentioned:
    assert created.offering_label == "Talent Partner"
    assert created.snapshot_key == "hrt-2026-27"
    assert created.starts_on == date(2026, 9, 1)
    assert created.ends_on == date(2027, 8, 31)


def test_an_explicit_null_clears_a_field(db, organisation):
    created = ps.create_partnership(db, organisation, season="2026/27", ends_on=date(2027, 8, 31))

    ps.update_partnership(db, created, {"ends_on": None})

    db.refresh(created)
    assert created.ends_on is None and created.is_expired is False


def test_unknown_keys_are_ignored_rather_than_half_applied(db, organisation):
    created = ps.create_partnership(db, organisation, season="2026/27")

    ps.update_partnership(db, created, {"organisation_id": 999, "amount": 1000, "status": "sent"})

    db.refresh(created)
    assert created.status == "sent"
    # Re-pointing a partnership at another organisation is not an update:
    assert created.organisation_id == organisation.id


# -- cross-organisation listing ------------------------------------------------

def test_list_partnerships_orders_by_soonest_end_date(db, organisation):
    other = orgs.create_organisation(db, "jane-street", "Jane Street")
    ps.create_partnership(db, organisation, season="2026/27", ends_on=date(2027, 8, 31))
    ps.create_partnership(db, other, season="2026/27", ends_on=date(2026, 12, 31))
    ps.create_partnership(db, other, season="rolling")  # open-ended sorts last

    listed = ps.list_partnerships(db)
    assert [row.ends_on for row in listed] == [date(2026, 12, 31), date(2027, 8, 31), None]
    # The organisation is loaded for every row, so the response shape never
    # lazy-loads it one query at a time:
    assert [row.organisation.slug for row in listed[:2]] == ["jane-street", "hudson-river-trading"]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
