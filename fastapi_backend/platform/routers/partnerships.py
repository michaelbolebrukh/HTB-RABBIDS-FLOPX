"""Partnership records: read one organisation's, or manage every organisation's.

Two audiences, one resource. A member of an organisation may read that
organisation's partnerships (the data-scoped ownership check over the membership
rows — capabilities are not parameterised, so "HRT's own people" cannot be a
grant string), while every cross-organisation and every WRITE surface gates on
the single primitive, require_capability(PARTNERSHIPS_MANAGE). Admin passes both
implicitly: the member-facing check goes through User.has_capability, which is
the same rule verify_capability applies, so no endpoint here ever asks about
is_admin (see the access-control rule).

The write surfaces are what let an agent holding a service token mark a
partnership signed — the designed mechanism, which is why the service account is
seeded with the grant.

The member-facing read is deliberately NARROWER than the manage read in two ways
that both come down to the same principle — a response body is readable in
devtools, so the filtering has to happen here and not at the client: an
organisation the caller may not read is indistinguishable from one that does not
exist (no existence oracle over our pipeline), and draft rows are withheld until
somebody decides to send them.
"""

from fastapi import APIRouter
from fastapi import status as st
from sqlalchemy.orm import Session

from infrastructure.dependencies import db_dependency, require_capability, user_dependency
from platform.capabilities import PARTNERSHIPS_MANAGE
from platform.exceptions import (
    OrganisationNotFoundException,
    PartnershipNotFoundException,
)
from platform.models import Organisation, User
from platform.partnerships import DRAFT_PARTNERSHIP_STATUS
from platform.schemas import PartnershipCreateRequest, PartnershipResponse, PartnershipUpdateRequest
from platform.services import organisation_service as orgs
from platform.services import partnership_service as ps


router = APIRouter(prefix="/partnerships", tags=["Partnerships"])

# Creating/updating any partnership, and reading across organisations, requires
# the partnerships:manage grant (admins hold every capability implicitly):
partnership_manager_dependency = require_capability(PARTNERSHIPS_MANAGE)


def _readable_organisation(db: Session, slug: str, user: User) -> Organisation:
    """Resolves an organisation the caller is allowed to read partnerships for.

    Membership of an ACTIVE organisation, OR the manage grant. `has_capability`
    is the Python twin of the gate dependency — it resolves the effective set and
    admits admin implicitly — so this stays a capability check with a data-scoped
    alternative, never a role check.

    Unauthorized COLLAPSES ONTO NOT-FOUND: an unknown slug and a slug the caller
    may not read are the same 404. Distinguishing them would make this endpoint
    an existence oracle — any verified account could enumerate which firms are in
    our pipeline, which is commercially sensitive in a way the rows themselves
    are not. The manage grant is the only thing that tells the two apart.

    Inactivity is read the same way `effective_capabilities` reads it: a lapsed
    organisation confers nothing, so it stops opening the member-facing door too.
    Manage holders still read it — operators need the lapsed rows most."""
    organisation = orgs.get_organisation(db, slug)

    if user.has_capability(PARTNERSHIPS_MANAGE):
        if organisation is None:
            raise OrganisationNotFoundException()
        return organisation

    if (
        organisation is not None
        and organisation.is_active
        and orgs.get_membership(db, organisation, user) is not None
    ):
        return organisation

    raise OrganisationNotFoundException()


# -- member-facing read --------------------------------------------------------

@router.get("/for-org/{slug}", response_model=list[PartnershipResponse], status_code=st.HTTP_200_OK)
def get_partnerships_for_organisation(slug: str, db: db_dependency, user: user_dependency):
    """Every partnership this organisation has held, across seasons.

    An empty list is a normal answer, not an error: an organisation that has
    never had a partnership is simply a prospect, and the client renders that.

    DRAFTS ARE WITHHELD from the member-facing view. A draft is our internal
    thinking about a firm — next season's offering label, dates and the fact we
    are drafting at all — and it reaches the sponsor contact only when somebody
    decides to send it (`sent`). Withholding it here rather than at the client is
    the point: a row in the response is readable in devtools no matter what the
    UI renders. Manage holders (admins implicitly) see every row."""
    organisation = _readable_organisation(db, slug, user)
    partnerships = ps.partnerships_for_organisation(db, organisation)

    if user.has_capability(PARTNERSHIPS_MANAGE):
        return partnerships
    return [p for p in partnerships if p.status != DRAFT_PARTNERSHIP_STATUS]


# -- management (partnerships:manage) ------------------------------------------

@router.get("", response_model=list[PartnershipResponse], status_code=st.HTTP_200_OK)
def list_partnerships(db: db_dependency, _: partnership_manager_dependency):
    """Every partnership on the instance, soonest end date first — the shape the
    "expiring soon" operator view reads, with expiry computed per response."""
    return ps.list_partnerships(db)


@router.post("", response_model=PartnershipResponse, status_code=st.HTTP_201_CREATED)
def create_partnership(body: PartnershipCreateRequest, db: db_dependency, _: partnership_manager_dependency):
    # Slug wins when both are supplied — it is the stable external identifier:
    if body.organisation_slug is not None:
        organisation = orgs.get_organisation(db, body.organisation_slug)
    else:
        organisation = orgs.get_organisation_by_id(db, body.organisation_id)

    if organisation is None:
        raise OrganisationNotFoundException()

    return ps.create_partnership(
        db,
        organisation,
        season=body.season,
        status=body.status,
        offering_label=body.offering_label,
        snapshot_key=body.snapshot_key,
        starts_on=body.starts_on,
        ends_on=body.ends_on,
        signed_on=body.signed_on,
    )


@router.patch("/{partnership_id}", response_model=PartnershipResponse, status_code=st.HTTP_200_OK)
def update_partnership(partnership_id: int, body: PartnershipUpdateRequest, db: db_dependency,
                       _: partnership_manager_dependency):
    """Marks a partnership signed, ended, re-dated or re-labelled.

    Only the fields present in the body are written (exclude_unset), so a
    one-field "mark this signed" call leaves every date it did not mention
    exactly as it was."""
    partnership = ps.get_partnership(db, partnership_id)
    if partnership is None:
        raise PartnershipNotFoundException()

    return ps.update_partnership(db, partnership, body.model_dump(exclude_unset=True))
