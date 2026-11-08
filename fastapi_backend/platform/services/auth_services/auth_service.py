import logging
import uuid
from datetime import datetime, timedelta, UTC

from fastapi import Request, Response
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy import select, func, nullsfirst
from sqlalchemy.orm import Session

from platform.exceptions import (
    InvalidCredentialsException,
    PasswordLoginUnavailableException,
    RefreshTokenMissingException,
    RefreshTokenInvalidException,
    RefreshTokenReuseException,
)
from platform.models import RefreshSession, User
from platform.services import user_service as us
from security import (
    bcrypt_context,
    decode_token,
    create_access_token,
    create_refresh_token,
    REFRESH_COOKIE_NAME,
    TOKEN_TYPE,
    set_all_auth_cookies,
    clear_all_auth_cookies,
    validate_csrf,
)
from security.cookies import _set_refresh_cookie, _set_csrf_cookie, _set_session_hint_cookie
from platform.enums import TokenUse
from platform.schemas import DualTokenResponse, AccessTokenResponse
from infrastructure.settings import settings as s


logger = logging.getLogger(__name__)


# ============================================================================
# AUTHENTICATION
# ============================================================================

def authenticate_user(db: Session, email: str, password: str):
    """Authenticates a user by email and password, verifying password against bcrypt hash"""
    user = us.get_user_by_email(db, email)
    if not user.password:
        raise PasswordLoginUnavailableException
        
    if not bcrypt_context.verify(password, user.password):
        raise InvalidCredentialsException
    return user


def login_and_generate_token(db: Session, auth_form: OAuth2PasswordRequestForm, request: Request, response: Response) -> DualTokenResponse:
    """Authenticates user credentials and generates new session with access and refresh tokens"""
    # auth_form is of type OAuth2PasswordRequestForm, so it has attributes username and password.
    # In this case, username represents the user's email:
    user = authenticate_user(db, auth_form.username, auth_form.password)
    
    session, access, rt = create_session_and_tokens(db, user.id)
    
    set_all_auth_cookies(response, rt["refresh_token"], rt["refresh_token_expiry"])

    result = {
        "access_token": access["access_token"],
        "access_token_expiry": access["access_token_expiry"],
        "refresh_token_expiry": rt["refresh_token_expiry"],
        "token_type": TOKEN_TYPE,
    }

    # Header-mode clients (cross-site previews, native shells) cannot use the
    # httpOnly refresh cookie; they opt in explicitly and carry the refresh
    # token themselves. Possession of the signed token is the credential.
    if _wants_header_auth(request):
        result["refresh_token"] = rt["refresh_token"]

    return result


# ============================================================================
# TOKEN REFRESH
# ============================================================================

def _wants_header_auth(request: Request) -> bool:
    """Whether the client explicitly opted into header-mode auth."""
    return (request.headers.get("X-Auth-Mode") or "").strip().lower() == "header"


def _bearer_refresh_token(request: Request) -> str | None:
    """Extracts a refresh token presented as a Bearer credential (header mode)."""
    auth_header = (request.headers.get("Authorization") or "").strip()
    if auth_header.lower().startswith("bearer "):
        return auth_header[7:].strip() or None
    return None


def refresh_access_token(db: Session, request: Request, response: Response) -> AccessTokenResponse:
    """Refreshes the access token with automatic rotation.

    Two transports: the httpOnly refresh cookie (CSRF-validated - it is sent
    ambiently), or a Bearer refresh token for header-mode clients (no CSRF
    needed: nothing ambient, possession of the signed token is the
    credential; the rotated refresh token returns in the response body).
    """
    raw_rt = request.cookies.get(REFRESH_COOKIE_NAME)
    bearer_rt = None if raw_rt else _bearer_refresh_token(request)

    if raw_rt:
        # CSRF check applies to the ambient cookie transport only:
        validate_csrf(request)
    elif not bearer_rt:
        raise RefreshTokenMissingException

    payload = decode_token(raw_rt or bearer_rt, expected_use=TokenUse.REFRESH)

    user_id = int(payload.get("sub"))
    jti = payload.get("jti")
    sid = payload.get("sid")

    if not jti or not sid:
        raise RefreshTokenInvalidException

    # Transactional rotation with row lock:
    try:
        now = datetime.now(UTC)
        session_row = get_session_by_id(db, int(sid), user_id, lock=True)

        if not session_row or session_row.expires_at < now:
            raise RefreshTokenInvalidException

        # Accepting current token match:
        if session_row.current_jti == jti:
            # Rotating:
            new_rt = rotate_refresh_token(db, session_row, user_id, sid)
            
            if bearer_rt:
                # Header mode: the rotated refresh token travels in the body -
                # there is no cookie jar to put it in.
                access = create_access_token(user_id, sid=sid)
                access["refresh_token"] = new_rt["refresh_token"]
                access["refresh_token_expiry"] = new_rt["refresh_token_expiry"]
                return access

            # Setting cookies for new token:
            _set_refresh_cookie(response, new_rt["refresh_token"], new_rt["refresh_token_expiry"]) 
            # Rotating CSRF token along with refresh:
            _set_csrf_cookie(response, uuid.uuid4().hex, new_rt["refresh_token_expiry"])
            # Refresh session hint cookie to keep session active:
            _set_session_hint_cookie(response, new_rt["refresh_token_expiry"]) 
            # Return new access
            return create_access_token(user_id, sid=sid)

        # Grace: allowing previous token for small window (handles near-simultaneous refreshes):
        if (
            session_row.previous_jti
            and session_row.previous_valid_until
            and session_row.previous_valid_until > now
            and session_row.previous_jti == jti
        ):
            # Treating as already rotated: the concurrent caller simply gets an
            # access token for the session the winner just rotated.
            #
            # The window is TIME-bounded, deliberately not single-use. Closing
            # it on the first grace hit looks safer and is not: a browser
            # restoring N tabs fires N refreshes off the same cookie, the row
            # lock serialises them, and the third one would land on "reuse" -
            # revoking a session nobody attacked. Since revocation now kills
            # that session's access tokens instantly, the cost of that false
            # positive is every tab bounced to login at once.
            #
            # What a stolen previous-jti BUYS is unchanged: this branch returns
            # an access token and nothing else - never a rotated refresh token,
            # never a later `expires_at` - so a second grace hit grants nothing
            # the first did not, and the time bound is identical either way.
            # What is genuinely weaker is DETECTION: a replay inside the window
            # after a legitimate tab already used it used to trip reuse
            # detection, and now does not. That is the trade being made - a 30
            # second detection window, against signing every tab out of a
            # healthy session on an ordinary browser restore.
            update_session_last_used(db, session_row)
            return create_access_token(user_id, sid=sid)

        # Anything else is reuse/invalid:
        logger.info("refresh_token: invalid/reuse detected; revoking sid=%s", sid)
        revoke_session(db, session_row)
        clear_all_auth_cookies(response)
        raise RefreshTokenReuseException

    except Exception:
        db.rollback()
        raise


# ============================================================================
# LOGOUT
# ============================================================================

def logout(db: Session, request: Request, response: Response) -> None:
    """Logs out user by revoking their refresh session and clearing all authentication cookies"""
    # If no refresh cookie is present, honouring a header-mode bearer refresh
    # token so those sessions revoke server-side too; else just clearing state:
    raw_rt = request.cookies.get(REFRESH_COOKIE_NAME)
    if not raw_rt:
        raw_rt = _bearer_refresh_token(request)
        if not raw_rt:
            clear_all_auth_cookies(response)
            return
    else:
        validate_csrf(request)
    
    try:
        payload = decode_token(raw_rt, expected_use=TokenUse.REFRESH)
        user_id = int(payload.get("sub"))
        jti = payload.get("jti")
        sid = payload.get("sid")
        
        row = db.execute(
            select(RefreshSession).where(
                RefreshSession.id == int(sid),
                RefreshSession.user_id == user_id,
                RefreshSession.revoked_at.is_(None),
            )
        ).scalars().first()
        
        if row:
            row.revoked_at = datetime.now(UTC)
            db.add(row)
            db.commit()
    except Exception:
        db.rollback()
    
    clear_all_auth_cookies(response)


# ============================================================================
# SESSION MANAGEMENT
# ============================================================================

def create_session_and_tokens(db: Session, user_id: int) -> tuple[RefreshSession, dict, dict]:
    """Creates a new refresh session and generates access + refresh tokens, enforcing session limit per user"""
    now = datetime.now(UTC)

    # Serialising concurrent logins for THIS user on the user row before
    # counting. Without it the cap is advisory: count and insert are separate
    # statements, so N simultaneous logins all read the same pre-insert count,
    # all conclude there is room, and all insert. Measured at 20 concurrent
    # logins against a cap of 10, that lands 15 live sessions - a session
    # exhaustion limit that stops holding exactly when it is being attacked.
    #
    # A row lock rather than counting differently, because the problem is a
    # phantom insert, which no predicate over the existing rows can see. Logins
    # are rare and this only serialises one account's own logins. Nothing takes
    # these locks in the opposite order: the refresh path locks refresh_sessions
    # and never users, so there is no cycle to deadlock on.
    db.execute(select(User.id).where(User.id == user_id).with_for_update())

    # Enforcing session limit per user to prevent session exhaustion attacks:
    active_sessions_count = db.execute(
        select(func.count(RefreshSession.id)).where(
            RefreshSession.user_id == user_id,
            RefreshSession.revoked_at.is_(None),
            RefreshSession.expires_at > now
        )
    ).scalar()
    
    # Retiring oldest-first until there is room for the one about to be created.
    #
    # A LOOP, not a single eviction: one-per-login only converges if the account
    # was exactly at the cap, so an account already over it - which the previous
    # eviction bug could produce, and which a change to MAX_SESSIONS_PER_USER
    # produces for everyone - would stay over it forever, retiring one session
    # per login and gaining one.
    #
    # The candidate filter matches the count's definition of "active" exactly.
    # When it did not, an expired-but-unrevoked row (invisible to the count)
    # could be picked instead of a live one, so that login retired nothing that
    # mattered and the live count came out one over the cap.
    surplus = active_sessions_count - s.MAX_SESSIONS_PER_USER + 1
    if surplus > 0:
        stale_first = db.execute(
            select(RefreshSession).where(
                RefreshSession.user_id == user_id,
                RefreshSession.revoked_at.is_(None),
                RefreshSession.expires_at > now,
            # A row that has never been used is the stalest thing there is, but
            # PostgreSQL sorts NULLs LAST on ASC, which would make it the very
            # last candidate instead of the first:
            ).order_by(nullsfirst(RefreshSession.last_used_at.asc()))
            .limit(surplus)
        ).scalars().all()

        for row in stale_first:
            logger.info(f"Session limit reached for user {user_id}; revoking oldest session {row.id}")
            row.revoked_at = now
            db.add(row)
    
    session = RefreshSession(
        user_id=user_id,
        current_jti="",
        previous_jti=None,
        previous_valid_until=None,
        expires_at=now,
        last_used_at=now,
    )
    db.add(session)
    db.flush()

    sid = str(session.id)
    access = create_access_token(user_id, sid=sid)
    rt = create_refresh_token(user_id, sid=sid)

    session.current_jti = rt["jti"]
    session.expires_at = rt["refresh_token_expiry"]
    session.last_used_at = now
    db.add(session)
    db.commit()

    return session, access, rt


def rotate_refresh_token(db: Session, session_row: RefreshSession, user_id: int, sid: str) -> dict:
    """Rotates a refresh token within an existing session, preserving previous JTI for grace window"""
    now = datetime.now(UTC)
    
    new_rt = create_refresh_token(user_id, sid=sid)
    
    # Moving current -> previous for grace window:
    session_row.previous_jti = session_row.current_jti
    session_row.previous_valid_until = now + timedelta(seconds=s.REFRESH_PREVIOUS_JTI_GRACE_SECONDS)
    session_row.current_jti = new_rt["jti"]
    session_row.expires_at = new_rt["refresh_token_expiry"]
    session_row.last_used_at = now
    
    db.add(session_row)
    db.commit()
    
    return new_rt


def update_session_last_used(db: Session, session_row: RefreshSession) -> None:
    """Stamps a session as used, leaving the rotation grace window alone.

    It deliberately does NOT pull `previous_valid_until` back to now: the grace
    window is bounded by time, not by a single use (see the grace branch in
    `refresh_access_token` for why closing it early revokes healthy sessions).
    """
    session_row.last_used_at = datetime.now(UTC)
    db.add(session_row)
    db.commit()


def revoke_session(db: Session, session_row: RefreshSession) -> None:
    """Revokes a specific session by setting its revoked_at timestamp"""
    now = datetime.now(UTC)
    session_row.revoked_at = now
    db.add(session_row)
    db.commit()


def revoke_all_user_sessions(db: Session, user_id: int) -> None:
    now = datetime.now(UTC)
    sessions = db.query(RefreshSession).filter(
        RefreshSession.user_id == user_id,
        RefreshSession.revoked_at.is_(None)
    ).all()
    
    for s in sessions:
        s.revoked_at = now
    
    if sessions:
        db.commit()


def get_session_by_id(db: Session, session_id: int, user_id: int, lock: bool = False) -> RefreshSession | None:
    """Retrieves a session by ID with optional row lock for atomic token rotation.

    Unexpired is NOT part of the filter, because the rotation path needs the row
    itself - to read `current_jti` / `previous_jti` and to rotate it - and it
    checks expiry separately against the same locked row. Callers that only need
    a yes/no should use `is_session_live`, which owns the full definition of a
    usable session.
    """
    query = select(RefreshSession).where(
        RefreshSession.id == session_id,
        RefreshSession.user_id == user_id,
        RefreshSession.revoked_at.is_(None),
    )

    if lock:
        query = query.with_for_update()

    return db.execute(query).scalars().first()


# A `sid` claim wider than a 64-bit signed integer is a malformed token, and
# SQLite raises OverflowError rather than simply matching nothing - which the
# test suite runs on, so an absurd claim would be a 500 there instead of a 401.
# PostgreSQL (the production target, where the key is a plain INTEGER) just
# returns no rows at any magnitude, so on the real dialect this guard is
# belt-and-braces: it costs one comparison and removes a dialect difference from
# a security check.
_MAX_SESSION_ID = 2 ** 63 - 1


def is_session_live(db: Session, session_id: int, user_id: int) -> bool:
    """Whether a session exists, belongs to this user, and is neither revoked nor expired.

    The single definition of a usable session, so the access-token check and the
    refresh path cannot drift apart. One indexed primary-key lookup; nothing is
    cached (auth/session state is DB-backed by design - see the caching rule).
    """
    if not (0 < session_id <= _MAX_SESSION_ID):
        return False

    row = db.execute(
        select(RefreshSession.id).where(
            RefreshSession.id == session_id,
            RefreshSession.user_id == user_id,
            RefreshSession.revoked_at.is_(None),
            RefreshSession.expires_at > datetime.now(UTC),
        )
    ).first()

    return row is not None

