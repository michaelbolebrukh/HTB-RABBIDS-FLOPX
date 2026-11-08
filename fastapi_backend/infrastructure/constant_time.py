"""Constant-time credential comparison for text carried on the wire.

`hmac.compare_digest` accepts `str` only when BOTH operands are ASCII-only; on
anything else it raises `TypeError: comparing strings with non-ASCII characters
is not supported`. Starlette decodes request headers and query strings as
latin-1, so any byte >= 0x80 in a bearer token, a signature or an unsubscribe
token arrives as a non-ASCII `str` — and an unguarded comparison turns a bad
credential into an unhandled 500 (plus a tracked error event) instead of the
ordinary refusal. That difference is measurable by an anonymous caller, and on
the public marketing endpoints it is a free error-quota drain.

`constant_time_equals` is the ONE comparison every credential check uses: it
encodes both sides to UTF-8 and defers to `hmac.compare_digest` on bytes, which
has no ASCII restriction. A non-ASCII credential is then simply a credential
that does not match — same code path, same timing profile, same response as any
other wrong value.
"""

import hmac


def constant_time_equals(presented: str | bytes | None, expected: str | bytes | None) -> bool:
    """Comparing two credentials in constant time, safe for any input bytes.

    `None` never matches (a missing credential is not a valid one). Text is
    compared by its UTF-8 encoding, so non-ASCII input is rejected exactly like
    any other mismatch rather than raising."""
    if presented is None or expected is None:
        return False
    presented_bytes = presented.encode("utf-8") if isinstance(presented, str) else presented
    expected_bytes = expected.encode("utf-8") if isinstance(expected, str) else expected
    return hmac.compare_digest(presented_bytes, expected_bytes)
