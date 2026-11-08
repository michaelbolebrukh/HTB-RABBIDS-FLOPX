"""Platform observability layer.

Owns logging configuration, error tracking (Sentry), and request correlation
in one place; `main.py` only composes. Error tracking is env-gated like the
cache: `SENTRY_DSN` unset = the SDK never initializes and nothing changes;
set = unhandled exceptions and ERROR-level logs are captured with stack,
locals, request context, and breadcrumbs. Fail-open throughout — a bad DSN
or a broken SDK must never take the app down.

Request correlation: `RequestIDMiddleware` accepts an inbound `X-Request-ID`
(or generates one), echoes it on the response, injects it into every log
line via a logging filter, and tags Sentry events with it — so one ID ties
together the client report, the log lines, and the error event.
"""

import logging
import os
import re
import uuid
from contextvars import ContextVar
from typing import Any, Optional

from infrastructure.settings import settings as s


logger = logging.getLogger(__name__)

# The current request's correlation id (None outside a request):
_request_id: ContextVar[Optional[str]] = ContextVar("request_id", default=None)

# Accepting only header values that cannot break log lines or headers:
_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


# ============================================================================
# LOGGING
# ============================================================================

class _RequestIDLogFilter(logging.Filter):
    """Injects the current request id into every log record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = _request_id.get() or "-"
        return True


def configure_logging() -> None:
    """Configures root logging with the request id woven into every line."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-5s | %(name)s | rid=%(request_id)s | %(message)s",
        handlers=[logging.StreamHandler()],
    )
    for handler in logging.getLogger().handlers:
        handler.addFilter(_RequestIDLogFilter())

    # Downgrading gunicorn's graceful-shutdown SIGTERM noise (also installed in
    # the master via gunicorn.conf.py, since that is where it is emitted):
    from infrastructure.log_noise import install_worker_lifecycle_filter
    install_worker_lifecycle_filter()


def get_request_id() -> Optional[str]:
    """Returns the current request's correlation id, if inside a request."""
    return _request_id.get()


# ============================================================================
# ERROR TRACKING (Sentry, env-gated)
# ============================================================================

def _stamp_request_id(event: Any, hint: Any) -> Any:
    """Attaches the current request's correlation id to every outgoing event.

    Runs at capture time via `before_send`, which is ordering-independent —
    it works even for unhandled 500s captured outside our middleware, where
    a scope tag set at request start might already be gone.
    """
    request_id = _request_id.get()
    if request_id:
        event.setdefault("tags", {}).setdefault("request_id", request_id)
    return event


def init_error_tracking() -> None:
    """Initializes Sentry when SENTRY_DSN is set; a silent no-op otherwise.

    Must run before the FastAPI app is instantiated so the Starlette/FastAPI
    integrations can wrap the middleware stack. The logging integration also
    turns every existing `logger.error`/`logger.exception` call into a
    tracked event, so swallowed background failures (e.g. email sends)
    surface without touching their call sites.
    """
    if not s.SENTRY_DSN:
        return

    try:
        # Importing lazily so the dependency is only exercised when configured:
        import sentry_sdk

        sentry_sdk.init(
            dsn=s.SENTRY_DSN,
            environment=s.ENV,
            # Render injects the deployed commit; local runs go unversioned:
            release=os.environ.get("RENDER_GIT_COMMIT"),
            # Never sending request bodies, cookies, or user emails/IPs:
            send_default_pii=False,
            # Never sending frame locals either. `send_default_pii` does NOT
            # cover these — it governs Sentry's own automatic PII, while local
            # variables default to ON and are captured for every frame of every
            # ERROR-level event. That is the wider leak of the two: any handler
            # that touches user data holds it in a local, so an exception
            # anywhere near a private document, an email body or an uploaded file ships the
            # contents to a third party. Losing the locals costs some debugging
            # convenience; the stack, the message and the request id remain:
            include_local_variables=False,
            # Performance tracing off by default; raise via env to sample:
            traces_sample_rate=s.SENTRY_TRACES_SAMPLE_RATE,
            before_send=_stamp_request_id,
        )
        logger.info("Error tracking enabled (sentry, env=%s)", s.ENV)
    except Exception:
        logger.warning(
            "SENTRY_DSN is set but Sentry failed to initialize; continuing without error tracking.",
            exc_info=True,
        )


def set_user(user_id: Any) -> None:
    """Attaches the authenticated user's id (never email) to error events."""
    if not s.SENTRY_DSN:
        return
    try:
        import sentry_sdk

        sentry_sdk.set_user({"id": str(user_id)})
    except Exception:
        pass


def _tag_current_scope(request_id: str) -> None:
    if not s.SENTRY_DSN:
        return
    try:
        import sentry_sdk

        sentry_sdk.set_tag("request_id", request_id)
    except Exception:
        pass


# ============================================================================
# REQUEST CORRELATION MIDDLEWARE
# ============================================================================

class RequestIDMiddleware:
    """Binds one correlation id per request: inbound header honored when sane,
    generated otherwise; echoed on the response; visible to logs and Sentry."""

    HEADER = b"x-request-id"

    def __init__(self, app: Any):
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        incoming = next(
            (value.decode("latin-1") for name, value in scope.get("headers", []) if name == self.HEADER),
            None,
        )
        request_id = incoming if incoming and _REQUEST_ID_PATTERN.match(incoming) else uuid.uuid4().hex
        # Each ASGI request runs in its own task, so this contextvar is task-local
        # and never leaks between requests; we deliberately do not reset it, so it
        # is still readable when Sentry captures an unhandled error above us:
        _request_id.set(request_id)
        _tag_current_scope(request_id)

        async def send_with_header(message: Any) -> None:
            if message["type"] == "http.response.start":
                message.setdefault("headers", []).append((self.HEADER, request_id.encode("latin-1")))
            await send(message)

        await self.app(scope, receive, send_with_header)
