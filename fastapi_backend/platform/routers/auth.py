from fastapi import APIRouter, Request, Response, status as st

from infrastructure.rate_limiter import limiter
from infrastructure.dependencies import auth_dependency, db_dependency
from platform.schemas import AccessTokenResponse, DualTokenResponse
from platform.services.auth_services import auth_service, oauth_service


router = APIRouter(prefix="/auth", tags=["Authentication"])


@router.post("/token", response_model=DualTokenResponse, status_code=st.HTTP_200_OK)
@limiter.limit("20/minute, 60/hour, 100/day")
def login_and_generate_token(db: db_dependency, auth_form: auth_dependency, request: Request, response: Response):
    return auth_service.login_and_generate_token(db, auth_form, request, response)


@router.post("/token/refresh", response_model=AccessTokenResponse, status_code=st.HTTP_200_OK)
# Silent refresh fires on every full page load and on access-token expiry, and is
# keyed by IP here (no auth header is sent on refresh). On shared NAT/CGNAT (e.g. a
# university network) many distinct users egress from one IP, so the previous
# 30/min, 120/hour budget was exhausted by legitimate traffic. Loosened to a
# NAT-tolerant limit; the cookie + CSRF requirements already gate real abuse:
@limiter.limit("60/minute, 1000/hour")
def refresh_token(db: db_dependency, request: Request, response: Response):
    return auth_service.refresh_access_token(db, request, response)


@router.post("/logout", status_code=st.HTTP_204_NO_CONTENT)
@limiter.limit("20/minute, 60/hour, 100/day")
def logout(db: db_dependency, request: Request, response: Response):
    auth_service.logout(db, request, response)
    return Response(status_code=st.HTTP_204_NO_CONTENT)


@router.get("/oauth/{provider}/login")
@limiter.limit("30/minute, 200/hour")
async def oauth_login(provider: str, request: Request):
    return await oauth_service.oauth_login(request, provider)


@router.get("/oauth/{provider}/callback", name="oauth_callback")
@limiter.limit("60/minute, 500/hour")
async def oauth_callback(provider: str, db: db_dependency, request: Request, response: Response):
    return await oauth_service.oauth_callback(db, request, response, provider)


@router.get("/oauth/providers", status_code=st.HTTP_200_OK)
def list_oauth_providers():
    return oauth_service.list_available_providers()
