"""
Shared JSON decoding for media adapters (Phase 7, D43).

A provider can answer HTTP 200 with a body that is not JSON at all — an HTML
error page from an edge proxy, a truncated stream, a plain-text gateway
notice. `httpx.Response.json()` raises `json.JSONDecodeError` there, which is
NOT a `MediaError`, so it escapes the port untyped: `image_tool.py` only turns
`MediaError` into a reactable tool result, and everything else kills the run.

`types.MediaCallFailedError` is documented as covering "malformed response",
so every adapter decodes through this helper instead of calling `.json()`
directly. A non-dict JSON body (a bare list/string/number) is malformed for
the same reason — callers index it with `.get`.
"""

from __future__ import annotations

import httpx

from ..types import MediaCallFailedError


def json_payload(response: httpx.Response, label: str) -> dict:
    """Decoding a provider 200 body as a JSON object, or failing typed:"""
    try:
        payload = response.json()
    except ValueError as exc:  # json.JSONDecodeError is a ValueError
        raise MediaCallFailedError(
            f"{label} returned a non-JSON body ({response.status_code}): {response.text[:200]}"
        ) from exc
    if not isinstance(payload, dict):
        raise MediaCallFailedError(
            f"{label} returned a non-object JSON body ({type(payload).__name__})"
        )
    return payload
