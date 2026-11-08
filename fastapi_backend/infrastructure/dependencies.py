import logging
from datetime import datetime, timedelta, UTC
from typing import Annotated, Optional, Generator

from fastapi import Depends, Request
from fastapi.security import OAuth2PasswordRequestForm, OAuth2PasswordBearer
from fastapi.security.base import SecurityBase
from sqlalchemy.orm import Session, selectinload

from platform.exceptions import JWTException, UserNotFoundException, CapabilityException, UnverifiedUserException
from infrastructure.database import SessionLocal
from infrastructure.observability import set_user
from infrastructure.service_auth import bearer_from_authorization, get_service_account, match_token
from platform.capabilities import ALL_CAPABILITIES, FEATURE_PREVIEW
from platform.models import OrganisationMember, User
from platform.services.auth_services.auth_service import is_session_live
from platform.services.organisation_service import effective_capabilities
from security import decode_token
from platform.enums import TokenUse


logger = logging.getLogger(__name__)


# How stale users.last_accessed_at is allowed to get before a request pays for a
# write. A code default, not an env var (config-minimalism): nothing about this
# number is per-instance, and a wrong value is a code change, not an outage.
#
# Five minutes is the whole reason the feature is affordable. Stamping on every
# request would turn every authenticated READ in the app into a write — the read
# path is the hot path — and would serialise concurrent requests from the same
# account on one row. At five minutes, a user browsing all day costs ~12 writes
# an hour and the column is still accurate to within five minutes, which is far
# finer than any "who has been active" question needs.
LAST_ACCESSED_THROTTLE = timedelta(minutes=5)


# Using a generator as context manager to manage the DB session:
def get_db() -> Generator[Session, None, None]:
    """Provides a database session with automatic cleanup and rollback on errors"""

    # Creating a new session to connect to the DB, using the session factory:
    db = SessionLocal()
    try:
        # Providing a DB session to the caller:
        yield db
    
    except Exception:
        # Rolling back any changes if an error occurs:
        db.rollback()
        raise
    
    finally:
        # This code only runs after the function calling get_db completes, 
        # allowing the connection to be closed when no longer needed (and not too soon):
        db.close()


# Dependency injection - if an object of type db_dependency (which is of type Session) is not provided, 
# FastAPI will automatically call get_db to obtain a DB session:
db_dependency = Annotated[Session, Depends(get_db)]


# For the authentication dependency, the OAuth2PasswordRequestForm class must be instantiated:
auth_dependency = Annotated[OAuth2PasswordRequestForm, Depends(OAuth2PasswordRequestForm)]


# Every OAuth2PasswordBearer below shares one tokenUrl, on purpose: FastAPI
# merges same-named security schemes into a single OpenAPI entry with a silent
# last-writer-wins dict update, so two instances whose URLs drifted apart would
# publish whichever happened to register last. One constant makes divergence a
# one-line edit that changes all of them together (and the OpenAPI test asserts
# same-named schemes agree, so a stray inline URL fails loudly):
_TOKEN_URL = "auth/token"

# Instantiating OAuth2PasswordBearer with the token-generating endpoint URL as a kwarg:
token_dependency = Annotated[str, Depends(OAuth2PasswordBearer(tokenUrl=_TOKEN_URL))]

# Variant that yields None instead of erroring when no bearer token is supplied,
# for endpoints that serve both anonymous and signed-in users. Hoisted to a
# named instance so BearerOrServiceToken below can adopt its model and scheme
# name — sharing the objects keeps those two declarations identical by
# construction:
_oauth2_password_scheme = OAuth2PasswordBearer(tokenUrl=_TOKEN_URL, auto_error=False)
optional_token_dependency = Annotated[Optional[str], Depends(_oauth2_password_scheme)]


def stamp_last_accessed(db: Session, user: User) -> None:
    """Records that this account just made a request — at most once per throttle window.

    Called from the ONE dependency every authenticated request already resolves
    through (get_user), so coverage is by construction: every current and future
    authenticated endpoint stamps, and no router ever has to remember to.

    Two properties matter more than the value itself:

    - It is usually a NO-OP. The freshness test compares in Python against the
      user row this request already loaded, so a fresh stamp costs zero queries;
      only a stale one issues an UPDATE.
    - It can never fail a real request. Recording an access is bookkeeping; a
      user's actual call must not 500 because the bookkeeping write lost a race
      or hit a dead connection. Any failure is rolled back and logged at WARNING
      (degraded-but-handled, the same contract the cache layer fails open with)
      rather than raised.

    One consequence to know about before caching anything derived from `users`:
    this commits through the ORM session, so it bumps the `users` cache tag. No
    cached read is tagged `users` today; one added later would be evicted by
    every stamp and should use a TTL-only entry (see the caching rule).
    """
    now = datetime.now(UTC)
    previous = user.last_accessed_at
    if previous is not None:
        # Normalising: PostgreSQL hands back an aware datetime, SQLite a naive
        # one, and comparing the two raises. Stored values are always UTC:
        if previous.tzinfo is None:
            previous = previous.replace(tzinfo=UTC)
        if now - previous < LAST_ACCESSED_THROTTLE:
            return

    try:
        # A bulk UPDATE by primary key rather than a mutation of the loaded
        # instance: one statement, no flush of anything else the session happens
        # to hold, and no dependence on the ORM's change detection:
        db.query(User).filter(User.id == user.id).update(
            {User.last_accessed_at: now}, synchronize_session=False,
        )

        # Committing WITHOUT expiring the session's instances. The default
        # expire-on-commit would discard the user this request just loaded along
        # with its eagerly-loaded memberships, so the very next attribute access
        # would re-SELECT the row and lazy-load the relation the capability check
        # needs — reintroducing on the hottest path exactly the N+1 that
        # get_user's selectinload exists to prevent:
        expire_on_commit = db.expire_on_commit
        db.expire_on_commit = False
        try:
            db.commit()
        finally:
            db.expire_on_commit = expire_on_commit

        # Keeping the in-memory row consistent with the database, so anything
        # later in this request reads the value it just wrote:
        user.last_accessed_at = now

    except Exception:
        # Nothing else is pending at auth-resolution time, so this rollback
        # discards only the failed stamp:
        db.rollback()
        logger.warning("Failed to stamp last_accessed_at for user %s", user.id, exc_info=True)


def get_user(db: db_dependency, user_id: int, require_verification: bool = True) -> User:
    """Retrieves a user by ID with optional verification requirement.

    Memberships and their organisations are EAGER-loaded: capability checks
    resolve the effective set (direct grants + those conferred by each active
    organisation), so without this every gated request would lazy-load the
    relation — an N+1 the database rule forbids, and a load that would raise
    outright under raiseload discipline. One round-trip, on the hottest path
    in the app."""
    user = (
        db.query(User)
        .options(selectinload(User.organisation_memberships).selectinload(OrganisationMember.organisation))
        .filter_by(id=user_id)
        .first()
    )

    if user is None: raise UserNotFoundException
    if require_verification and not user.is_verified:
        # Getting current verification cooldown from user model:
        cooldown_seconds = user.get_verification_cooldown_seconds()
        raise UnverifiedUserException(cooldown_seconds=cooldown_seconds)

    # Attaching the user id (never email) to error-tracking events:
    set_user(user.id)

    # Recording the access, after the verification gate: "last accessed" means
    # the last request this account actually got to make, not one that bounced:
    stamp_last_accessed(db, user)
    return user


def assert_session_live(db: Session, sid: Optional[str], user_id: int) -> None:
    """Rejects an access token whose session has been revoked or has expired.

    Signing out revokes the session row, but an access token is a self-contained
    signature: nothing about revocation reaches it. Without this check a token
    minted moments before a sign-out stayed fully privileged - reads AND
    mutations, every capability-gated endpoint - for the remainder of its TTL.
    That is what let a second tab keep working after the user signed out in the
    first, and it is why "sign out everywhere" could not be built on top of the
    revoke alone.

    The cost is one extra indexed primary-key lookup per authenticated request -
    a second query, not a merged one - in exchange for revocation meaning
    something. Session state is never cached (see the caching rule).

    Fail-CLOSED on a missing claim: `create_access_token` requires a `sid`, so
    there is no such thing as a legitimate access token without one, and
    admitting it would make any future mint site that forgot the argument
    silently unrevocable. Tokens are HMAC-signed, so the claim cannot be
    stripped to dodge the check.
    """
    try:
        session_id = int(sid)
    except (TypeError, ValueError):
        # A missing or malformed claim is a malformed token, not a free pass.
        # `int()` accepts a few shapes a mint site would never produce
        # (" 1 ", "+1", "1_0") - that is fine, this is not a sanitiser: the
        # claim is HMAC-signed and every mint site writes str(session.id). It
        # only has to fail closed on absence.
        raise JWTException from None

    if not is_session_live(db, session_id, user_id):
        raise JWTException


def get_current_user(db: db_dependency, token: token_dependency, require_verification: bool = True) -> User:
    """Extracts and validates user from JWT access token"""
    # Attempting to decode the token using the secret key and algorithm:
    # (If successful, this will return a dictionary that contains the user data):
    payload = decode_token(token, expected_use=TokenUse.ACCESS)

    # Extracting the user ID from the payload dictionary:
    # The subject of a JWT needs to be a string, so converting back to integer:
    user_id: Optional[int] = payload.get("sub")

    if user_id is None: raise JWTException

    # A valid signature is not enough: the session behind it must still be live.
    assert_session_live(db, payload.get("sid"), int(user_id))

    return get_user(db, int(user_id), require_verification)


def get_current_verified_user(db: db_dependency, token: token_dependency) -> User:
    """Gets current user, requiring verified status"""
    return get_current_user(db, token, require_verification=True)


def get_current_unverified_user(db: db_dependency, token: token_dependency) -> User:
    """Gets current user, allowing unverified status"""
    return get_current_user(db, token, require_verification=False)


# Plain session-user dependencies: these resolve session JWTs only. Service
# tokens work ONLY through require_capability gates below — a machine credential
# never impersonates "the current user" on an ungated endpoint:
user_dependency = Annotated[User, Depends(get_current_verified_user)]
unverified_user_dependency = Annotated[User, Depends(get_current_unverified_user)]


def get_current_user_or_none(db: db_dependency, token: optional_token_dependency) -> Optional[User]:
    """Resolves the signed-in user when a valid token is present; anonymous requests pass through"""
    if not token:
        return None
    try:
        return get_current_user(db, token, require_verification=False)
    except Exception:
        # Treating stale/invalid tokens as anonymous rather than failing the request:
        return None


optional_user_dependency = Annotated[Optional[User], Depends(get_current_user_or_none)]


def verify_capability(user: User, capability: str) -> None:
    """Verifies a named capability grant (registry: platform/capabilities.py).

    Admin is the catch-all identity and implicitly holds every capability; any
    other account holds its EFFECTIVE set — grants listed on its row plus those
    conferred by each active organisation it belongs to. Conferral is computed
    (organisation_service.effective_capabilities is the one definition, shared
    with User.has_capability), never materialised, so a membership change takes
    effect on the very next request. See the access-control rule.
    """
    if user.is_admin:
        return
    if capability not in effective_capabilities(user):
        raise CapabilityException


class BearerOrServiceToken(SecurityBase):
    """Resolves the Authorization header, and says so in the OpenAPI document.

    Capability-gated endpoints accept two unrelated credentials on one header: a
    session JWT, and an opaque SERVICE_TOKENS value that no OAuth2 flow can mint.
    So the header has to be read directly rather than through a stock scheme —
    and a hand-read header is invisible to FastAPI, which records a security
    requirement only when a SecurityBase sits in the dependency graph. Every
    capability-gated operation was therefore published as needing no credentials
    at all: the gates ran exactly as always, but the document describing the most
    privileged surface in the app said the opposite, and the docs UI believed it.

    It declares itself as the SAME OAuth2 password scheme the rest of the API
    uses, by adopting the shared instance's model and scheme name outright. One
    Authorize login in the docs UI — email and password against auth/token —
    then covers every operation, gated and plain alike. Adopting the objects
    rather than re-declaring equivalent ones is load-bearing: FastAPI folds
    same-named schemes together with a last-writer-wins dict update, so two
    hand-maintained copies that drifted apart would merge silently into
    whichever registered last.

    Service tokens deliberately get no docs-UI affordance from this. The
    password dialog cannot carry an opaque token, and machine callers were
    never docs-UI users — they set the header themselves. Extraction stays
    delegated to bearer_from_authorization rather than reimplemented: FastAPI's
    own parse splits on a single space without stripping while ours strips, and
    a service token sent as "Bearer  <token>" must keep working.

    It cannot reject a request: an absent or unparseable header resolves to
    None, exactly as the manual read did, leaving require_capability the only
    gate. And never populate the scheme's scopes — OAuth2 scopes would be a
    second authorization axis competing with the capability registry.
    """

    def __init__(self) -> None:
        self.model = _oauth2_password_scheme.model
        self.scheme_name = _oauth2_password_scheme.scheme_name

    async def __call__(self, request: Request) -> Optional[str]:
        return bearer_from_authorization(request.headers.get("Authorization"))


# The one instance: FastAPI keys securitySchemes by scheme_name, so every gate
# shares a single entry in the document:
capable_bearer_dependency = Annotated[Optional[str], Depends(BearerOrServiceToken())]


def require_capability(capability: str):
    """Builds THE authorization dependency: a real account holding the given grant.

    The single primitive every protected resource gates on. The bearer resolves
    to either the persisted service-account User row (when it matches a
    SERVICE_TOKENS entry — audit-logged by the token's fingerprint, never the
    token) or the verified user from the session JWT; the capability check then
    applies the same rule to both. Every caller is a real User with a real id,
    so FKs, set_user() observability, and audit attribution work identically
    for humans and machines.

    The grant string is validated against the registry HERE, at definition time.
    Dependency aliases are built at module import, so a typo is a startup crash
    rather than a silently admin-only endpoint: an unregistered string is held
    by no account, yet `verify_capability` still short-circuits on admin, so a
    mistyped gate would quietly narrow a surface to admins with nothing failing.
    """
    if capability not in ALL_CAPABILITIES:
        raise ValueError(
            f"Unknown capability {capability!r}; add it to platform/capabilities.py "
            f"(with its first real consumer) or fix the typo. "
            f"Known: {sorted(ALL_CAPABILITIES)}"
        )

    def get_current_capable_user(
        request: Request, db: db_dependency, bearer: capable_bearer_dependency,
    ) -> User:
        fingerprint = match_token(bearer)
        if fingerprint is not None:
            logger.info("Service action authorized (tid=%s) %s %s",
                        fingerprint, request.method, request.url.path)
            user = get_service_account(db)
            if user is None:
                # SERVICE_TOKENS is set but the account is not seeded yet:
                # failing closed rather than minting an identity per request.
                raise CapabilityException
            # Attaching the service account's id to error-tracking events, the
            # same as the session path does inside get_user:
            set_user(user.id)
            # Deliberately NOT stamping last_accessed_at here. The column answers
            # "when was this person last around", and the service account is a
            # single shared machine principal: any scheduled job pins it to
            # ~now forever, so the field would read as permanently active while
            # saying nothing about anyone. Leaving it null keeps the column's
            # meaning intact and keeps the machine path free of writes. The
            # per-token audit line above is already the record of service
            # activity, and it distinguishes tokens, which one column cannot:
        else:
            # Not a service token: a verified human session (empty/garbage -> 401):
            user = get_current_verified_user(db, bearer or "")
        verify_capability(user, capability)
        return user

    return Annotated[User, Depends(get_current_capable_user)]


def entitlement_dependency(feature: str):
    """Builds a dependency alias requiring an active Stripe entitlement.

    feature:preview holders bypass (admins pass implicitly via has_capability),
    so sponsor/demo preview accounts reach paid features without a purchase."""
    from platform.exceptions import EntitlementRequiredException
    from platform.services.payment_services import has_entitlement

    def get_current_entitled_user(db: db_dependency, token: token_dependency) -> User:
        user = get_current_verified_user(db, token)
        if user.has_capability(FEATURE_PREVIEW) or has_entitlement(user, feature):
            return user
        raise EntitlementRequiredException

    return Annotated[User, Depends(get_current_entitled_user)]
