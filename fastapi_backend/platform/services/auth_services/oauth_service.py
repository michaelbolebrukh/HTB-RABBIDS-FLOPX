import logging
from typing import Optional, Dict, Any
from dataclasses import dataclass
from enum import Enum

from fastapi import Request, Response
from starlette.responses import RedirectResponse
from starlette.status import HTTP_303_SEE_OTHER
from authlib.integrations.starlette_client import OAuth, OAuthError
from sqlalchemy import func
from sqlalchemy.orm import Session

from infrastructure.service_auth import service_account_email
from platform.exceptions import OAuthProviderNotConfiguredException, UserExistsException
from platform.models import User
from platform.utils.name_format import format_display_name
from .auth_service import create_session_and_tokens
from security import (
    set_all_auth_cookies,
    sanitize_next_path,
    is_allowed_frontend_origin,
    build_error_redirect_url,
)


logger = logging.getLogger(__name__)


# ============================================================================
# OAUTH PROVIDER CONFIGURATION
# ============================================================================

class OAuthProviderType(Enum):
    GOOGLE = "google"
    APPLE = "apple"


@dataclass
class OAuthProviderConfig:
    """Configuration for an OAuth provider."""
    name: str
    display_name: str
    
    # Provider credentials:
    client_id: Optional[str]
    client_secret: Optional[str]
    
    # OAuth flow settings:
    server_metadata_url: Optional[str] = None  # For OIDC providers
    scopes: str = "openid email profile"
    
    # Feature flags:
    supports_pkce: bool = True


class OAuthProviderRegistry:
    """Registry for OAuth providers with automatic configuration."""
    
    def __init__(self, oauth_client):
        self.oauth = oauth_client
        self.providers: Dict[str, OAuthProviderConfig] = {}
        self._register_all_providers()
    
    def _register_all_providers(self):
        """Registers all configured OAuth providers from environment variables"""
        from infrastructure.settings import settings as s
        
        # Registering Google (OIDC):
        if s.GOOGLE_CLIENT_ID and s.GOOGLE_CLIENT_SECRET:
            self._register_google(s.GOOGLE_CLIENT_ID, s.GOOGLE_CLIENT_SECRET)
            logger.info("OAuth provider registered: Google")
        
        # Registering Apple (OIDC):
        if s.APPLE_CLIENT_ID and s.APPLE_CLIENT_SECRET:
            self._register_apple(s.APPLE_CLIENT_ID, s.APPLE_CLIENT_SECRET)
            logger.info("OAuth provider registered: Apple")
    
    def _register_google(self, client_id: str, client_secret: str):
        """Registers Google as an OAuth 2.0 provider using OpenID Connect (OIDC) discovery"""
        config = OAuthProviderConfig(
            name="google",
            display_name="Google",
            client_id=client_id,
            client_secret=client_secret,
            server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
            scopes="openid email profile",
            supports_pkce=True,
        )
        
        self.oauth.register(
            name=config.name,
            server_metadata_url=config.server_metadata_url,
            client_id=config.client_id,
            client_secret=config.client_secret,
            client_kwargs={"scope": config.scopes}
        )
        
        self.providers[config.name] = config
    
    def _register_apple(self, client_id: str, client_secret: str):
        """Registers Apple as an OAuth 2.0 provider using OIDC with form_post response mode"""
        config = OAuthProviderConfig(
            name="apple",
            display_name="Apple",
            client_id=client_id,
            client_secret=client_secret,
            server_metadata_url="https://appleid.apple.com/.well-known/openid-configuration",
            scopes="openid email name",
            supports_pkce=True,
        )
        
        self.oauth.register(
            name=config.name,
            server_metadata_url=config.server_metadata_url,
            client_id=config.client_id,
            client_secret=config.client_secret,
            client_kwargs={
                "scope": config.scopes,
                "response_mode": "form_post",  # Apple requires form_post
            }
        )
        
        self.providers[config.name] = config
    
    def get_provider(self, name: str) -> Optional[OAuthProviderConfig]:
        return self.providers.get(name)
    
    def list_enabled_providers(self) -> list[str]:
        return list(self.providers.keys())
    
    def is_provider_enabled(self, name: str) -> bool:
        return name in self.providers


# Initializing OAuth client and provider registry:
oauth = OAuth()
provider_registry = OAuthProviderRegistry(oauth)


# ============================================================================
# OAUTH USER MANAGEMENT
# ============================================================================

async def extract_user_info(oauth_client, request: Request, token: dict, provider_name: str) -> Optional[Dict[str, Any]]:
    """Extracts user information from OAuth provider's userinfo endpoint
    
    Uses the standard OIDC userinfo endpoint which is more reliable and
    consistent across providers than parsing ID tokens.
    """
    try:
        user_info = await oauth_client.userinfo(token=token)
        if user_info:
            return dict(user_info)
        return None
    except Exception as e:
        logger.error(f"Failed to extract user info from {provider_name}: {e}")
        return None


def find_or_create_oauth_user(db: Session, email: str, user_info: dict, provider_display_name: str) -> User:
    """Finds existing user by email or creates new verified user from OAuth provider data"""
    # Refusing the reserved service-account address as defense-in-depth: the
    # callback guards it before reaching here, but any future caller must not be
    # able to mint a verified, passwordless row at the service identity (which a
    # service token resolves to). Mirrors the registration-path guard:
    if email.strip().lower() == service_account_email():
        raise UserExistsException
    user = db.query(User).filter(func.lower(User.email) == email.lower()).first()

    # Provider-supplied avatar (OIDC "picture" claim), used only when the user
    # has none — never overwriting an uploaded or previously captured image:
    picture = user_info.get("picture") or None

    if not user:
        # Creating new user from OAuth data:
        user = User(
            # Deterministic casing on the provider-supplied name; moderation is
            # deliberately not applied to a trusted OAuth identity (the user can
            # still be renamed later via the moderated update path):
            name=format_display_name(user_info.get("name") or f"{provider_display_name} User"),
            email=email.lower(),
            is_verified=True,
            avatar_url=picture,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
    else:
        changed = False
        # Auto-verifying existing user when IdP asserts verified email:
        if not user.is_verified:
            user.is_verified = True
            changed = True
        # Backfilling an avatar only when the user does not already have one:
        if picture and not user.avatar_url:
            user.avatar_url = picture
            changed = True
        if changed:
            db.add(user)
            db.commit()

    return user


# ============================================================================
# OAUTH FLOW HANDLERS
# ============================================================================

async def oauth_login(request: Request, provider_name: str):
    """Initiates OAuth 2.0 login flow with PKCE (S256) for supported provider"""
    if not provider_registry.is_provider_enabled(provider_name):
        raise OAuthProviderNotConfiguredException(provider_name)
    
    provider = provider_registry.get_provider(provider_name)
    redirect_uri = request.url_for("oauth_callback", provider=provider_name)
    
    # Persisting optional next path in session (validated to be a relative path):
    next_param = (request.query_params.get("next") or "").strip()
    request.session["oauth_next"] = sanitize_next_path(next_param)
    
    # Persisting frontend base from Origin to build absolute redirect later:
    origin = (request.headers.get("origin") or "").strip()
    if origin and is_allowed_frontend_origin(origin):
        request.session["oauth_frontend_base"] = origin
    
    # Provider-specific flow setup:
    auth_params = {}
    
    if provider.supports_pkce:
        auth_params["code_challenge_method"] = "S256"
    
    # Initiating OAuth flow:
    oauth_client = getattr(oauth, provider_name)
    return await oauth_client.authorize_redirect(request, redirect_uri, **auth_params)


async def oauth_callback(db: Session, request: Request, response: Response, provider_name: str):
    """Processes OAuth 2.0 callback, exchanges authorization code for tokens, retrieves user info, and establishes authenticated session"""
    if not provider_registry.is_provider_enabled(provider_name):
        raise OAuthProviderNotConfiguredException(provider_name)
    
    provider = provider_registry.get_provider(provider_name)
    oauth_client = getattr(oauth, provider_name)
    
    # Exchanging code for token:
    try:
        token = await oauth_client.authorize_access_token(request)
    except OAuthError as e:
        logger.warning(f"OAuth error for {provider_name}: {e}")
        return RedirectResponse(url=build_error_redirect_url("oauth_failed"), status_code=HTTP_303_SEE_OTHER)
    
    # Extracting user info:
    user_info = await extract_user_info(oauth_client, request, token, provider_name)
    
    if not user_info:
        return RedirectResponse(url=build_error_redirect_url("oauth_failed"), status_code=HTTP_303_SEE_OTHER)
    
    # Cleaning up nonce from session (not needed when using userinfo endpoint):
    request.session.pop("oauth_oidc_nonce", None)
    
    # Validating email:
    email = user_info.get("email")
    if not email:
        return RedirectResponse(url=build_error_redirect_url("email_missing"), status_code=HTTP_303_SEE_OTHER)
    
    if user_info.get("email_verified") is not True:
        return RedirectResponse(url=build_error_redirect_url("email_unverified"), status_code=HTTP_303_SEE_OTHER)

    # Refusing the reserved service-account address on the OAuth path too (the
    # registration path guards it separately): an OAuth-created row is verified
    # and passwordless, so without this the seeder's password-bearing adopt
    # refusal would not fire and a service token could resolve to it. Failing as
    # a generic OAuth error so the reserved address is not enumerable:
    if email.strip().lower() == service_account_email():
        return RedirectResponse(url=build_error_redirect_url("oauth_failed"), status_code=HTTP_303_SEE_OTHER)

    # Finding or creating user:
    user = find_or_create_oauth_user(db, email, user_info, provider.display_name)
    
    # Creating session and tokens:
    session, access, rt = create_session_and_tokens(db, user.id)
    
    # Building redirect:
    next_path = sanitize_next_path(request.session.pop("oauth_next", "/"))
    candidate_base = request.session.pop("oauth_frontend_base", None)
    
    from infrastructure.settings import settings as s
    frontend_base = candidate_base if (isinstance(candidate_base, str) and is_allowed_frontend_origin(candidate_base)) else (s.FRONTEND_ORIGINS_LIST[0] if s.FRONTEND_ORIGINS_LIST else None)
    
    redirect_url = f"{frontend_base}/oauth/success?next={next_path}" if frontend_base else next_path
    redirect = RedirectResponse(url=redirect_url, status_code=HTTP_303_SEE_OTHER)
    set_all_auth_cookies(redirect, rt["refresh_token"], rt["refresh_token_expiry"])
    
    return redirect


def list_available_providers() -> list[str]:
    return provider_registry.list_enabled_providers()

