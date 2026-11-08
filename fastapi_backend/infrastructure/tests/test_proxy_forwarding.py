"""Which address an IP-keyed rate limit ends up bucketing on.

Behaviour, not configuration. The failure this exists to stop is silent: every
anonymous visitor collapsing onto the proxy's address, so the limit still
returns headers and still counts, but protects nothing.
"""

from starlette.requests import Request

from infrastructure.rate_limiter import client_ip


def _request(headers: dict[str, str], peer: str = "10.216.27.42") -> Request:
    return Request({
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        "client": (peer, 51234),
        "server": ("test", 80),
        "scheme": "http",
        "query_string": b"",
    })


def test_a_client_cannot_choose_its_own_bucket():
    """The spoof: the caller prepends an address, the proxy appends the real
    one. Reading left-to-right would hand them a bucket of their choosing —
    a fresh one per request, or a chosen victim's."""
    assert client_ip(_request({"X-Forwarded-For": "9.9.9.9, 8.8.4.4"})) == "8.8.4.4"


def test_the_real_client_is_used_when_nothing_is_spoofed():
    assert client_ip(_request({"X-Forwarded-For": "8.8.4.4"})) == "8.8.4.4"


def test_private_hops_are_skipped_rather_than_bucketed_on():
    """Internal hops appended after the client must not become the key — that
    is the exact collapse this replaced."""
    assert client_ip(_request({"X-Forwarded-For": "8.8.4.4, 10.0.0.7"})) == "8.8.4.4"


# NOTE: the fixtures use genuinely globally-routable addresses. RFC 5737
# documentation ranges (203.0.113.0/24 and friends) are `is_private` on
# Python 3.11+, so using them here would have exercised the skip branch and
# quietly asserted the opposite of the intended case.


def test_a_garbage_header_falls_back_to_the_socket_peer():
    assert client_ip(_request({"X-Forwarded-For": "not-an-ip"}, peer="198.51.100.9")) == "198.51.100.9"


def test_no_header_falls_back_to_the_socket_peer():
    assert client_ip(_request({}, peer="198.51.100.9")) == "198.51.100.9"


def test_two_visitors_behind_the_same_proxy_get_different_keys():
    """The property that was actually broken."""
    a = client_ip(_request({"X-Forwarded-For": "8.8.4.4"}))
    b = client_ip(_request({"X-Forwarded-For": "1.1.1.1"}))
    assert a != b
