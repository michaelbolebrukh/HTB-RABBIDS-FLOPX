"""
Platform-tool tests (P5): search_conversation digests + D30 scoping, the
web_fetch fill's SSRF guard / size cap / redirect handling, and the web_search
fill's parsing. All offline — HTTP and DNS are monkeypatched.
"""

import pytest

from platform.services.generative_ai_service import platform_tools
from platform.services.generative_ai_service.engine.tool_table import ToolContext
from platform.services.generative_ai_service.engine.types import AgentFeatures, ToolExecutionError
from platform.services.generative_ai_service.platform_tools import (
    search_conversation,
    web_fetch,
    web_search,
)
from platform.services.generative_ai_service.transcript import (
    append_item,
    branch_from,
    create_conversation,
)
from platform.services.generative_ai_service.transcript.types import Item, ItemKind, Role, TextPart


def _msg(conversation_id, text, role=Role.user):
    """Building a plain message item for seeding transcripts."""
    return Item(conversation_id=conversation_id, kind=ItemKind.message, role=role,
                parts=[TextPart(text=text)])


def _ctx(db, conversation_id, **features):
    """Building a ToolContext the way the engine does (D11 injection)."""
    return ToolContext(
        db=db, owner_id="u1", conversation_id=conversation_id,
        features=AgentFeatures(**features),
    )


# ============================================================================
# search_conversation
# ============================================================================

@pytest.mark.asyncio
async def test_search_returns_digests_never_raw(db):
    conv = create_conversation(db, owner_id="u1")
    item = _msg(conv.id, "the launch code is 4242")
    item.raw = {"provider_internal": "must-not-leak"}
    append_item(db, item, owner_id="u1")

    results = await search_conversation(_ctx(db, conv.id), query="launch code")
    assert len(results) == 1
    digest = results[0]
    # The digest shape: orientation fields + bounded excerpt, nothing else:
    assert set(digest) == {"role", "kind", "tool_name", "text", "created_at"}
    assert "4242" in digest["text"]
    assert "must-not-leak" not in str(digest)


@pytest.mark.asyncio
async def test_search_scope_follows_d30(db):
    conv = create_conversation(db, owner_id="u1")
    first = append_item(db, _msg(conv.id, "shared context"), owner_id="u1")
    abandoned = branch_from(db, first.id, _msg(conv.id, "retracted alpha fact"), owner_id="u1")
    # Editing creates a sibling — the previous branch is now abandoned:
    branch_from(db, first.id, _msg(conv.id, "active beta fact"), owner_id="u1")

    # Default scope (active path) treats the edited-away branch as retracted:
    results = await search_conversation(_ctx(db, conv.id), query="fact")
    assert [r["text"] for r in results] == ["active beta fact"]

    # The per-agent D30 opt-in widens to all branches:
    results = await search_conversation(
        _ctx(db, conv.id, rag_scope_all_branches=True), query="fact",
    )
    assert {r["text"] for r in results} == {"retracted alpha fact", "active beta fact"}
    assert abandoned.id is not None  # The branch stayed STORED either way


@pytest.mark.asyncio
async def test_search_k_is_bounded_and_excerpts_truncated(db):
    conv = create_conversation(db, owner_id="u1")
    append_item(db, _msg(conv.id, "needle " + "x" * 2000), owner_id="u1")

    # A model-supplied huge k is clamped rather than honored:
    results = await search_conversation(_ctx(db, conv.id), query="needle", k=9999)
    assert len(results) == 1
    assert len(results[0]["text"]) <= platform_tools._DIGEST_EXCERPT_CHARS + 1  # +1 for the ellipsis


@pytest.mark.asyncio
async def test_search_semantic_path_uses_ctx_embedder(db):
    from platform.services.generative_ai_service.transcript import embed_pending_items

    class FakeEmbedder:
        """Ranking 'banana' texts near the query vector, everything else far:"""
        def embed(self, texts):
            return [[1.0, 0.0] if "banana" in t else [0.0, 1.0] for t in texts]

    conv = create_conversation(db, owner_id="u1")
    append_item(db, _msg(conv.id, "banana bread recipe"), owner_id="u1")
    append_item(db, _msg(conv.id, "quarterly earnings report"), owner_id="u1")
    embed_pending_items(db, conv.id, FakeEmbedder(), owner_id="u1")

    ctx = _ctx(db, conv.id)
    ctx.embedder = FakeEmbedder()
    results = await search_conversation(ctx, query="banana", k=1)
    assert results[0]["text"] == "banana bread recipe"


# ============================================================================
# web_fetch — SSRF guard and caps
# ============================================================================

@pytest.mark.asyncio
async def test_web_fetch_blocks_private_addresses(db, monkeypatch):
    conv = create_conversation(db, owner_id="u1")
    # Metadata-endpoint style target — resolution says private, fetch must die:
    monkeypatch.setattr(platform_tools, "_resolve_host", lambda host, port: ["169.254.169.254"])
    with pytest.raises(ToolExecutionError, match="non-public"):
        await web_fetch(_ctx(db, conv.id), "http://metadata.internal/latest")

    monkeypatch.setattr(platform_tools, "_resolve_host", lambda host, port: ["127.0.0.1"])
    with pytest.raises(ToolExecutionError, match="non-public"):
        await web_fetch(_ctx(db, conv.id), "http://localhost:8000/admin")


@pytest.mark.asyncio
async def test_web_fetch_rejects_non_http_schemes(db, monkeypatch):
    conv = create_conversation(db, owner_id="u1")
    monkeypatch.setattr(platform_tools, "_resolve_host", lambda host, port: ["1.2.3.4"])
    with pytest.raises(ToolExecutionError, match="http/https"):
        await web_fetch(_ctx(db, conv.id), "ftp://example.com/file")


@pytest.mark.asyncio
async def test_web_fetch_extracts_text_and_caps_size(db, monkeypatch):
    conv = create_conversation(db, owner_id="u1")
    monkeypatch.setattr(platform_tools, "_resolve_host", lambda host, port: ["93.184.216.34"])

    html = b"<html><head><title>t</title></head><body><script>evil()</script><p>Hello &amp; welcome</p></body></html>"

    class FakeResponse:
        status_code = 200
        is_redirect = False
        headers = {}

        async def aiter_bytes(self):
            yield html

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    class FakeClient:
        def __init__(self, **kwargs):
            # The guard's contract: redirects must be off at the client:
            assert kwargs.get("follow_redirects") is False

        def stream(self, method, url):
            return FakeResponse()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr(platform_tools.httpx, "AsyncClient", FakeClient)
    text = await web_fetch(_ctx(db, conv.id), "https://example.com/page")
    # Scripts and tags stripped, entities unescaped:
    assert text == "Hello & welcome"
    assert "evil" not in text


# ============================================================================
# web_search fill — DDG HTML parsing (mocked)
# ============================================================================

@pytest.mark.asyncio
async def test_web_search_parses_results(db, monkeypatch):
    conv = create_conversation(db, owner_id="u1")

    body = (
        '<a rel="nofollow" class="result__a" href="https://a.example/x">First <b>Hit</b></a>'
        '<a class="result__snippet" href="#">Snippet one</a>'
        '<a rel="nofollow" class="result__a" href="https://b.example/y">Second</a>'
    )

    class FakeResponse:
        status_code = 200
        text = body

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        async def get(self, url, params=None):
            assert params == {"q": "test query"}
            return FakeResponse()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr(platform_tools.httpx, "AsyncClient", FakeClient)
    results = await web_search(_ctx(db, conv.id), query="test query", max_results=5)
    assert results[0] == {"title": "First Hit", "url": "https://a.example/x", "snippet": "Snippet one"}
    assert results[1]["url"] == "https://b.example/y"
