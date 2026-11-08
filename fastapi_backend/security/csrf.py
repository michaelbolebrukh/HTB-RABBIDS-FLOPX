import re
from urllib.parse import urlparse

from fastapi import Request

from platform.exceptions import CSRFException
from infrastructure.settings import settings as s

# Importing from same module:
from .cookies import CSRF_COOKIE_NAME


# ============================================================================
# ORIGIN VALIDATION
# ============================================================================

def origin_matches_suffix(origin: str, allowed_suffix: str) -> bool:
    """Checks if an origin matches the allowed suffix pattern"""
    try:
        hostname = (urlparse(origin).hostname or "").lower()
        normalized_suffix = allowed_suffix.replace("https://", "").replace("http://", "").lstrip(".").lower()
        return hostname.endswith(normalized_suffix)
    except Exception:
        return False


def is_origin_allowed(origin: str) -> bool:
    """Checks if an origin is in the allowed list or matches allowed patterns"""
    if not origin: return False
    try:
        if origin in s.FRONTEND_ORIGINS_LIST:
            return True
        if s.ALLOWED_ORIGIN_SUFFIX and origin_matches_suffix(origin, s.ALLOWED_ORIGIN_SUFFIX):
            return True
        if s.ALLOWED_ORIGIN_REGEX_COMPUTED and re.match(s.ALLOWED_ORIGIN_REGEX_COMPUTED, origin):
            return True
        return False
    except Exception:
        return False


def is_allowed_frontend_origin(candidate: str) -> bool:
    """Checks if a candidate URL is an allowed frontend origin"""
    try:
        p = urlparse(candidate)
        if not (p.scheme and p.netloc):
            return False
        if is_origin_allowed(candidate):
            return True
        if p.hostname in s.FRONTEND_HOSTS:
            return True
        return False
    except Exception:
        return False


# ============================================================================
# PATH & URL UTILITIES
# ============================================================================

def sanitize_next_path(path: str | None) -> str:
    """Sanitizes and validates a 'next' path parameter to prevent open redirects"""
    if not isinstance(path, str):
        return "/"
    p = path.strip()
    if p.startswith("//"):
        return "/"
    if not p.startswith("/"):
        p = "/" + p
    p = p.replace("\\", "/")
    p = re.sub(r'/+', '/', p)
    if ".." in p:
        return "/"
    return p or "/"


# ============================================================================
# CSRF VALIDATION
# ============================================================================

def validate_csrf(request: Request) -> None:
    """Validates CSRF protection via origin check or double-submit cookie pattern"""
    # Accepting either allowed Origin OR double-submit cookie/header:
    expected = request.cookies.get(CSRF_COOKIE_NAME)
    provided = request.headers.get("X-CSRF-Token")

    origin = (request.headers.get("origin") or "").strip()

    if is_origin_allowed(origin) or (expected and provided and provided == expected):
        return

    raise CSRFException


def build_error_redirect_url(error_type: str, path: str = "/login") -> str:
    """Builds a frontend redirect URL with an error query parameter"""
    frontend_base = s.FRONTEND_ORIGINS_LIST[0] if s.FRONTEND_ORIGINS_LIST else None
    if frontend_base:
        return f"{frontend_base}{path}?error={error_type}"
    return f"{path}?error={error_type}"

