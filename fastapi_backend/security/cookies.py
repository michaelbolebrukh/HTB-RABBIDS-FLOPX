import uuid
from datetime import datetime, UTC

from fastapi import Response

from infrastructure.settings import settings as s


# Cookie name constants:
REFRESH_COOKIE_NAME = "refresh_token"
CSRF_COOKIE_NAME = "csrf_token"
SESSION_HINT_COOKIE_NAME = "session_present"
COOKIE_PATH = "/auth"
TOKEN_TYPE = "bearer"


# ============================================================================
# COOKIES
# 1) refresh_token: httpOnly JWT for token rotation (scoped to /auth)
# 2) csrf_token: Readable by JS, used for CSRF (Cross Site Request Forgery) protection via X-CSRF-Token header
# 3) session_present: Hint cookie for frontend to detect authentication without parsing JWTs
# ============================================================================

def _set_refresh_cookie(response: Response, token: str, expires_at: datetime) -> None:
    """Sets refresh token as cookie with httpOnly, secure, and samesite attributes for token rotation, scoped to /auth path"""
    # Clearing any legacy/root-path cookie to avoid stale cookie precedence:
    try:
        response.delete_cookie(key=REFRESH_COOKIE_NAME, path="/")
    except Exception:
        pass
    response.set_cookie(
        key=REFRESH_COOKIE_NAME,
        value=token,
        httponly=True,
        secure=s.COOKIE_SECURE,
        samesite=s.COOKIE_SAMESITE,
        path=COOKIE_PATH,
        max_age=int((expires_at - datetime.now(UTC)).total_seconds()),
    )


def _set_csrf_cookie(response: Response, csrf_value: str, expires_at: datetime) -> None:
    """Sets CSRF token cookie for double-submit pattern, accessible to JavaScript for X-CSRF-Token header"""
    response.set_cookie(
        key=CSRF_COOKIE_NAME,
        value=csrf_value,
        httponly=False,
        secure=s.COOKIE_SECURE,
        samesite=s.COOKIE_SAMESITE,
        path="/",
        max_age=int((expires_at - datetime.now(UTC)).total_seconds()),
    )


def _set_session_hint_cookie(response: Response, expires_at: datetime) -> None:
    """Sets session hint cookie for client-side session state awareness, allowing frontend to detect authentication without parsing JWTs"""
    response.set_cookie(
        key=SESSION_HINT_COOKIE_NAME,
        value="1",
        httponly=False,
        secure=s.COOKIE_SECURE,
        samesite=s.COOKIE_SAMESITE,
        path="/",
        domain=s.SESSION_HINT_COOKIE_DOMAIN,
        max_age=int((expires_at - datetime.now(UTC)).total_seconds()),
    )


# ============================================================================
# COOKIE CLEANERS
# ============================================================================

def _expire_cookie(
    response: Response,
    key: str,
    path: str,
    domain: str | None = None,
    httponly: bool = True,
) -> None:
    """Expires one cookie variant with the SAME attributes it was set with.

    Starlette's `delete_cookie` defaults to `secure=False, samesite="lax"`, which
    does NOT mirror how these cookies are set (`secure=True` and the configured
    SameSite). That asymmetry is not cosmetic: under `COOKIE_SAMESITE=none` — the
    cross-site setup used by preview/native frontends — a browser REJECTS an
    expiring Set-Cookie that arrives without `SameSite=None; Secure` on a
    cross-site request, so the cookie survives the logout and the next page load
    silently restores the session (the "had to sign out twice" symptom). Setting
    an empty value with max-age 0 and matching attributes is what actually
    expires it:
    """
    response.set_cookie(
        key=key,
        value="",
        max_age=0,
        expires=0,
        path=path,
        domain=domain,
        httponly=httponly,
        secure=s.COOKIE_SECURE,
        samesite=s.COOKIE_SAMESITE,
    )


def _clear_refresh_cookie(response: Response) -> None:
    """Clears refresh token cookie"""
    _expire_cookie(response, REFRESH_COOKIE_NAME, path=COOKIE_PATH)
    # Clearing any legacy/root-path cookie to avoid stale cookie precedence:
    _expire_cookie(response, REFRESH_COOKIE_NAME, path="/")


def _clear_csrf_cookie(response: Response) -> None:
    # Set JS-readable, so expired the same way:
    _expire_cookie(response, CSRF_COOKIE_NAME, path="/", httponly=False)


def _clear_session_hint_cookie(response: Response) -> None:
    """Clears session hint cookie including domain-specific variants for cross-domain logout"""
    # Both variants are expired unconditionally: the backend sets this cookie
    # domain-scoped in production, while the frontend's own optimistic write is
    # host-only, so either (or both) can be present in the jar:
    _expire_cookie(response, SESSION_HINT_COOKIE_NAME, path="/", httponly=False)
    if s.SESSION_HINT_COOKIE_DOMAIN:
        _expire_cookie(
            response,
            SESSION_HINT_COOKIE_NAME,
            path="/",
            domain=s.SESSION_HINT_COOKIE_DOMAIN,
            httponly=False,
        )


# ============================================================================
# COOKIE MANAGEMENT UTILITIES
# ============================================================================

def set_all_auth_cookies(response: Response, refresh_token: str, expiry: datetime) -> None:
    """Sets all authentication-related cookies (refresh, CSRF, session hint) atomically for complete authentication state"""
    _set_refresh_cookie(response, refresh_token, expiry)
    _set_csrf_cookie(response, uuid.uuid4().hex, expiry)
    _set_session_hint_cookie(response, expiry)


def clear_all_auth_cookies(response: Response) -> None:
    """Clears all authentication-related cookies (refresh, CSRF, session hint) for complete logout"""
    _clear_refresh_cookie(response)
    _clear_csrf_cookie(response)
    _clear_session_hint_cookie(response)
