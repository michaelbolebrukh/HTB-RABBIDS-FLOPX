import os
from typing import Optional, Tuple
from datetime import datetime, timedelta, UTC
import uuid

from jose import jwt, JWTError

from platform.exceptions import JWTException
from platform.enums import TokenUse
from infrastructure.settings import settings as s



# Token type constant:
TOKEN_TYPE = "bearer"


# ============================================================================
# TOKEN CREATION
# ============================================================================

def _create_token(user_id: int, ttl: timedelta, token_use: TokenUse, extra: Optional[dict] = None) -> Tuple[str, str]:
    """Creates a JWT token with standard claims and returns token string and JTI"""
    now = datetime.now(UTC)
    exp = now + ttl
    jti = str(uuid.uuid4())
    payload = {
        "sub": str(user_id),
        "iat": now,
        "exp": exp,
        "jti": jti,
        "token_use": token_use.value,
    }
    if extra:
        payload.update(extra)
    token = jwt.encode(payload, s.HASH_KEY, s.HASH_ALGORITHM)
    return token, jti


def create_tokens(user_id: int, sid: str):
    """Creates both access and refresh tokens for a user session"""
    
    # sid is required for refresh tokens; enforcing:
    if not sid: raise ValueError("sid is required to mint tokens")

    # Creating tokens:
    access_token, _ = _create_token(user_id, s.TOKEN_TTL_TIMEDELTA, TokenUse.ACCESS, {"sid": sid})
    refresh_token, _ = _create_token(user_id, s.REFRESH_TOKEN_TTL_TIMEDELTA, TokenUse.REFRESH, {"sid": sid})
    now = datetime.now(UTC)

    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "access_token_expiry": now + s.TOKEN_TTL_TIMEDELTA,
        "refresh_token_expiry": now + s.REFRESH_TOKEN_TTL_TIMEDELTA,
        "token_type": TOKEN_TYPE,
    }


def create_access_token(user_id: int, sid: str):
    """Creates a short-lived access token for API requests.

    `sid` is required, not optional. An access token is a self-contained
    signature that nothing can reach once minted, so the session claim is the
    only thing that lets a sign-out revoke it (see `assert_session_live`). A
    default of None would mean any future mint site that forgot the argument
    silently produced a permanently unrevocable token - fail-open, invisibly.
    """
    if not sid: raise ValueError("sid is required to mint an access token")

    access_token, _ = _create_token(user_id, s.TOKEN_TTL_TIMEDELTA, TokenUse.ACCESS, {"sid": sid})
    now = datetime.now(UTC)
    return {
        "access_token": access_token,
        "access_token_expiry": now + s.TOKEN_TTL_TIMEDELTA,
        "token_type": TOKEN_TYPE,
    }


def create_refresh_token(user_id: int, sid: str):
    """Creates a long-lived refresh token for session persistence"""
    token, jti = _create_token(user_id, s.REFRESH_TOKEN_TTL_TIMEDELTA, TokenUse.REFRESH, {"sid": sid})
    now = datetime.now(UTC)
    return {
        "refresh_token": token,
        "refresh_token_expiry": now + s.REFRESH_TOKEN_TTL_TIMEDELTA,
        "jti": jti,
        "sid": sid,
    }


# ============================================================================
# TOKEN DECODING
# ============================================================================

def decode_token(token: str, expected_use: Optional[TokenUse] = None) -> dict:
    """Decodes and validates a JWT token with optional token use verification"""
    try:
        payload = jwt.decode(token, s.HASH_KEY, algorithms=[s.HASH_ALGORITHM])
        user_id = payload.get("sub")
        if user_id is None:
            raise JWTError
        if expected_use is not None and payload.get("token_use") != expected_use.value:
            raise JWTError
    except JWTError:
        raise JWTException
    return payload


