"""Global ASGI-level cap on request body size — the transport-layer backstop in
front of FastAPI's multipart/form parser.

FastAPI resolves `file: UploadFile = File(...)` by awaiting `request.form()`
BEFORE the endpoint function body runs, and Starlette spools each multipart
part to a `SpooledTemporaryFile` (1MB in memory, then to disk) while it parses.
A size check written inside a handler (a per-endpoint `_read_capped` helper)
only bounds what the HANDLER holds — by the time the handler gets to run its
own check, the whole body is already resident, in memory or on disk. This
middleware sits in front of the form parser instead, so the reject happens
before an anonymous caller can make the process buffer anything.

Both this middleware AND any per-endpoint cap stay, on purpose - they are not
redundant:
- This middleware is the single global ceiling for the WHOLE app, sized to the
  largest legitimate upload ANY endpoint accepts, enforced at the transport
  layer before any endpoint-specific code (or even routing) has a say.
- A per-endpoint cap can be tighter than the global ceiling, and it is what
  produces the endpoint's own message (quoting MAX_DOCUMENT_BYTES in its own
  words) rather than this middleware's generic one.
Deleting either one reopens the gap the other was added to close.
"""

import json
from typing import Optional

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from infrastructure.settings import settings as s
from platform.exceptions import DocumentTooLargeException


# What a multipart envelope costs on top of the file itself: the boundary
# lines, the per-part Content-Disposition and Content-Type headers, and the
# filename. A few hundred bytes in practice; 64KB is generous enough that no
# real client can exceed it and small enough to still be a meaningful ceiling.
#
# This margin is the whole point. The per-endpoint caps measure the DECODED
# FILE, while this middleware measures the RAW WIRE BODY, so setting them to
# the same number rejects a file of exactly the advertised size — the endpoint
# says "up to 10 MB", the visitor sends exactly 10 MB, and the framing pushes
# the body 127 bytes over a ceiling they were told they were within. Measured,
# not theorised: that is the exact overhead of a minimal multipart wrapper
# around a 10 MB PDF.
_MULTIPART_FRAMING_HEADROOM_BYTES = 64 * 1024

# The one global ceiling, sized to the LARGEST legitimate upload any endpoint
# accepts today - the private document cap, not the smaller avatar cap - plus the
# framing margin above. A future endpoint with a bigger legitimate limit should
# raise this, never add a second body-limit middleware (config-minimalism: one
# ceiling, not a var per endpoint):
MAX_REQUEST_BODY_BYTES = max(s.MAX_DOCUMENT_BYTES, s.MAX_AVATAR_BYTES) + _MULTIPART_FRAMING_HEADROOM_BYTES

# How much of an over-long body to swallow before answering it. Replying while
# the client is still uploading closes the connection under it, and a browser
# reports that as a network failure rather than showing the 413 — so the person
# who picked too large a file sees nothing useful instead of being told the
# size. Draining lets the exchange finish cleanly; the bound is what stops the
# drain from costing the very bandwidth the rejection exists to save.
_DRAIN_LIMIT_BYTES = 2 * 1024 * 1024


def _declared_content_length(scope: Scope) -> Optional[int]:
    """The client-declared Content-Length, or None when absent/unparseable
    (chunked transfer encoding carries no such header at all)."""
    for name, value in scope.get("headers", ()):
        if name == b"content-length":
            try:
                return int(value)
            except ValueError:
                return None
    return None


async def _drain(receive: Receive) -> None:
    """Reads and discards up to `_DRAIN_LIMIT_BYTES` of the body being sent.

    Answering a request whose body is still in flight leaves unread bytes on
    the socket, and the server closes on them — which surfaces to the client as
    a connection reset rather than the 413 we carefully wrote. Draining first
    gives the exchange somewhere to land. Bounded, because the caller is by
    definition sending more than we are willing to accept."""
    drained = 0
    while drained < _DRAIN_LIMIT_BYTES:
        try:
            message = await receive()
        except Exception:
            # The client hung up mid-upload; there is nothing left to drain and
            # nobody left to answer:
            return
        if message["type"] != "http.request":
            return
        drained += len(message.get("body") or b"")
        if not message.get("more_body"):
            return


async def _reject(send: Send, exc: DocumentTooLargeException) -> None:
    """Sends the 413 directly over the ASGI `send` we were given, WITHOUT ever
    calling the inner app. `send` here is whatever the middleware layers above
    us (CORS included, since this middleware is wired closer to the router
    than CORSMiddleware - see main.py) have already wrapped, so this still
    picks up the usual response headers rather than bypassing them."""
    body = json.dumps({"detail": exc.detail}).encode("utf-8")
    await send({
        "type": "http.response.start",
        "status": exc.status_code,
        "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode("ascii")),
        ],
    })
    await send({"type": "http.response.body", "body": body})


class BodySizeLimitMiddleware:
    """Rejects an oversized request body before the multipart/form parser (or
    any other body reader downstream) ever sees it.

    Two checks, because either alone is beatable:
    - `Content-Length` is checked up front, before the inner app is invoked at
      all - the cheap, zero-read rejection that covers every honest client.
    - The body is also metered while it streams in, because Content-Length can
      be absent (chunked transfer encoding) or simply a lie. This raises
      `DocumentTooLargeException` from inside the wrapped `receive`, which is
      invoked deep within routing/dependency resolution - the raise unwinds
      through that call stack and is caught by Starlette's ExceptionMiddleware
      (the innermost layer, wrapping the router), which already knows how to
      turn an HTTPException into a clean JSON response. Verified in
      infrastructure/tests/test_body_limit.py rather than assumed.
    """

    def __init__(self, app: ASGIApp, max_bytes: int = MAX_REQUEST_BODY_BYTES) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            # Lifespan and websocket scopes carry no request body to cap:
            await self.app(scope, receive, send)
            return

        declared = _declared_content_length(scope)
        if declared is not None and declared > self.max_bytes:
            await _drain(receive)
            await _reject(send, DocumentTooLargeException(_detail(self.max_bytes)))
            return

        total = 0

        async def capped_receive() -> Message:
            nonlocal total
            message = await receive()
            if message["type"] == "http.request":
                total += len(message.get("body") or b"")
                if total > self.max_bytes:
                    raise DocumentTooLargeException(_detail(self.max_bytes))
            return message

        await self.app(scope, capped_receive, send)


def _detail(max_bytes: int) -> str:
    """Platform-generic message - this middleware has no idea what kind of
    upload it is guarding, so it names no file type, only the limit (mirrors
    DocumentTooLargeException's own default, said in the units a person uses)."""
    megabytes = max_bytes // (1024 * 1024)
    return f"Request body exceeds the {megabytes} MB limit."
