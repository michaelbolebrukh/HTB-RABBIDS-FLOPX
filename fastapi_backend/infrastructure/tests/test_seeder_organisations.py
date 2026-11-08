"""Seeding partners from committed code: idempotent, add-only, never destructive.

Who our partners are is public product data, declared in the product boundary
and applied on every boot, so the property that matters is what a SECOND boot
does. Three things must hold, and each has cost somebody a production incident
somewhere:

- an organisation that already exists is ADOPTED by slug, not duplicated (the
  live database already holds the row a one-off job created);
- grants union in and never out, so a capability granted by hand survives;
- an existing partnership is left completely alone. Status and dates are changed
  through the API by operators and agents, and a restart must not undo them.

Presentation fields are the deliberate exception and get their own tests: no API
writes name/website_url/logo_url, so the declaration is their only author and a
difference is drift rather than somebody's decision.
"""

from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from infrastructure import seeder
from infrastructure.database import Base
import platform.models  # noqa: F401 — registers the models on Base.metadata
from platform.capabilities import EARLY_ACCESS, FEATURE_PREVIEW, INBOX_READ
from platform.models import Organisation, Partnership
from platform.services import organisation_service as orgs
from platform.services import partnership_service as ps


# A declaration shaped exactly like the one the first instance shipped, used as
# a FIXTURE rather than read from product.organisations: the current instance
# declares no organisations (product/organisations.py), and the behaviour under
# test is the seeder's, not any one product's catalogue.
DECLARED = [
    {
        "slug": "example-partner",
        "name": "Example Partner",
        "granted_capabilities": [FEATURE_PREVIEW, EARLY_ACCESS],
        "partnerships": [
            {
                "season": "2026/27",
                "status": "signed",
                "offering_label": "Talent Partner",
                "snapshot_key": "example-partner-2026-27",
                "signed_on": date(2026, 7, 28),
                "starts_on": date(2026, 9, 1),
                "ends_on": date(2027, 8, 31),
            },
        ],
    },
]


@pytest.fixture(autouse=True)
def _declared_catalogue(monkeypatch):
    monkeypatch.setattr(seeder, "SEEDED_ORGANISATIONS", [dict(d) for d in DECLARED])


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(autocommit=False, autoflush=False, bind=engine)()
    try:
        yield session
    finally:
        session.close()


# -- the fixture's contract ----------------------------------------------------

def test_the_fixture_declares_the_sponsor_tier_grants():
    declaration = next(d for d in DECLARED if d["slug"] == "example-partner")
    assert set(declaration["granted_capabilities"]) == {FEATURE_PREVIEW, EARLY_ACCESS}
    assert declaration["partnerships"][0]["season"] == "2026/27"


# -- a fresh database ----------------------------------------------------------

def test_a_fresh_database_comes_up_complete(db):
    seeder.seed_organisations_and_partnerships(db)

    organisation = orgs.get_organisation(db, "example-partner")
    assert organisation is not None and organisation.name == "Example Partner"
    assert set(organisation.granted_capabilities) == {FEATURE_PREVIEW, EARLY_ACCESS}

    partnership = ps.find_partnership_for_season(db, organisation, "2026/27")
    assert partnership is not None
    assert partnership.status == "signed"
    assert partnership.offering_label == "Talent Partner"
    assert partnership.snapshot_key == "example-partner-2026-27"
    assert partnership.signed_on == date(2026, 7, 28)
    assert partnership.starts_on == date(2026, 9, 1)
    assert partnership.ends_on == date(2027, 8, 31)


# -- the second boot -----------------------------------------------------------

def test_reseeding_duplicates_nothing(db):
    seeder.seed_organisations_and_partnerships(db)
    seeder.seed_organisations_and_partnerships(db)
    seeder.seed_organisations_and_partnerships(db)

    assert db.query(Organisation).count() == 1
    assert db.query(Partnership).count() == 1


def test_a_status_changed_through_the_api_survives_a_restart(db):
    seeder.seed_organisations_and_partnerships(db)
    organisation = orgs.get_organisation(db, "example-partner")
    partnership = ps.find_partnership_for_season(db, organisation, "2026/27")

    # An operator (or an agent over the service token) ends the partnership
    # early and clears the end date:
    ps.update_partnership(db, partnership, {"status": "ended", "ends_on": None})

    seeder.seed_organisations_and_partnerships(db)  # next boot

    db.refresh(partnership)
    assert partnership.status == "ended"
    assert partnership.ends_on is None
    assert db.query(Partnership).count() == 1


def test_an_organisation_created_by_an_earlier_job_is_adopted_not_duplicated(db):
    # What production actually looks like: the org row exists already, with no
    # conferred grants, created outside this seeder:
    existing = Organisation(slug="example-partner", name="Example Partner")
    db.add(existing)
    db.commit()
    db.refresh(existing)
    original_id = existing.id

    seeder.seed_organisations_and_partnerships(db)

    assert db.query(Organisation).count() == 1
    adopted = orgs.get_organisation(db, "example-partner")
    assert adopted.id == original_id  # the SAME row, foreign keys intact
    assert set(adopted.granted_capabilities) == {FEATURE_PREVIEW, EARLY_ACCESS}
    assert ps.find_partnership_for_season(db, adopted, "2026/27") is not None


def test_grants_union_in_and_never_out(db, monkeypatch):
    # The declaration must state FEWER grants than the row holds, or a seeder
    # that wholesale-REPLACED the list would pass this test unchanged. So:
    # declared = {early-access}, held by hand = {feature:preview}, disjoint.
    monkeypatch.setattr(seeder, "SEEDED_ORGANISATIONS", [
        {"slug": "example-partner", "name": "Example Partner",
         "granted_capabilities": [EARLY_ACCESS]},
    ])
    seeder.seed_organisations_and_partnerships(db)

    organisation = orgs.get_organisation(db, "example-partner")
    orgs.set_granted_capabilities(db, organisation, [FEATURE_PREVIEW])

    seeder.seed_organisations_and_partnerships(db)  # next boot

    db.refresh(organisation)
    # The declared grant comes back IN; the hand-held one is never stripped OUT:
    assert set(organisation.granted_capabilities) == {FEATURE_PREVIEW, EARLY_ACCESS}


def test_a_stray_non_conferrable_grant_does_not_block_partnership_seeding(db, caplog):
    """The union validates the WHOLE list, including what the row already held.

    One operator capability left on the row therefore makes it raise on every
    boot — which must cost this organisation its grants union and nothing else.
    Before the step was isolated it took every partnership in the declaration
    with it, forever."""
    # Hand-written, bypassing the service that would have refused it:
    stray = Organisation(slug="example-partner", name="Example Partner",
                         granted_capabilities=[INBOX_READ])
    db.add(stray)
    db.commit()

    with caplog.at_level("ERROR"):
        seeder.seed_organisations_and_partnerships(db)

    organisation = orgs.get_organisation(db, "example-partner")
    # The grants union failed loudly (it is a real problem worth an error event):
    assert organisation.granted_capabilities == [INBOX_READ]
    assert any("conferred grant" in record.message for record in caplog.records)
    # ...and the partnership downstream of it was seeded anyway:
    assert ps.find_partnership_for_season(db, organisation, "2026/27") is not None


def test_presentation_fields_follow_the_declaration(db):
    """Nothing else writes name/website_url/logo_url, so a difference is drift.

    A stale name is not somebody's decision surviving a boot — it is served to
    that sponsor's own dashboard as `organisation_name`."""
    stale = Organisation(slug="example-partner", name="Partner (old name)",
                         website_url="https://old.example.com")
    db.add(stale)
    db.commit()
    db.refresh(stale)
    original_id = stale.id

    seeder.seed_organisations_and_partnerships(db)

    adopted = orgs.get_organisation(db, "example-partner")
    assert adopted.id == original_id  # reconciled in place, not replaced
    assert adopted.name == "Example Partner"
    # The declaration states no website_url, so "not declared" leaves it alone
    # rather than clearing it:
    assert adopted.website_url == "https://old.example.com"


def test_partnership_state_is_still_never_reconciled(db):
    # The counterpart to the test above: presentation follows the declaration,
    # partnership state emphatically does not:
    seeder.seed_organisations_and_partnerships(db)
    organisation = orgs.get_organisation(db, "example-partner")
    partnership = ps.find_partnership_for_season(db, organisation, "2026/27")
    ps.update_partnership(db, partnership, {"offering_label": "Headline Partner"})

    seeder.seed_organisations_and_partnerships(db)

    db.refresh(partnership)
    assert partnership.offering_label == "Headline Partner"


# -- failure containment -------------------------------------------------------

def test_a_bad_declaration_is_logged_and_skipped(db, monkeypatch, caplog):
    monkeypatch.setattr(seeder, "SEEDED_ORGANISATIONS", [
        {"slug": "broken", "name": "Broken", "partnerships": [{"season": "2026/27", "status": "nope"}]},
        {"slug": "jane-street", "name": "Jane Street"},
    ])

    with caplog.at_level("ERROR"):
        seeder.seed_organisations_and_partnerships(db)

    # One bad declaration never takes the rest of the boot with it:
    assert orgs.get_organisation(db, "jane-street") is not None
    assert any("Failed to seed organisation 'broken'" in record.message for record in caplog.records)


def test_nothing_declared_is_a_no_op(db, monkeypatch):
    monkeypatch.setattr(seeder, "SEEDED_ORGANISATIONS", [])
    seeder.seed_organisations_and_partnerships(db)
    assert db.query(Organisation).count() == 0


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
