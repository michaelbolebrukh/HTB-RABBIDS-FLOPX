from fastapi import APIRouter
from fastapi.responses import PlainTextResponse, RedirectResponse
from sqlalchemy import text
from starlette import status

from infrastructure.dependencies import db_dependency
from infrastructure.rate_limiter import limiter


router = APIRouter()


@router.get("/", response_class=PlainTextResponse, include_in_schema=False)
def root():
    return RedirectResponse(url="/docs", status_code=status.HTTP_301_MOVED_PERMANENTLY)


# Both health endpoints are exempt from rate limiting, and that is a
# correctness requirement rather than a quota courtesy. The limiter middleware
# runs on every non-exempt route and reads its counter store first, so without
# this the platform's health probe answers only as fast as the CACHE_URL store
# does — a degraded cache then reads to the host as a dead service and gets the
# instance replaced, which is exactly what happened on 2026-08-10. Liveness in
# particular is supposed to report on this process and nothing else.
@router.get("/healthz", response_class=PlainTextResponse, include_in_schema=False)
@limiter.exempt
def health_check():
    """Liveness only — the process is up. Never touches dependencies."""
    return "OK"


@router.get("/readyz", response_class=PlainTextResponse, include_in_schema=False)
@limiter.exempt
def readiness_check(db: db_dependency):
    """Readiness — the service can do real work (database reachable)."""
    try:
        db.execute(text("SELECT 1"))
    except Exception:
        return PlainTextResponse("NOT READY", status_code=status.HTTP_503_SERVICE_UNAVAILABLE)
    return "READY"
