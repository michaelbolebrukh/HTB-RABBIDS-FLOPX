import logging
import re
from datetime import timedelta

from fastapi import Request
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from starlette import status as st
from slowapi.errors import RateLimitExceeded
from slowapi import _rate_limit_exceeded_handler

from infrastructure.settings import settings as s


def _origin_is_allowed(origin: str) -> bool:
    """Mirrors the CORSMiddleware origin decision for the error-response path."""
    if "*" in s.ALLOWED_ORIGINS or origin in s.ALLOWED_ORIGINS:
        return True
    regex = s.ALLOWED_ORIGIN_REGEX_COMPUTED
    return bool(regex and re.fullmatch(regex, origin))


logger = logging.getLogger(__name__)


async def unhandled_exception_handler(request: Request, exc: Exception):
    """Returns a CORS-safe 500 for otherwise-unhandled exceptions.

    Starlette generates 500s in ServerErrorMiddleware, which sits ABOVE the CORS
    middleware, so an uncaught exception would return a 500 with no CORS headers.
    A browser on another origin (the frontend on a different subdomain) then can
    not read that response and the client sees a bare network failure instead of
    a server error. Handling Exception here lets us re-apply the CORS headers the
    CORSMiddleware would have added. The exception is NOT swallowed — error
    tracking still captures it once at the error boundary (verified), so this
    changes only what the browser can read, not error visibility.
    """
    # Logging it here as well as leaving it to error tracking. The two are not
    # interchangeable: a deployment with no SENTRY_DSN (previews, local, any
    # instance before Sentry is wired) would otherwise record the failure
    # NOWHERE at all, and a 500 with an empty log is close to undebuggable:
    logger.exception("Unhandled exception on %s %s", request.method, request.url.path)
    response = JSONResponse(
        status_code=st.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": "Something went wrong on our end. Try again in a moment."},
    )
    origin = request.headers.get("origin")
    if origin and _origin_is_allowed(origin):
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Access-Control-Allow-Credentials"] = "true"
        response.headers["Vary"] = "Origin"
    return response



async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """Formats validation errors into user-friendly messages"""
    errors = []
    for error in exc.errors():
        # Checking if this is a password validation error:
        if 'password' in str(error.get('loc', [])).lower() and error.get('type') in ['password_too_short', 'password_too_weak', 'value_error']:
            # Preserving the existing clean message:
            errors.append(error['msg'])
        else:
            # Applying the standard formatting to other validation errors:
            field = " -> ".join([
                str(elem).replace('_', ' ').capitalize() for elem in error['loc'] if elem != 'body'
            ])
            
            # Removing the first word from the message, which is the data type:
            message = " ".join(error['msg'].split()[1:])
            
            errors.append(f"{field} {message}.")
    
    return JSONResponse(status_code=st.HTTP_422_UNPROCESSABLE_ENTITY, content={"detail": " ".join(errors)})


async def rate_limit_exception_handler(request: Request, exc: RateLimitExceeded):
    """Handles rate limit exceeded errors with user-friendly messages"""
    fallback_detail = "You're going a bit too fast. Please wait a moment before trying again."
    
    try:
        # Obtaining slowapi's default response for headers and timing:
        default_response = _rate_limit_exceeded_handler(request, exc)
        
        # Extracting retry_after and preparing headers:
        retry_after = None
        response_headers = {}
        
        if hasattr(default_response, 'headers') and 'retry-after' in default_response.headers:
            retry_after = int(default_response.headers['retry-after'])
            response_headers['Retry-After'] = default_response.headers['retry-after']
        
        # Creating simple, user-facing message:
        if retry_after:
            time_str = format_time_duration(retry_after)
            detail = f"You're going a bit too fast. Please wait {time_str} before trying again."
        else:
            detail = fallback_detail
        
        return JSONResponse(
            status_code=st.HTTP_429_TOO_MANY_REQUESTS,
            content={"detail": detail},
            headers=response_headers
        )
        
    except Exception:
        # Simple fallback:
        return JSONResponse(
            status_code=st.HTTP_429_TOO_MANY_REQUESTS,
            content={"detail": fallback_detail}
        )


def format_time_duration(seconds: int) -> str:
    """Formats a duration in seconds into a human-readable string"""
    if seconds < 60: return f"{seconds} second{'s' if seconds != 1 else ''}"
    
    duration = timedelta(seconds=seconds)
    days = duration.days
    hours, remainder = divmod(duration.seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    
    parts = []
    if days:
        parts.append(f"{days} day{'s' if days != 1 else ''}")
    if hours:
        parts.append(f"{hours} hour{'s' if hours != 1 else ''}")
    if minutes:
        parts.append(f"{minutes} minute{'s' if minutes != 1 else ''}")

    # Only showing seconds if no days/hours:
    if secs and not (days or hours):
        parts.append(f"{secs} second{'s' if secs != 1 else ''}")
    
    if len(parts) == 1:
        return parts[0]
    elif len(parts) == 2:
        return f"{parts[0]} and {parts[1]}"
    else:
        return f"{', '.join(parts[:-1])}, and {parts[-1]}"