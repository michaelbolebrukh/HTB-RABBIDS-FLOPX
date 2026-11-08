"""
Authentication Services
======================

This package handles all authentication-related functionality including:
- Standard email/password authentication with JWT tokens
- OAuth 2.0 / OIDC flows (Google, Apple)
- Session management with refresh token rotation
- CSRF protection via double-submit cookies
"""

from .auth_service import (
    authenticate_user,
    login_and_generate_token,
    refresh_access_token,
    logout,
    create_session_and_tokens,
    rotate_refresh_token,
    update_session_last_used,
    revoke_session,
    revoke_all_user_sessions,
    get_session_by_id,
    is_session_live,
)

from .oauth_service import (
    oauth_login,
    oauth_callback,
    list_available_providers,
    provider_registry,
    oauth,
)

__all__ = [
    # Auth service exports:
    "authenticate_user",
    "login_and_generate_token",
    "refresh_access_token",
    "logout",
    "create_session_and_tokens",
    "rotate_refresh_token",
    "update_session_last_used",
    "revoke_session",
    "revoke_all_user_sessions",
    "get_session_by_id",
    "is_session_live",
    # OAuth service exports:
    "oauth_login",
    "oauth_callback",
    "list_available_providers",
    "provider_registry",
    "oauth",
]

