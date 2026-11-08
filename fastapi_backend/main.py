import os
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from slowapi.middleware import SlowAPIMiddleware
from fastapi.exceptions import RequestValidationError
from slowapi.errors import RateLimitExceeded
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from dotenv import load_dotenv
import uvicorn


# Setting root path for local imports:
BACKEND_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BACKEND_ROOT)

# Ensuring the local platform package wins even if stdlib platform was imported first:
loaded_platform = sys.modules.get("platform")
loaded_platform_path = getattr(loaded_platform, "__file__", "") if loaded_platform else ""
loaded_platform_path = loaded_platform_path or ""
local_platform_root = os.path.join(BACKEND_ROOT, "platform")
if loaded_platform and not os.path.abspath(loaded_platform_path).startswith(local_platform_root):
    del sys.modules["platform"]


# Loading environment variables and declaring FastAPI instance before local imports:
load_dotenv(os.path.join(BACKEND_ROOT, ".env"))

# Configuring logging and error tracking before the app and its imports:
from infrastructure.observability import configure_logging, init_error_tracking, RequestIDMiddleware
configure_logging()
init_error_tracking()

from infrastructure.body_limit import BodySizeLimitMiddleware
from infrastructure.handlers import validation_exception_handler, rate_limit_exception_handler, unhandled_exception_handler
from infrastructure.cache import CacheHeaderMiddleware
from platform import routers as platform_router
from product import routers as product_router
from infrastructure.startup import run_startup_tasks
from infrastructure.rate_limiter import limiter
from infrastructure.settings import settings as s


# Running startup tasks (migrations + seeders) when the app starts serving, not
# at import time — so merely importing this module never mutates a database. A
# failure here aborts boot (fail-closed): the ASGI server exits non-zero, so
# Render's health gate keeps the old instance instead of promoting a broken one.
@asynccontextmanager
async def lifespan(_app: FastAPI):
    run_startup_tasks()
    # Background entitlement reconciliation failsafe (no-op unless payments are
    # configured and the interval is > 0); cancelled cleanly on shutdown:
    from platform.services.payment_services.reconcile_scheduler import start_entitlement_reconcile_loop
    reconcile_task = start_entitlement_reconcile_loop()
    # Hourly question-flow reminder + lead-retention sweep (returns None unless
    # some registered flow declares reminder stages or opts into lead capture):
    from platform.services.question_flow_service import start_question_flow_sweep_loop
    question_flow_task = start_question_flow_sweep_loop()
    try:
        yield
    finally:
        for task in (reconcile_task, question_flow_task):
            if task is not None:
                task.cancel()


# Initializing FastAPI application instance:
app = FastAPI(lifespan=lifespan)

# Capping request body size before ANYTHING else touches the body - added
# first so it ends up innermost (closest to the router) among the middleware
# below, which matters twice over: Starlette's ExceptionMiddleware (which
# turns the DocumentTooLargeException this raises into a clean 413) sits right
# below it, and every middleware added AFTER it (CORS included) still wraps
# its response, so a rejected upload still carries normal CORS headers rather
# than looking like a bare network failure to a cross-origin frontend:
app.add_middleware(BodySizeLimitMiddleware)

# Adding CORS middleware before other middleware:
app.add_middleware(
    CORSMiddleware,
    allow_origins=s.ALLOWED_ORIGINS,
    allow_origin_regex=s.ALLOWED_ORIGIN_REGEX_COMPUTED,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-CSRF-Token", "X-Auth-Mode", "X-Request-ID"],
    expose_headers=["WWW-Authenticate", "Retry-After", "X-Cache", "X-Request-ID"],
)

# Adding session middleware for OAuth state handling:
app.add_middleware(
    SessionMiddleware,
    secret_key=s.HASH_KEY,
    same_site=s.COOKIE_SAMESITE,
    https_only=s.COOKIE_SECURE,
)

# Stamping X-Cache: HIT|MISS on responses that touched the cache:
app.add_middleware(CacheHeaderMiddleware)

# Binding a correlation id per request (logs, Sentry, and the response header):
app.add_middleware(RequestIDMiddleware)

# Adding rate limiting middleware:
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, rate_limit_exception_handler)
app.add_middleware(SlowAPIMiddleware)

# Adding custom validation handler that generates user-facing error messages:
app.add_exception_handler(RequestValidationError, validation_exception_handler)

# Ensuring unhandled 500s still carry CORS headers so cross-origin clients (the
# frontend on another subdomain) can read the error instead of seeing a bare
# network failure. Does not swallow the exception — error tracking still captures it:
app.add_exception_handler(Exception, unhandled_exception_handler)


# Serving locally-stored uploads (no-op target when object storage is S3-backed):
os.makedirs(s.STORAGE_LOCAL_DIR, exist_ok=True)
app.mount("/uploads", StaticFiles(directory=s.STORAGE_LOCAL_DIR), name="uploads")

# Adding all routers:
for module in [platform_router, product_router]:
    app.include_router(module.router)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)



# Run Commands:
"""
source fastapi_backend/.venv/bin/activate && uvicorn fastapi_backend.main:app --host localhost --port 8000 --reload
cd next-pwa && npm run dev
cd next-pwa && npm run build && npm run start


Render Settings:
Root Directory: fastapi_backend/
Build Command: pip install -r requirements.txt
Start Command: gunicorn -k uvicorn.workers.UvicornWorker --bind 0.0.0.0:$PORT main:app

Worker count is the WEB_CONCURRENCY env var (gunicorn reads it natively);
unset = 1 (safe floor for a fresh clone). gunicorn.conf.py ships two memory
defaults with no env var to enable them: preload_app (the app is imported once
in the master and forked, so the AI SDK / SQLAlchemy / Sentry imports are shared
copy-on-write instead of duplicated per worker) and max_requests recycling (each
worker is refreshed after a bounded number of requests, so gradual growth never
reaches the instance ceiling). Preload makes each extra worker cheap; scaling
WEB_CONCURRENCY up is far safer than before. The generative-AI service still
graduates to its own instance later to shed the footprint entirely.
"""

