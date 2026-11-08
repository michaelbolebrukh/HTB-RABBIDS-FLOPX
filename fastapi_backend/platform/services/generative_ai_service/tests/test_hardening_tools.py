"""
Phase-5 hardening tests: brutal adversarial edge cases for the tool table
(D33/D34), the platform tool fills (web_fetch SSRF guard / caps, web_search DDG
parsing, search_conversation RAG scoping), structured-output mode selection
(D20), and the D35 background embedding backfill. Complements the happy-path
suites (test_tool_table.py, test_platform_tools.py, test_tools_engine.py,
test_search.py) without duplicating them. All offline — HTTP/DNS are mocked and
the pure SSRF guard is exercised directly.
"""

import asyncio

import pytest
from pydantic_ai.exceptions import UserError
from pydantic_ai.models.function import FunctionModel

from platform.services.generative_ai_service import platform_tools
from platform.services.generative_ai_service.engine import adapter as adapter_mod
from platform.services.generative_ai_service.engine import engine as engine_mod
from platform.services.generative_ai_service.engine.providers import BuiltModel
from platform.services.generative_ai_service.engine.tool_table import (
    DUAL_IMPLEMENTATION_TOOLS,
    RegisteredTool,
    register_tool,
    split_native_tools,
)
from platform.services.generative_ai_service.engine.tool_table import ToolContext
from platform.services.generative_ai_service.engine.types import (
    AgentFeatures,
    AgentSpec,
    RunDeps,
    ToolAlreadyRegisteredError,
    ToolExecutionError,
    UserInput,
)
from platform.services.generative_ai_service.models import AISpendEvent, AITranscriptItem
from platform.services.generative_ai_service.platform_tools import (
    _assert_public_url,
    search_conversation,
    web_fetch,
    web_search,
)
from platform.services.generative_ai_service.registry.schema import Capabilities, ModelInfo
from platform.services.generative_ai_service.transcript import (
    append_item,
    branch_from,
    create_conversation,
    embed_pending_items,
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


def _info(**caps) -> ModelInfo:
    """Wrapping capability flags into a registry ModelInfo entry."""
    return ModelInfo(id="test:test", provider="test", capabilities=Capabilities(**caps))


def _fake_built(model, model_id="test:test"):
    """Wrapping a test model in the engine's BuiltModel envelope."""
    return BuiltModel(model=model, requested_id=model_id, served_by="test")


class _AxisEmbedder:
    """Deterministic 2-d embedder: axis 0 counts 'apple', axis 1 counts 'stock':"""

    def embed(self, texts):
        return [[float(t.lower().count("apple")), float(t.lower().count("stock"))] for t in texts]


# ============================================================================
# web_fetch — SSRF guard (the pure guard, exercised directly, no network)
# ============================================================================

def test_guard_blocks_ipv6_loopback_and_v4_mapped():
    # IPv6 loopback and IPv4-mapped loopback both classify non-global — the
    # guard rejects them the same as bare 127.0.0.1:
    with pytest.raises(ToolExecutionError, match="non-public"):
        _assert_public_url("http://[::1]/", ["::1"])
    with pytest.raises(ToolExecutionError, match="non-public"):
        _assert_public_url("http://x/", ["::ffff:127.0.0.1"])


def test_guard_blocks_every_reserved_range():
    # 0.0.0.0, link-local metadata, CGNAT, IPv6 ULA, and IPv6 link-local are
    # each non-global; the guard must block all of them:
    for address in ("0.0.0.0", "169.254.169.254", "100.64.0.1", "fd00::1", "fe80::1"):
        with pytest.raises(ToolExecutionError, match="non-public"):
            _assert_public_url("http://host/", [address])


def test_guard_blocks_when_any_resolved_address_is_private():
    # A name resolving to BOTH a public and a private address must be blocked —
    # the guard checks every resolved address, defeating split-horizon / DNS
    # rebinding that hides a private A record behind a public one:
    with pytest.raises(ToolExecutionError, match="non-public"):
        _assert_public_url("http://dual.example/", ["93.184.216.34", "127.0.0.1"])


def test_guard_rejects_non_http_scheme_even_with_public_ip():
    # Scheme is checked independently of address: a public IP behind gopher://
    # is still refused (only http/https are fetchable):
    with pytest.raises(ToolExecutionError, match="http/https"):
        _assert_public_url("gopher://example.com/", ["8.8.8.8"])


@pytest.mark.asyncio
async def test_web_fetch_file_scheme_is_blocked_both_ways(db, monkeypatch):
    conv = create_conversation(db, owner_id="u1")
    # file:///etc/passwd carries no host -> rejected before any resolution:
    with pytest.raises(ToolExecutionError, match="no host"):
        await web_fetch(_ctx(db, conv.id), "file:///etc/passwd")
    # file://localhost/... has a host that resolves to loopback -> blocked as
    # non-public (the scheme check runs after resolution, so either way it dies):
    monkeypatch.setattr(platform_tools, "_resolve_host", lambda host, port: ["127.0.0.1"])
    with pytest.raises(ToolExecutionError):
        await web_fetch(_ctx(db, conv.id), "file://localhost/etc/passwd")


def test_guard_blocks_v4_embedding_v6_addresses():
    # 64:ff9b::7f00:1 is the NAT64 well-known prefix (64:ff9b::/96) whose low
    # 32 bits encode 127.0.0.1; Python's ipaddress marks the OUTER address
    # is_global=True, so the guard decodes the embedded IPv4 itself and judges
    # it too (found by this test as a real bypass, then fixed):
    with pytest.raises(ToolExecutionError, match="non-public"):
        _assert_public_url("http://[64:ff9b::7f00:1]/", ["64:ff9b::7f00:1"])
    # The RFC 8215 local-use NAT64 prefix, 6to4, and Teredo forms embedding
    # private IPv4 addresses are judged the same way:
    with pytest.raises(ToolExecutionError, match="non-public"):
        _assert_public_url("http://[64:ff9b:1::a00:1]/", ["64:ff9b:1::a00:1"])
    with pytest.raises(ToolExecutionError, match="non-public"):
        _assert_public_url("http://[2002:7f00:1::1]/", ["2002:7f00:1::1"])
    # ...while a NAT64 form embedding a PUBLIC IPv4 stays fetchable:
    _assert_public_url("http://[64:ff9b::101:101]/", ["64:ff9b::101:101"])


# ============================================================================
# web_fetch — redirect posture, error handling, and the byte/char caps
# ============================================================================

def _streaming_client(monkeypatch, *, chunks=(b"",), status=200, is_redirect=False, headers=None):
    """Installing a fake httpx.AsyncClient whose stream() yields the given chunks:"""
    _headers = headers or {}

    class FakeResponse:
        status_code = status
        headers = _headers

        def __init__(self):
            self.is_redirect = is_redirect

        async def aiter_bytes(self):
            for chunk in chunks:
                yield chunk

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    class FakeClient:
        def __init__(self, **kwargs):
            assert kwargs.get("follow_redirects") is False

        def stream(self, method, url):
            return FakeResponse()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr(platform_tools.httpx, "AsyncClient", FakeClient)


@pytest.mark.asyncio
async def test_web_fetch_reports_redirect_without_following_it(db, monkeypatch):
    conv = create_conversation(db, owner_id="u1")
    monkeypatch.setattr(platform_tools, "_resolve_host", lambda host, port: ["93.184.216.34"])
    # A 302 to an internal host is NOT followed — the guard never re-validated
    # the target, so the model only gets told the Location and must re-fetch
    # (which re-runs the guard). This is the SSRF-safe redirect posture:
    _streaming_client(monkeypatch, status=302, is_redirect=True,
                      headers={"location": "http://169.254.169.254/latest"})
    text = await web_fetch(_ctx(db, conv.id), "https://example.com/go")
    assert text.startswith("[redirect 302]")
    assert "169.254.169.254" in text


@pytest.mark.asyncio
async def test_web_fetch_raises_on_http_error(db, monkeypatch):
    conv = create_conversation(db, owner_id="u1")
    monkeypatch.setattr(platform_tools, "_resolve_host", lambda host, port: ["93.184.216.34"])
    _streaming_client(monkeypatch, status=503)
    with pytest.raises(ToolExecutionError, match="HTTP 503"):
        await web_fetch(_ctx(db, conv.id), "https://example.com/down")


@pytest.mark.asyncio
async def test_web_fetch_byte_cap_drops_the_overflowing_chunk(db, monkeypatch):
    conv = create_conversation(db, owner_id="u1")
    monkeypatch.setattr(platform_tools, "_resolve_host", lambda host, port: ["93.184.216.34"])
    monkeypatch.setattr(platform_tools, "_FETCH_MAX_BYTES", 10)
    # The first chunk fills the cap exactly (kept); the next chunk pushes total
    # over the cap and is dropped WHOLE, so its marker never reaches the text:
    _streaming_client(monkeypatch, chunks=[b"0123456789", b"OMEGA-past-the-cap"])
    text = await web_fetch(_ctx(db, conv.id), "https://example.com/big")
    assert text == "0123456789"
    assert "OMEGA" not in text


@pytest.mark.asyncio
async def test_web_fetch_truncates_extracted_text_to_char_cap(db, monkeypatch):
    conv = create_conversation(db, owner_id="u1")
    monkeypatch.setattr(platform_tools, "_resolve_host", lambda host, port: ["93.184.216.34"])
    monkeypatch.setattr(platform_tools, "_FETCH_MAX_TEXT_CHARS", 5)
    _streaming_client(monkeypatch, chunks=[b"abcdefghijklmnop"])
    text = await web_fetch(_ctx(db, conv.id), "https://example.com/text")
    assert text == "abcde"


# ============================================================================
# web_search — malformed / degraded DDG HTML
# ============================================================================

def _search_client(monkeypatch, body, status=200):
    """Installing a fake httpx.AsyncClient whose get() returns the given body:"""
    class FakeResponse:
        def __init__(self):
            self.status_code = status
            self.text = body

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        async def get(self, url, params=None):
            return FakeResponse()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr(platform_tools.httpx, "AsyncClient", FakeClient)


@pytest.mark.asyncio
async def test_web_search_unparseable_html_returns_honest_sentinel(db, monkeypatch):
    conv = create_conversation(db, owner_id="u1")
    # Markup the result regex cannot match (DDG changed its layout) yields a
    # single honest sentinel rather than an empty list or a crash:
    _search_client(monkeypatch, "<div class='totally-different-markup'>no anchors here</div>")
    results = await web_search(_ctx(db, conv.id), query="anything")
    assert len(results) == 1
    assert results[0]["url"] is None
    assert "No results parsed" in results[0]["snippet"]


@pytest.mark.asyncio
async def test_web_search_tolerates_snippet_link_mismatch_and_entities(db, monkeypatch):
    conv = create_conversation(db, owner_id="u1")
    # Two result links but only ONE snippet, plus HTML entities/nested tags in a
    # title: the parser must strip markup, decode entities, and default the
    # missing snippet to "" instead of index-erroring:
    body = (
        '<a class="result__a" href="https://a.example/1">A &amp; <b>B</b></a>'
        '<a class="result__snippet" href="#">only snippet</a>'
        '<a class="result__a" href="https://b.example/2">Second</a>'
    )
    _search_client(monkeypatch, body)
    results = await web_search(_ctx(db, conv.id), query="q", max_results=5)
    assert results[0] == {"title": "A & B", "url": "https://a.example/1", "snippet": "only snippet"}
    assert results[1]["snippet"] == ""


# ============================================================================
# Tool table — duplicate registration, the D34 split's exact gate
# ============================================================================

def test_registering_an_existing_platform_tool_raises():
    # web_fetch is registered at import time; a second registration under the
    # same name must fail loudly (raises before mutating the table, so no
    # snapshot/restore is needed — the global stays intact):
    with pytest.raises(ToolAlreadyRegisteredError, match="web_fetch"):
        register_tool("web_fetch", lambda ctx: None, description="shadow")


def test_split_native_requires_dual_membership_not_just_a_flag():
    # "vision" is a real Capabilities field set True here, but it is NOT a
    # dual-implementation tool — the flag lookup only happens for DUAL names, so
    # "vision" stays a fill. Only names in DUAL_IMPLEMENTATION_TOOLS can go
    # native, and the split preserves duplicates it is handed (no dedup here):
    assert "vision" not in DUAL_IMPLEMENTATION_TOOLS
    caps = Capabilities(vision=True, web_search=True)
    native, fills = split_native_tools(["vision", "web_search", "web_search"], caps)
    assert native == ["web_search", "web_search"]
    assert fills == ["vision"]


# ============================================================================
# Tool binding — unexpected exceptions must fail the run, not become ModelRetry
# ============================================================================

@pytest.mark.asyncio
async def test_unexpected_tool_exception_is_not_swallowed_as_retry(db):
    conv = create_conversation(db, owner_id="u1")

    async def buggy_tool(ctx: ToolContext, x: str) -> str:
        """Raising a NON-ToolExecutionError (a real bug, not an expected failure):"""
        raise ValueError("genuine bug in tool body")

    entry = RegisteredTool(name="buggy_tool", fn=buggy_tool, description="boom")
    (tool,) = adapter_mod.bind_tools([entry], _ctx(db, conv.id))

    # Only ToolExecutionError becomes ModelRetry; anything else propagates so
    # the run fails loudly rather than feeding the model a fake tool result:
    with pytest.raises(ValueError, match="genuine bug"):
        await tool.function(x="hi")


# ============================================================================
# Unknown spec tool — raised BEFORE any generation token is spent
# ============================================================================

@pytest.mark.asyncio
async def test_unknown_tool_fails_before_the_model_ever_streams(db, monkeypatch):
    async def exploding_stream(messages, info):
        """Asserting the model is NEVER driven when a tool name is unknown:"""
        raise AssertionError("model streamed despite an unresolved tool")
        yield  # pragma: no cover - makes this an async generator

    monkeypatch.setattr(
        engine_mod, "build_model",
        lambda model_id, fallbacks=None: _fake_built(FunctionModel(stream_function=exploding_stream)),
    )
    conv = create_conversation(db, owner_id="u1")
    spec = AgentSpec(name="a", model="test:test", tools=["ghost_tool"],
                     features=AgentFeatures(rag_search=False))
    deps = RunDeps(db=db, owner_id="u1", conversation_id=conv.id)

    events = [e async for e in engine_mod.stream_agent(spec, deps, UserInput(text="hi"))]
    # The run failed at tool resolution — the failure names the ghost tool, and
    # the model's stream never ran (else the AssertionError would surface):
    assert events[-1].type == "run_failed"
    assert "ghost_tool" in events[-1].error
    assert "AssertionError" not in events[-1].error


# ============================================================================
# Structured output (D20) — invalid schema fails loudly at mode selection
# ============================================================================

def test_choose_output_mode_rejects_a_non_object_schema():
    # A malformed output schema (not a JSON object) must raise at construction,
    # never silently degrade to free text — build_agent propagates this:
    with pytest.raises(UserError):
        adapter_mod.choose_output_mode({"type": "definitely-not-a-type"}, _info(structured_output=True))


# ============================================================================
# RAG — semantic search over an edited-away branch, scope on/off
# ============================================================================

@pytest.mark.asyncio
async def test_tool_semantic_search_honors_active_path_then_widens(db):
    conv = create_conversation(db, owner_id="u1")
    first = append_item(db, _msg(conv.id, "apple apple apple"), owner_id="u1")
    # Editing `first` retracts it — the sibling becomes the active leaf:
    branch_from(db, first.parent_id, _msg(conv.id, "stock talk only"), owner_id="u1")
    embed_pending_items(db, conv.id, _AxisEmbedder(), owner_id="u1")  # both items carry vectors

    # Default D30 scope: the retracted pure-apple item has a vector but is out
    # of the active path, so a semantic 'apple' query never surfaces it:
    ctx = _ctx(db, conv.id)
    ctx.embedder = _AxisEmbedder()
    results = await search_conversation(ctx, query="apple")
    assert [r["text"] for r in results] == ["stock talk only"]

    # The per-agent opt-in widens to all branches and recovers it:
    ctx = _ctx(db, conv.id, rag_scope_all_branches=True)
    ctx.embedder = _AxisEmbedder()
    results = await search_conversation(ctx, query="apple")
    assert "apple apple apple" in {r["text"] for r in results}


# ============================================================================
# D35 backfill — graceful degradation when no session_factory is provided
# ============================================================================

@pytest.mark.asyncio
async def test_backfill_is_skipped_when_session_factory_is_none(db, monkeypatch):
    monkeypatch.setattr(
        engine_mod, "build_model",
        lambda model_id, fallbacks=None: _fake_built(
            __import__("pydantic_ai.models.test", fromlist=["TestModel"]).TestModel(custom_output_text="ok")
        ),
    )
    # A working embedder is available, but with no session_factory the engine
    # must NOT schedule a detached task against the request's own session
    # (which dies with the response) — the run still completes cleanly:
    monkeypatch.setattr(adapter_mod, "make_embedder", lambda: _AxisEmbedder())
    before = set(engine_mod._BACKFILL_TASKS)

    conv = create_conversation(db, owner_id="u1")
    deps = RunDeps(db=db, owner_id="u1", conversation_id=conv.id)  # session_factory defaults to None
    spec = AgentSpec(name="a", model="test:test")

    events = [e async for e in engine_mod.stream_agent(spec, deps, UserInput(text="remember"))]
    assert events[-1].type == "run_completed"

    # No backfill task was scheduled, and nothing was embedded or billed:
    assert set(engine_mod._BACKFILL_TASKS) == before
    rows = db.query(AITranscriptItem).filter_by(conversation_id=conv.id).all()
    assert all(r.embedding is None for r in rows)
    assert db.query(AISpendEvent).filter_by(owner_id="u1", kind="embedding").count() == 0


# ============================================================================
# D35 backfill — an embedder that fails partway leaves the run untouched
# ============================================================================

@pytest.mark.asyncio
async def test_backfill_embedder_crash_never_bills_or_breaks_the_run(db, db_factory, monkeypatch):
    from pydantic_ai.models.test import TestModel

    monkeypatch.setattr(
        engine_mod, "build_model",
        lambda model_id, fallbacks=None: _fake_built(TestModel(custom_output_text="ok")),
    )

    class _CrashingEmbedder:
        """Raising once the backfill actually calls embed() (mid-pass failure):"""
        def embed(self, texts):
            raise RuntimeError("embeddings backend exploded")

    monkeypatch.setattr(adapter_mod, "make_embedder", lambda: _CrashingEmbedder())

    conv = create_conversation(db, owner_id="u1")
    deps = RunDeps(db=db, owner_id="u1", conversation_id=conv.id, session_factory=db_factory)
    spec = AgentSpec(name="a", model="test:test")

    events = [e async for e in engine_mod.stream_agent(spec, deps, UserInput(text="hello"))]
    assert events[-1].type == "run_completed"

    # Draining the detached task: it must swallow the crash (D35 best-effort):
    await asyncio.gather(*engine_mod._BACKFILL_TASKS, return_exceptions=True)

    # No vectors written and NO embedding spend row despite the run completing —
    # the failed backfill degrades search to text matching, silently:
    rows = db.query(AITranscriptItem).filter_by(conversation_id=conv.id).all()
    assert all(r.embedding is None for r in rows)
    assert db.query(AISpendEvent).filter_by(owner_id="u1", kind="embedding").count() == 0
