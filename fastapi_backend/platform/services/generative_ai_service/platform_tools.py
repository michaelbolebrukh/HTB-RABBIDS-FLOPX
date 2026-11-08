"""
Platform tools — the built-in residents of the tool table (Phase 5).

Three implementations, registered at import time (D33):

- `search_conversation` — THE requirement-7 tool: RAG over this
  conversation's full transcript, working even after summarization has
  compacted old turns out of the rendered context. Semantic when vectors
  exist, text matching otherwise; scoped per D30.
- `web_fetch` — our client-side FILL for models without a native WebFetch
  capability (D34): plain HTTP GET with an SSRF guard, size cap, and
  HTML-to-text reduction.
- `web_search` — our client-side FILL for models without native web search
  (D34): a best-effort keyless search via DuckDuckGo's HTML endpoint. A
  degraded stand-in by design — models whose provider offers native search
  never see this implementation (the registry decides, D34).

Security posture (tools are the new attack surface):
- Tool results are UNTRUSTED MODEL INPUT, never instructions — the transcript
  stores them as data items (P1 design), so nothing here needs to sanitize
  "prompt injection" beyond not fabricating authority.
- `search_conversation` returns compact digests, never raw items — `raw`
  holds provider-internal payloads that must not leak into model-visible text.
- `web_fetch`/`web_search` block private/reserved address ranges so a model
  fetching attacker-supplied URLs cannot become a proxy into our network.

No `pydantic_ai` import (D3): implementations are plain async callables; the
adapter binds them to the SDK.
"""

from __future__ import annotations

import asyncio
import html as html_lib
import ipaddress
import re
import socket
from typing import Any
from urllib.parse import urlsplit

import httpx

from .engine.tool_table import ToolContext, register_tool
from .engine.types import ToolExecutionError
from .transcript.search import search_conversation as _search_store
from .transcript.types import Item


# ============================================================================
# search_conversation — RAG over the transcript (requirement 7)
# ============================================================================

# Digest excerpts stay small: the model asked for RECALL, not a re-dump of the
# transcript into its context window (which would defeat summarization):
_DIGEST_EXCERPT_CHARS = 500


def _digest(item: Item) -> dict[str, Any]:
    """
    Reducing one transcript item to the compact, model-safe digest shape:
    role/kind/tool_name for orientation, a bounded text excerpt, and the
    timestamp. Never `raw` (provider-internal payloads must not surface):
    """
    text = item.extract_text() or ""
    if len(text) > _DIGEST_EXCERPT_CHARS:
        text = text[:_DIGEST_EXCERPT_CHARS] + "…"
    return {
        "role": item.role.value if item.role else None,
        "kind": item.kind.value,
        "tool_name": item.tool_name,
        "text": text,
        "created_at": item.created_at.isoformat() if item.created_at else None,
    }


async def search_conversation(ctx: ToolContext, query: str, k: int = 8) -> list[dict]:
    """
    Search this conversation's full history for earlier content — including
    turns that have been summarized out of the visible context. Returns the
    most relevant excerpts, newest-biased, as compact digests.
    """
    # Bounding k defensively — the model controls it, so it must not be able
    # to request the entire transcript through a huge k:
    k = max(1, min(int(k), 25))

    # D30 scope: active path unless the agent was explicitly granted the
    # all-branches view (edited-away branches are treated as retracted):
    scope = "all_branches" if ctx.features.rag_scope_all_branches else "active_path"

    # The store call is sync DB work — hopping to a thread keeps the event
    # loop serving other streams (D29). Semantic ranking engages automatically
    # when the injected embedder exists AND vectors have been backfilled (D35);
    # until then this degrades gracefully to text matching:
    def _search() -> list[Item]:
        """Reading in a worker thread, then releasing the connection immediately."""
        try:
            return _search_store(
                ctx.db, ctx.conversation_id, query,
                k=k, embedder=ctx.embedder, scope=scope, owner_id=ctx.owner_id,
            )
        finally:
            # The engine released the DB connection before streaming (P2); this
            # read re-acquired one, so it is returned to the pool here rather
            # than staying pinned idle-in-transaction for the rest of the
            # stream. Safe: returned items are detached pydantic copies:
            ctx.db.rollback()

    # Guarding the shared run session against concurrent tool calls in one model
    # step (P10): a SQLAlchemy Session is not concurrency-safe, so serialize the
    # DB critical section behind the per-run lock:
    async with ctx.db_lock:
        items = await asyncio.to_thread(_search)
    return [_digest(item) for item in items]


# ============================================================================
# web_fetch — client-side fill (D34)
# ============================================================================

# Caps chosen so a hostile page can neither exhaust memory nor flood the
# model's context — 500KB of HTML reduces to far less text than any window:
_FETCH_MAX_BYTES = 500_000
_FETCH_TIMEOUT_S = 15.0
_FETCH_MAX_TEXT_CHARS = 20_000


def _assert_public_url(url: str, addresses: list[str]) -> None:
    """
    The SSRF guard: rejecting anything that is not plain http(s) to a PUBLIC
    address. Every resolved address must be public — private, loopback,
    link-local, reserved, and multicast ranges are all blocked so a model
    fetching attacker-controlled URLs cannot reach cloud metadata endpoints
    or internal services. (Redirects are disabled at the client, so a public
    page cannot bounce the request somewhere blocked.)
    """
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise ToolExecutionError(f"Only http/https URLs are fetchable (got {parts.scheme!r})")
    for address in addresses:
        parsed = ipaddress.ip_address(address)
        if not parsed.is_global:
            raise ToolExecutionError("URL resolves to a non-public address; fetch blocked")
        # v4-embedding v6 forms: `is_global` judges only the OUTER v6 address,
        # so a private IPv4 tunneled inside NAT64 (64:ff9b::/96 well-known +
        # 64:ff9b:1::/48 local-use), 6to4, or Teredo would slip through and a
        # translating gateway would deliver it to the private target. The
        # embedded IPv4 must pass the same public test as the outer address:
        if isinstance(parsed, ipaddress.IPv6Address):
            for embedded in _embedded_ipv4(parsed):
                if not embedded.is_global:
                    raise ToolExecutionError("URL resolves to a non-public address; fetch blocked")


# The NAT64 translation prefixes (RFC 6052 well-known + RFC 8215 local-use) —
# the low 32 bits of an address under either carry a literal IPv4 address:
_NAT64_PREFIXES = (ipaddress.ip_network("64:ff9b::/96"), ipaddress.ip_network("64:ff9b:1::/48"))


def _embedded_ipv4(address: ipaddress.IPv6Address) -> list[ipaddress.IPv4Address]:
    """Extracting every IPv4 address tunneled inside a v6 form (NAT64/6to4/Teredo):"""
    found = []
    for prefix in _NAT64_PREFIXES:
        if address in prefix:
            found.append(ipaddress.IPv4Address(int(address) & 0xFFFFFFFF))
    if address.sixtofour is not None:
        found.append(address.sixtofour)
    if address.teredo is not None:
        # Teredo carries (server, client) — the client address is obfuscated
        # by XOR in the spec, which Python already undoes; check both:
        found.extend(address.teredo)
    return found


def _resolve_host(host: str, port: int) -> list[str]:
    """Resolving a hostname to its addresses (sync; callers hop via to_thread):"""
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise ToolExecutionError(f"Could not resolve host {host!r}") from exc
    return [info[4][0] for info in infos]


def _html_to_text(body: str) -> str:
    """
    Reducing HTML to readable text: dropping script/style/head blocks and
    tags, unescaping entities, collapsing whitespace. Deliberately simple —
    a fill's job is "give the model the page's text", not a DOM:
    """
    body = re.sub(r"(?is)<(script|style|head|noscript)[^>]*>.*?</\1>", " ", body)
    body = re.sub(r"(?s)<[^>]+>", " ", body)
    body = html_lib.unescape(body)
    return re.sub(r"\s+", " ", body).strip()


async def web_fetch(ctx: ToolContext, url: str) -> str:
    """
    Fetch a public web page and return its readable text content.
    """
    parts = urlsplit(url)
    if not parts.hostname:
        raise ToolExecutionError("URL has no host")

    # Resolving and validating BEFORE any connection is made (the guard):
    port = parts.port or (443 if parts.scheme == "https" else 80)
    addresses = await asyncio.to_thread(_resolve_host, parts.hostname, port)
    _assert_public_url(url, addresses)

    # Redirects stay OFF: following one would re-enter fetching with a URL the
    # guard never saw. The model gets the redirect location and may re-fetch —
    # which runs the guard again:
    async with httpx.AsyncClient(follow_redirects=False, timeout=_FETCH_TIMEOUT_S) as client:
        async with client.stream("GET", url) as response:
            if response.is_redirect:
                return f"[redirect {response.status_code}] Location: {response.headers.get('location', '?')}"
            if response.status_code >= 400:
                raise ToolExecutionError(f"Fetch failed with HTTP {response.status_code}")
            # Reading at most the byte cap — a hostile endpoint cannot stream
            # unbounded data into memory:
            chunks: list[bytes] = []
            total = 0
            async for chunk in response.aiter_bytes():
                total += len(chunk)
                if total > _FETCH_MAX_BYTES:
                    break
                chunks.append(chunk)

    text = _html_to_text(b"".join(chunks).decode("utf-8", errors="replace"))
    return text[:_FETCH_MAX_TEXT_CHARS]


# ============================================================================
# web_search — client-side fill (D34)
# ============================================================================

# DuckDuckGo's keyless HTML endpoint — the ONLY keyless option that needs no
# extra dependency. Best-effort by design: result markup can change, and when
# parsing yields nothing the model is told so honestly:
_SEARCH_ENDPOINT = "https://html.duckduckgo.com/html/"
_RESULT_LINK_RE = re.compile(r'(?is)<a[^>]*class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>')
_RESULT_SNIPPET_RE = re.compile(r'(?is)class="result__snippet"[^>]*>(.*?)</a>')


async def web_search(ctx: ToolContext, query: str, max_results: int = 5) -> list[dict]:
    """
    Search the web and return results as {title, url, snippet} entries.
    """
    max_results = max(1, min(int(max_results), 10))
    async with httpx.AsyncClient(follow_redirects=False, timeout=_FETCH_TIMEOUT_S) as client:
        response = await client.get(_SEARCH_ENDPOINT, params={"q": query})
    if response.status_code >= 400:
        raise ToolExecutionError(f"Search failed with HTTP {response.status_code}")

    body = response.text
    links = _RESULT_LINK_RE.findall(body)[:max_results]
    snippets = [_html_to_text(s) for s in _RESULT_SNIPPET_RE.findall(body)]
    if not links:
        return [{"title": None, "url": None, "snippet": "No results parsed — try web_fetch on a known URL."}]
    return [
        {
            "title": _html_to_text(title),
            "url": url,
            "snippet": snippets[i] if i < len(snippets) else "",
        }
        for i, (url, title) in enumerate(links)
    ]


# ============================================================================
# Registration — import-time, code-only (D33)
# ============================================================================

register_tool(
    "search_conversation", search_conversation,
    description=(
        "Search this conversation's full history (including turns summarized out "
        "of the visible context) and return the most relevant excerpts."
    ),
)
register_tool(
    "web_fetch", web_fetch,
    description="Fetch a public web page by URL and return its readable text content.",
)
register_tool(
    "web_search", web_search,
    description="Search the web for a query and return result titles, URLs, and snippets.",
)
