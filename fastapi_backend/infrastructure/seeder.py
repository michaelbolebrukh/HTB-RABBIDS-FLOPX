import logging

from sqlalchemy.orm import Session

from platform.capabilities import (
    LINKS_MANAGE,
    NOTIFICATIONS_BROADCAST,
    PARTNERSHIPS_MANAGE,
)
from platform.models import Organisation, User
from platform.enums import Role
from platform.services import organisation_service as orgs
from platform.services import partnership_service as ps
from product.organisations import SEEDED_ORGANISATIONS
from security import bcrypt_context
from infrastructure.service_auth import service_account_email
from infrastructure.settings import settings as s


logger = logging.getLogger(__name__)


# What the shared service account is seeded with, so a fresh instance keeps the
# agent-operated paths working: pushing announcements, and recording partnership
# state (marking one signed over the API is the designed mechanism — see the
# access-control rule). Add-only: a grant is never revoked as a boot side effect.
SERVICE_ACCOUNT_CAPABILITIES: tuple[str, ...] = (
    NOTIFICATIONS_BROADCAST,
    PARTNERSHIPS_MANAGE,
)


def _seed_display_name(email: str) -> str:
    """Derives a non-empty display name for a seeded account from the email local-part."""
    from platform.utils.name_format import format_display_name
    local_part = email.split("@", 1)[0].strip().replace(".", " ").replace("_", " ")
    return format_display_name(local_part) if local_part else "User"


def seed_test_user_if_configured(db: Session) -> None:
    """Creates a verified test account if TEST_USER_EMAIL and TEST_USER_PASSWORD are set.

    Idempotent: rerunning never duplicates the user. If the user already exists
    we keep their row, refresh the password from env, and ensure they are
    marked verified so Playwright can sign in.

    The password refresh is the point of this account — QA tooling must be able
    to sign in after every boot — and it is also why the account must never be a
    human. Refreshing an admin's password from env on every restart would silently
    overwrite whatever they set through the normal reset flow, so a configured
    test user that is also a configured admin (or whose row is already an admin)
    is refused with an ERROR rather than half-applied. The address itself should
    be org-controlled (`TEST_USER_EMAIL_LOCAL_PART` puts it on APP_DOMAIN), per
    the access-control rule's email invariant for machine accounts.
    """
    email = (s.TEST_USER_EMAIL or "").strip().lower()
    password = (s.TEST_USER_PASSWORD or "").strip()

    if not email or not password:
        logger.info("Test user env vars not set; skipping test-user seeding.")
        return

    if email in s.admin_emails:
        logger.error(
            "TEST_USER_EMAIL '%s' is a configured admin address; refusing to seed the "
            "test user — a human admin's password must not be rewritten from env on "
            "every boot. Point TEST_USER_EMAIL at a separate org-controlled QA address.",
            email,
        )
        return

    user = db.query(User).filter(User.email == email).first()

    if user is not None and user.role == Role.ADMIN:
        logger.error(
            "TEST_USER_EMAIL '%s' resolves to an existing admin row; refusing to "
            "refresh its password or grants from env.", email,
        )
        return

    hashed = bcrypt_context.hash(password)

    if user is None:
        user = User(
            name="Test User",
            email=email,
            password=hashed,
            is_verified=True,
            role=Role.USER,
            marketing_consent=False,
            # QA account may exercise operator surfaces (links manage) without being admin:
            capabilities=[LINKS_MANAGE],
        )
        db.add(user)
        db.commit()
        logger.info("Seeded fresh test user '%s'.", email)
        return

    # Refreshing password and verification flag without otherwise mutating the user.
    # Grant links:manage add-only so preview/Playwright can open /links:
    grants = list(user.capabilities or [])
    if LINKS_MANAGE not in grants:
        grants.append(LINKS_MANAGE)
        user.capabilities = grants
    user.password = hashed
    user.is_verified = True
    db.commit()
    logger.info("Refreshed existing test user '%s' (password refreshed from env, verified).", email)


def seed_admin_user_if_configured(db: Session) -> None:
    """Seeds a verified admin account for every configured admin email.

    Admins come from two env vars (see settings): ADMIN_EMAIL for the platform
    owner(s) that carry across startup clones, and PRODUCT_ADMIN_EMAIL for
    admin(s) specific to this instance. Each accepts a single email or a CSV
    list; `settings.admin_emails` is the merged, de-duplicated set. Both grant
    identical admin rights today — the split is provenance/clone-hygiene.

    Idempotent and add-only: reruns never duplicate a user, and existing rows
    only gain the admin/verified flags (admin is never revoked here — removing an
    email from the env does not strip admin on the next boot). New admins are
    created with no password so the owner sets one via the email code flow.

    A row that is unverified AND password-bearing is never promoted: it was
    registered by someone who never proved mailbox ownership (see _seed_one_admin).
    """
    emails = s.admin_emails

    if not emails:
        logger.info("No admin emails configured; skipping admin-user seeding.")
        return

    for email in emails:
        _seed_one_admin(db, email)


def _seed_one_admin(db: Session, email: str) -> None:
    """Seeds or promotes a single admin (add-only). `email` is already normalized."""
    user = db.query(User).filter(User.email == email).first()

    if user is None:
        user = User(
            name=_seed_display_name(email),
            email=email,
            password=None,
            is_verified=True,
            # Admin is the catch-all identity (implicitly holds every capability):
            role=Role.ADMIN,
            marketing_consent=False,
        )
        db.add(user)
        db.commit()
        logger.info("Seeded fresh admin user '%s' (no password; set via email flow).", email)
        return

    if not user.is_verified and user.password is not None:
        # Refusing to promote a row registered by someone who never proved
        # mailbox ownership — verifying and promoting it here would hand the
        # admin identity to whoever squatted the address first:
        logger.error(
            "Admin email '%s' is held by an unverified password-bearing user; "
            "refusing to promote — investigate.", email,
        )
        return

    changed = False
    if user.role != Role.ADMIN:
        user.role = Role.ADMIN
        changed = True
    if not user.is_verified:
        user.is_verified = True
        changed = True

    if not changed:
        logger.info("Admin user '%s' already present; no changes applied.", email)
        return

    db.commit()
    logger.info("Refreshed existing admin user '%s' (admin and verified flags ensured).", email)


def seed_service_account_if_configured(db: Session) -> None:
    """Seeds the shared service account whenever SERVICE_TOKENS is configured.

    Service tokens resolve to this persisted account (see
    infrastructure/service_auth.py): one row, email service@<APP_DOMAIN> (an
    address under org control by construction), verified, role=user, headless
    (no password, so interactive login paths are dead by construction), and no
    marketing consent.

    Idempotent and add-only like the admin seeder: reruns never duplicate the
    row, and grants only union in — SERVICE_ACCOUNT_CAPABILITIES is seeded so a
    fresh instance keeps the agent-operated paths working; revoking a grant is a
    deliberate DB edit, never a boot side effect. A password-bearing row at
    the reserved address is never adopted (registration also refuses the
    address — see platform/services/user_service.create_user).
    """
    if not (s.SERVICE_TOKENS or "").strip():
        logger.info("SERVICE_TOKENS not set; skipping service-account seeding.")
        return

    email = service_account_email()
    user = db.query(User).filter(User.email == email).first()

    if user is None:
        user = User(
            name=_seed_display_name(email),
            email=email,
            password=None,
            is_verified=True,
            role=Role.USER,
            marketing_consent=False,
            capabilities=list(SERVICE_ACCOUNT_CAPABILITIES),
        )
        db.add(user)
        db.commit()
        logger.info("Seeded fresh service account '%s' (headless; %d grants).",
                    email, len(SERVICE_ACCOUNT_CAPABILITIES))
        return

    if user.password is not None:
        # Refusing to adopt a password-bearing row: the service account is
        # headless by construction, so a password here means someone else
        # registered the address — adopting it would hand them its grants:
        logger.error(
            "Service account email '%s' is held by a password-bearing user; "
            "refusing to adopt — investigate.", email,
        )
        return

    changed = False
    grants = list(user.capabilities or [])
    missing = [grant for grant in SERVICE_ACCOUNT_CAPABILITIES if grant not in grants]
    if missing:
        # Add-only union: existing grants are never removed here, so a grant an
        # operator added by hand survives every boot:
        user.capabilities = grants + missing
        changed = True
    if not user.is_verified:
        user.is_verified = True
        changed = True

    if not changed:
        logger.info("Service account '%s' already present; no changes applied.", email)
        return

    db.commit()
    logger.info("Refreshed existing service account '%s' (verified and grants ensured).", email)


def seed_organisations_and_partnerships(db: Session) -> None:
    """Seeds the instance's public organisations and their partnership records.

    Who our partners are is PUBLIC PRODUCT DATA, not configuration, so it is
    declared in committed code (product.organisations.SEEDED_ORGANISATIONS) and
    applied on every boot — a fresh database comes up complete, with no
    dashboard step and no one-off script to remember. Infrastructure reaching a
    stable product export at a composition point is the same move
    infrastructure/startup.py already makes for product.startup.STARTUP_TASKS;
    platform is the boundary that must never import product, which is why this
    lives here rather than in organisation_service.

    Idempotent and ADD-ONLY, like the admin seeder, in two distinct senses:

    - The organisation is got-or-created by SLUG and its grants are UNIONED, so
      a row created earlier by another route is ADOPTED rather than duplicated,
      and a capability someone granted by hand is never stripped at boot. Its
      PRESENTATION fields (name, website_url, logo_url) do follow the
      declaration, which is the only thing that writes them — see
      _seed_one_organisation for why the three kinds of state differ.
    - A partnership is created only when the organisation has no row for that
      SEASON yet. An existing row is left completely alone — never re-statused,
      never re-dated — because status and dates are changed through the API by
      operators and agents, and a boot must not undo their decisions.

    Failures are per-organisation: one bad declaration is logged and skipped
    rather than taking the rest of the seeding (or the boot) down with it.
    """
    if not SEEDED_ORGANISATIONS:
        logger.info("No organisations declared for seeding; skipping.")
        return

    for declaration in SEEDED_ORGANISATIONS:
        try:
            _seed_one_organisation(db, declaration)
        except Exception:
            db.rollback()
            logger.exception("Failed to seed organisation '%s'.", declaration.get("slug"))


def _seed_one_organisation(db: Session, declaration: dict) -> None:
    """Seeds one declared organisation and every partnership it declares.

    Adoption treats the three kinds of state on an existing row differently, and
    the differences are the whole design:

    - PRESENTATION (name, website_url, logo_url) FOLLOWS THE DECLARATION. No API
      writes these, so the committed declaration is their only author and a
      difference is drift, not somebody's decision — and a stale name is served
      straight to the sponsor's own dashboard as `organisation_name`.
    - GRANTS UNION, never subtract: a capability granted by hand survives boot.
    - PARTNERSHIP STATE IS NEVER TOUCHED (see _seed_one_partnership): status and
      dates are exactly what operators and agents change through the API.

    The reconcile and grants steps are each isolated so neither can take the
    partnership loop down with it — a stray non-conferrable grant on an adopted
    row makes the union raise, and before this was isolated that error escaped
    past every partnership in the declaration on every single boot."""
    slug = declaration["slug"]
    granted = list(declaration.get("granted_capabilities") or [])

    organisation = orgs.get_organisation(db, slug)
    if organisation is None:
        organisation = orgs.create_organisation(
            db, slug, declaration["name"],
            website_url=declaration.get("website_url"),
            logo_url=declaration.get("logo_url"),
            granted_capabilities=granted,
        )
        logger.info("Seeded fresh organisation '%s'.", slug)
    else:
        # Adopting the existing row (production already holds one created by an
        # earlier one-off job):
        _reconcile_presentation_fields(db, organisation, declaration)
        _union_granted_capabilities(db, organisation, granted)

    for partnership in declaration.get("partnerships") or []:
        _seed_one_partnership(db, organisation, partnership)


def _reconcile_presentation_fields(db: Session, organisation: Organisation, declaration: dict) -> None:
    """Brings an adopted row's presentation fields back in line with the declaration.

    Only the fields NOTHING ELSE WRITES: with no API surface for them, the
    declaration is their single author, so a difference can only be drift from a
    hand-made row or an older declaration. `website_url` and `logo_url` are
    reconciled only when the declaration states them — an absent key is "not
    declared", not "clear it"."""
    changes: dict[str, object] = {"name": declaration["name"]}
    for field in ("website_url", "logo_url"):
        if declaration.get(field) is not None:
            changes[field] = declaration[field]

    changed = [field for field, value in changes.items() if getattr(organisation, field) != value]
    if not changed:
        return

    try:
        for field in changed:
            setattr(organisation, field, changes[field])
        db.add(organisation)
        db.commit()
        db.refresh(organisation)
        logger.info("Reconciled organisation '%s' presentation field(s): %s.",
                    organisation.slug, ", ".join(changed))
    except Exception:
        db.rollback()
        logger.exception("Failed to reconcile presentation fields for organisation '%s'.", organisation.slug)


def _union_granted_capabilities(db: Session, organisation: Organisation, granted: list[str]) -> None:
    """Adds the declared grants onto an adopted row, order preserved, never subtracting.

    Isolated from the rest of seeding on purpose. set_granted_capabilities
    validates the WHOLE list it is handed, including what the row already held,
    so one non-conferrable string somebody left on the row makes the union raise
    forever. That is a real problem worth an error event — it is not a reason for
    this organisation's partnerships to go unseeded on every boot."""
    existing = list(organisation.granted_capabilities or [])
    missing = [capability for capability in granted if capability not in existing]
    if not missing:
        return

    try:
        orgs.set_granted_capabilities(db, organisation, existing + missing)
        logger.info("Adopted organisation '%s' and added %d conferred grant(s).",
                    organisation.slug, len(missing))
    except Exception:
        db.rollback()
        logger.exception("Failed to add conferred grant(s) to organisation '%s'; "
                         "continuing with its partnerships.", organisation.slug)


def _seed_one_partnership(db: Session, organisation: Organisation, declaration: dict) -> None:
    """Creates one declared partnership, unless that season already has a row"""
    season = declaration["season"]

    if ps.find_partnership_for_season(db, organisation, season) is not None:
        # Deliberately not comparing fields: an existing row is the operator's,
        # and reconciling it against the declaration would undo exactly the
        # API-driven changes this seeder must preserve:
        logger.info("Organisation '%s' already has a %s partnership; leaving it untouched.",
                    organisation.slug, season)
        return

    ps.create_partnership(
        db, organisation,
        season=season,
        status=declaration.get("status"),
        offering_label=declaration.get("offering_label"),
        snapshot_key=declaration.get("snapshot_key"),
        starts_on=declaration.get("starts_on"),
        ends_on=declaration.get("ends_on"),
        signed_on=declaration.get("signed_on"),
    )
    logger.info("Seeded %s partnership for organisation '%s'.", season, organisation.slug)
