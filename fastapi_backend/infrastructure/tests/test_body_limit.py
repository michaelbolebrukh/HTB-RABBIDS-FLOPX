"""The transport-layer body-size backstop, exercised as real HTTP traffic
against a FastAPI app wired the same way `main.py` wires it: the middleware
sits closer to the router than CORSMiddleware, and an oversized body must
never reach the endpoint at all.

Two rejection paths are pinned separately because they are genuinely
different code paths: a declared Content-Length is rejected before the inner
app is even called, while an oversized body with no (or a lying)
Content-Length is only caught while it streams in.
"""

import pytest
from fastapi import FastAPI, File, UploadFile
from starlette.testclient import TestClient

from infrastructure.body_limit import BodySizeLimitMiddleware
from infrastructure.handlers import unhandled_exception_handler
from infrastructure.rate_limiter import limiter
from slowapi.errors import RateLimitExceeded
from infrastructure.handlers import rate_limit_exception_handler
from slowapi.middleware import SlowAPIMiddleware


CAP_BYTES = 1024  # A small cap so tests run fast and stay obviously over/under it.


def _make_app(reached: list) -> FastAPI:
    """A minimal app that mirrors main.py's wiring order: BodySizeLimitMiddleware
    is added FIRST (innermost, closest to the router), matching the real app so
    a passing test here says something about production, not just this file."""
    app = FastAPI()
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=CAP_BYTES)
    app.add_exception_handler(Exception, unhandled_exception_handler)
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, rate_limit_exception_handler)
    app.add_middleware(SlowAPIMiddleware)

    @app.post("/upload")
    async def upload(file: UploadFile = File(...)):
        reached.append(True)
        data = await file.read()
        return {"size": len(data)}

    return app


def _client(reached: list) -> TestClient:
    return TestClient(_make_app(reached))


def _multipart(size: int) -> bytes:
    """A hand-built multipart/form-data body carrying one file part of exactly
    `size` bytes - built by hand (not httpx's `files=`) so the exact wire size
    is known and controllable on both sides of the cap."""
    boundary = b"----bodylimittest"
    payload = b"A" * size
    parts = [
        b"--" + boundary,
        b'Content-Disposition: form-data; name="file"; filename="f.bin"',
        b"Content-Type: application/octet-stream",
        b"",
        payload,
        b"--" + boundary + b"--",
        b"",
    ]
    return b"\r\n".join(parts)


def _content_type(boundary: bytes = b"----bodylimittest") -> str:
    return f"multipart/form-data; boundary={boundary.decode()}"


# ============================================================================
# DECLARED CONTENT-LENGTH OVER THE CAP -> REJECTED BEFORE THE INNER APP RUNS
# ============================================================================

def test_oversized_declared_content_length_gets_413_and_never_reaches_the_handler():
    reached: list = []
    body = _multipart(CAP_BYTES * 4)
    assert len(body) > CAP_BYTES  # The declared Content-Length httpx sends is len(body)

    response = _client(reached).post(
        "/upload", content=body, headers={"Content-Type": _content_type()},
    )

    assert response.status_code == 413
    assert "limit" in response.json()["detail"].lower()
    assert reached == []  # The endpoint body never ran


# ============================================================================
# NO CONTENT-LENGTH (CHUNKED) -> STILL CAUGHT WHILE STREAMING
# ============================================================================

def test_oversized_body_with_no_content_length_still_gets_413():
    reached: list = []
    body = _multipart(CAP_BYTES * 4)

    def stream():
        # Yielding in small pieces so this genuinely exercises the streaming
        # meter rather than handing the whole body over in one message:
        for i in range(0, len(body), 200):
            yield body[i:i + 200]

    client = _client(reached)
    response = client.post(
        "/upload", content=stream(), headers={"Content-Type": _content_type()},
    )

    # httpx omits Content-Length entirely for a generator body (verified: see
    # scratch probe) - this is the "absent" half of "absent or a lie":
    assert response.status_code == 413
    assert reached == []


def test_declared_content_length_that_lies_small_is_still_caught_while_streaming():
    """A Content-Length header that UNDER-declares the real body - the "lie"
    half. The upfront check passes (declared <= cap), so only the streaming
    meter can catch this."""
    reached: list = []
    body = _multipart(CAP_BYTES * 4)

    def stream():
        for i in range(0, len(body), 200):
            yield body[i:i + 200]

    client = _client(reached)
    response = client.post(
        "/upload",
        content=stream(),
        headers={"Content-Type": _content_type(), "Content-Length": "10"},
    )

    assert response.status_code == 413
    assert reached == []


# ============================================================================
# A LEGITIMATE UPLOAD STILL SUCCEEDS - THE MIDDLEWARE IS NOT A BLANKET REJECT
# ============================================================================

def test_a_body_under_the_cap_reaches_the_handler_and_succeeds():
    reached: list = []
    small = CAP_BYTES // 4
    body = _multipart(small)

    response = _client(reached).post(
        "/upload", content=body, headers={"Content-Type": _content_type()},
    )

    assert response.status_code == 200, response.text
    assert reached == [True]
    assert response.json()["size"] == small


# ============================================================================
# NON-HTTP SCOPES PASS THROUGH UNTOUCHED
# ============================================================================

def test_lifespan_scope_passes_through_untouched():
    """Booting the app runs the ASGI lifespan protocol through every middleware,
    including this one - it must not choke on a scope with no body to cap."""
    reached: list = []
    with TestClient(_make_app(reached)) as client:
        # TestClient's context manager drives lifespan.startup/shutdown; simply
        # entering and exiting without error is the assertion:
        assert client.get("/does-not-exist").status_code == 404


# ============================================================================
# THE WIRING ITSELF, AND THE BOUNDARY
#
# Both exist because mutation testing found them missing: deleting the
# middleware from main.py left the whole suite green, and flipping the
# comparison to `>=` (so a body of exactly the cap is refused) went unnoticed
# too. A test that builds its own app can prove the class works, never that
# the running application uses it.
# ============================================================================

def test_the_real_application_actually_installs_the_middleware():
    """The one fact the rest of this file cannot check: that `main.app` - the
    object gunicorn serves - carries this middleware. Every other test here
    hand-wires its own FastAPI instance, so removing the middleware from
    main.py would leave them all passing and production unprotected."""
    from main import app

    assert BodySizeLimitMiddleware in [middleware.cls for middleware in app.user_middleware]


def test_the_middleware_sits_inside_cors_on_the_real_application():
    """Order matters for a reason a user would notice: a 413 carrying no CORS
    headers reaches a browser as an opaque network error instead of a message.
    Starlette applies `user_middleware` outermost-first, so CORS must come
    BEFORE this middleware in that list for its headers to wrap our reject."""
    from fastapi.middleware.cors import CORSMiddleware
    from main import app

    installed = [middleware.cls for middleware in app.user_middleware]
    assert installed.index(CORSMiddleware) < installed.index(BodySizeLimitMiddleware)


@pytest.mark.parametrize("over_by, expected", [(0, 200), (1, 413)])
def test_the_cap_is_exact_at_the_boundary(over_by, expected):
    """A body of exactly the cap passes; one byte more does not. Written
    because `>` quietly becoming `>=` refuses every upload that lands on the
    advertised limit, and nothing in the suite noticed."""
    reached: list = []
    # Sized so the whole multipart envelope, not just the payload, lands
    # exactly on the cap - the cap measures the wire body:
    payload_size = CAP_BYTES - (len(_multipart(0)) + over_by) + over_by
    body = _multipart(payload_size + over_by)
    assert len(body) == CAP_BYTES + over_by

    response = _client(reached).post(
        "/upload", content=body, headers={"content-type": _content_type()}
    )

    assert response.status_code == expected


def test_a_file_of_exactly_the_document_limit_survives_its_multipart_envelope():
    """The regression the framing margin exists for.

    The per-endpoint caps measure the decoded FILE; this middleware measures
    the raw WIRE BODY. With the two set to the same number, a visitor who
    uploads a file of exactly the size the product advertises is refused by the
    transport before the endpoint - which would have accepted it - is ever
    reached. The envelope is what pushes it over, so this sends a real one at
    the real default cap rather than the small test cap.
    """
    from infrastructure.settings import settings as real_settings

    reached: list = []
    app = FastAPI()
    app.add_middleware(BodySizeLimitMiddleware)  # The real default, not CAP_BYTES

    @app.post("/upload")
    async def upload(file: UploadFile = File(...)):
        reached.append(True)
        return {"size": len(await file.read())}

    body = _multipart(real_settings.MAX_DOCUMENT_BYTES)
    assert len(body) > real_settings.MAX_DOCUMENT_BYTES, "the envelope must exceed the file"

    response = TestClient(app).post(
        "/upload", content=body, headers={"content-type": _content_type()}
    )

    assert response.status_code == 200, "a file at exactly the advertised limit must not be refused"
    assert reached == [True]
