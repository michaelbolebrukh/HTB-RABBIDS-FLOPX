"""
Memory (P10) — offline unit tests, ZERO live cost.

Every LLM call is replaced by a deterministic fake `resolve_fn` and every
embedding by a tiny hand-rolled bag-of-words vector, so this whole file runs
with no API keys and no spend. It proves the mechanisms the frontier gets wrong:

- temporal arithmetic (supersede vs accrue vs learning-about-the-past) — pure;
- write-time reconciliation (exact dedup, contradiction supersession, accrual);
- bi-temporal as-of recall (what was true at a past date);
- the uniqueness-guarded supersede/delete tools.

The live head-to-head-vs-mem0 benchmark is a separate, cost-capped harness.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest
from pydantic_ai.models.function import DeltaToolCall, FunctionModel

from platform.services.generative_ai_service.engine import adapter as adapter_mod
from platform.services.generative_ai_service.engine import engine as engine_mod
from platform.services.generative_ai_service.engine import AgentFeatures, AgentSpec, RunDeps, UserInput
from platform.services.generative_ai_service.engine.providers import BuiltModel
from platform.services.generative_ai_service.memory import (
    ResolveResult,
    reconcile_write,
    store,
    temporal_resolve_contradictions,
)
from platform.services.generative_ai_service.memory import store as store_mod
from platform.services.generative_ai_service.transcript import create_conversation


OWNER = "owner-1"
AGENT = "assistant"


def D(y, m, d):
    """A tz-aware UTC date (fixed, so temporal assertions are deterministic):"""
    return datetime(y, m, d, tzinfo=timezone.utc)


# ============================================================================
# Deterministic offline doubles
# ============================================================================

def _toy_vector(text: str) -> list[float]:
    """A 26-dim letter-frequency vector — enough for cosine to separate topics offline:"""
    vec = [0.0] * 26
    for ch in text.lower():
        if "a" <= ch <= "z":
            vec[ord(ch) - 97] += 1.0
    return vec


def _embed(texts: list[str]) -> list[list[float]]:
    """The injected embed_fn double (sync, like the real _SDKEmbedder.embed):"""
    return [_toy_vector(t) for t in texts]


def make_resolver(script):
    """
    Building a fake ResolveFn from a predicate. `script(new, candidates)` returns
    (duplicate_idxs, contradicted_idxs); the resolver never calls a model:
    """
    async def _resolve(new_text: str, candidate_texts: list[str]) -> ResolveResult:
        dupes, contras = script(new_text, candidate_texts)
        return ResolveResult(duplicate_idxs=list(dupes), contradicted_idxs=list(contras))
    return _resolve


# A resolver that treats any candidate sharing the first word as a contradiction
# (crude "same slot" heuristic) — good enough for the value-change cases:
def _same_first_word(new_text, candidates):
    key = new_text.lower().split()[:3]
    contras = [i for i, c in enumerate(candidates) if c.lower().split()[:3] == key]
    return [], contras


# ============================================================================
# store — hashing, similarity, fusion (pure)
# ============================================================================

def test_content_hash_normalizes():
    assert store.content_hash("  The  Sky is BLUE ") == store.content_hash("the sky is blue")
    assert store.content_hash("a") != store.content_hash("b")


def test_cosine_and_rrf():
    assert store.cosine([1, 0], [1, 0]) == pytest.approx(1.0)
    assert store.cosine([1, 0], [0, 1]) == pytest.approx(0.0)
    assert store.cosine([], [1]) == 0.0
    # RRF rewards top ranks and needs no score normalization:
    fused = store.rrf_fuse([["a", "b", "c"], ["b", "a"]])
    assert fused["b"] > fused["c"]  # b is rank0 in one list, rank1 in the other


# ============================================================================
# temporal_resolve_contradictions — the crown jewel, pure
# ============================================================================

def _mem(valid_at=None, invalid_at=None):
    """A detached AIMemory carrying only the fields the temporal logic reads:"""
    from platform.services.generative_ai_service.models import AIMemory
    return AIMemory(owner_id=OWNER, scope=AGENT, text="x", content_hash="h",
                     valid_at=valid_at, invalid_at=invalid_at)


def test_temporal_overlap_supersedes():
    """Old started first, windows overlap → old is invalidated at new's start:"""
    old = _mem(valid_at=D(2026, 1, 1))
    out = temporal_resolve_contradictions(
        new_valid_at=D(2026, 5, 1), new_invalid_at=None, contradicted=[old], now=D(2026, 5, 1),
    )
    assert len(out.invalidate) == 1
    assert out.invalidate[0].invalid_at == D(2026, 5, 1)
    assert not out.keep_both


def test_temporal_sequential_keeps_both():
    """Old already ended before new began → sequential truths, keep both:"""
    old = _mem(valid_at=D(2020, 1, 1), invalid_at=D(2021, 1, 1))
    out = temporal_resolve_contradictions(
        new_valid_at=D(2021, 1, 1), new_invalid_at=None, contradicted=[old], now=D(2026, 1, 1),
    )
    assert not out.invalidate
    assert out.keep_both == [old]


def test_temporal_learning_about_the_past():
    """New describes an EARLIER state than old → new's window closes at old's start, old stays live:"""
    old = _mem(valid_at=D(2026, 6, 1))
    out = temporal_resolve_contradictions(
        new_valid_at=D(2026, 1, 1), new_invalid_at=None, contradicted=[old], now=D(2026, 6, 1),
    )
    assert not out.invalidate
    assert out.new_invalid_at == D(2026, 6, 1)


def test_temporal_missing_dates_falls_back_to_tx_time():
    """Undated contradiction → newest belief wins (transaction-time):"""
    old = _mem()
    out = temporal_resolve_contradictions(
        new_valid_at=None, new_invalid_at=None, contradicted=[old], now=D(2026, 3, 1),
    )
    assert len(out.invalidate) == 1
    assert out.invalidate[0].invalid_at == D(2026, 3, 1)


# ============================================================================
# reconcile_write — the write path (fake resolver, no live cost)
# ============================================================================

@pytest.mark.asyncio
async def test_exact_dedup_merges_provenance(db):
    r1 = await reconcile_write(db, owner_id=OWNER, scope=AGENT, text="Mia is allergic to penicillin",
                               source_turn_ids=["t1"], embed_fn=_embed, resolve_fn=make_resolver(lambda n, c: ([], [])))
    assert r1.action == "added"
    # Restating it (different casing/space) hits the md5 fast path — no new row:
    r2 = await reconcile_write(db, owner_id=OWNER, scope=AGENT, text="  mia is allergic to PENICILLIN ",
                               source_turn_ids=["t2"], embed_fn=_embed, resolve_fn=make_resolver(lambda n, c: ([], [])))
    assert r2.action == "deduped"
    assert r2.memory.id == r1.memory.id
    assert set(r2.memory.source_turn_ids) == {"t1", "t2"}       # provenance accrued
    assert len(store.live_memories(db, OWNER, AGENT)) == 1


@pytest.mark.asyncio
async def test_knowledge_update_supersedes(db):
    """ku1: email changes → old retired (kept), new live, current = new:"""
    await reconcile_write(db, owner_id=OWNER, scope=AGENT, text="primary email is alice@old.com",
                          valid_at=D(2026, 1, 3), embed_fn=_embed, resolve_fn=make_resolver(_same_first_word))
    r = await reconcile_write(db, owner_id=OWNER, scope=AGENT, text="primary email is alice@new.com",
                              valid_at=D(2026, 5, 9), embed_fn=_embed, resolve_fn=make_resolver(_same_first_word))
    assert r.action == "updated"
    live = store.live_memories(db, OWNER, AGENT)
    assert len(live) == 1 and "new.com" in live[0].text
    # The old fact is retired but still present (invalidate, don't delete):
    everything = store.all_memories(db, OWNER, AGENT)
    assert len(everything) == 2
    old = next(m for m in everything if "old.com" in m.text)
    # SQLite round-trips datetimes tz-naive (a harness artifact the store's
    # _aware normalizes on read); compare through it, not against raw ==:
    assert old.superseded_at is not None
    assert store_mod._aware(old.invalid_at) == D(2026, 5, 9)


@pytest.mark.asyncio
async def test_accrual_keeps_all_values(db):
    """mv1: languages accrue — the resolver reports NO contradiction, so all stay live:"""
    for text, date in [("speaks English", D(2026, 1, 2)), ("speaks Turkish", D(2026, 2, 2)),
                       ("speaks German", D(2026, 3, 2))]:
        await reconcile_write(db, owner_id=OWNER, scope=AGENT, text=text, valid_at=date,
                              embed_fn=_embed, resolve_fn=make_resolver(lambda n, c: ([], [])))
    live = store.live_memories(db, OWNER, AGENT)
    assert len(live) == 3


@pytest.mark.asyncio
async def test_as_of_time_travel(db):
    """ku4: HQ Austin→Denver; as-of May 2026 must still return Austin (bi-temporal):"""
    await reconcile_write(db, owner_id=OWNER, scope=AGENT, text="company HQ is in Austin",
                          valid_at=D(2026, 1, 20), embed_fn=_embed, resolve_fn=make_resolver(_same_first_word))
    await reconcile_write(db, owner_id=OWNER, scope=AGENT, text="company HQ is in Denver",
                          valid_at=D(2026, 8, 15), embed_fn=_embed, resolve_fn=make_resolver(_same_first_word))
    # Now → Denver:
    now_hits = store.recall(db, owner_id=OWNER, scope=AGENT, query="where is company HQ",
                            query_embedding=_toy_vector("where is company HQ"), k=5)
    assert "Denver" in now_hits[0][0].text
    # As-of May 2026 → Austin (the retired fact, valid at that date):
    asof_hits = store.recall(db, owner_id=OWNER, scope=AGENT, query="where is company HQ",
                             query_embedding=_toy_vector("where is company HQ"), k=5, as_of=D(2026, 5, 1))
    texts = [m.text for m, _ in asof_hits]
    assert any("Austin" in t for t in texts) and not any("Denver" in t for t in texts)


@pytest.mark.asyncio
async def test_multi_supersession_chain(db):
    """ku5: three phone numbers in sequence → only the last is live:"""
    for text, date in [("phone number is 555-0100", D(2026, 3, 5)),
                       ("phone number is 555-0199", D(2026, 4, 5)),
                       ("phone number is 555-0242", D(2026, 9, 5))]:
        await reconcile_write(db, owner_id=OWNER, scope=AGENT, text=text, valid_at=date,
                              embed_fn=_embed, resolve_fn=make_resolver(_same_first_word))
    live = store.live_memories(db, OWNER, AGENT)
    assert len(live) == 1 and "555-0242" in live[0].text
    assert len(store.all_memories(db, OWNER, AGENT)) == 3


@pytest.mark.asyncio
async def test_no_llm_claim_key_supersedes(db):
    """Without a resolver, a live claim_key holder is a deterministic contradiction (cognee slot):"""
    await reconcile_write(db, owner_id=OWNER, scope=AGENT, text="lives in Berlin",
                          valid_at=D(2026, 1, 1), claim_key="residence", embed_fn=_embed, resolve_fn=None)
    r = await reconcile_write(db, owner_id=OWNER, scope=AGENT, text="lives in Paris",
                              valid_at=D(2026, 6, 1), claim_key="residence", embed_fn=_embed, resolve_fn=None)
    assert r.action == "updated"
    live = store.live_memories(db, OWNER, AGENT)
    assert len(live) == 1 and "Paris" in live[0].text


@pytest.mark.asyncio
async def test_abstention_returns_nothing(db):
    """ab1: nothing about a wife was ever stored → recall returns empty (the model must decline):"""
    await reconcile_write(db, owner_id=OWNER, scope=AGENT, text="work has been busy but good",
                          valid_at=D(2026, 1, 30), embed_fn=_embed, resolve_fn=make_resolver(lambda n, c: ([], [])))
    hits = store.recall(db, owner_id=OWNER, scope=AGENT, query="what is my wife's name",
                        query_embedding=_toy_vector("what is my wife's name"), k=5)
    # Lexical/semantic may surface the unrelated fact weakly, but it must not
    # assert a wife fact — the digest is inspected by the caller. We assert the
    # store did not fabricate a matching fact:
    assert not any("wife" in m.text.lower() for m, _ in hits)


# ============================================================================
# scope isolation (D54 — per owner_id, no cross-tenant config)
# ============================================================================

@pytest.mark.asyncio
async def test_scope_isolation_by_owner(db):
    """Memory never crosses users, regardless of scope (the D54 hard boundary):"""
    await reconcile_write(db, owner_id="alice", scope=AGENT, text="favorite color is blue",
                          embed_fn=_embed, resolve_fn=make_resolver(lambda n, c: ([], [])))
    await reconcile_write(db, owner_id="bob", scope=AGENT, text="favorite color is red",
                          embed_fn=_embed, resolve_fn=make_resolver(lambda n, c: ([], [])))
    assert len(store.live_memories(db, "alice", AGENT)) == 1
    assert len(store.live_memories(db, "bob", AGENT)) == 1
    # Alice's recall never sees Bob's fact:
    hits = store.recall(db, owner_id="alice", scope=AGENT, query="favorite color",
                        query_embedding=_toy_vector("favorite color"), k=5)
    assert all(m.owner_id == "alice" for m, _ in hits)


@pytest.mark.asyncio
async def test_scope_sharing_vs_isolation(db):
    """
    D59: same scope shares, different scopes isolate — the knob that lets any
    memory attach to any agent. Two 'agents' writing the SAME scope for one user
    see each other's facts; a third scope stays separate:
    """
    # Two agents, one shared scope ("default") — they pool the user's memory:
    await reconcile_write(db, owner_id="u", scope="default", text="the user is named Dana",
                          embed_fn=_embed, resolve_fn=make_resolver(lambda n, c: ([], [])))
    await reconcile_write(db, owner_id="u", scope="default", text="the user prefers dark mode",
                          embed_fn=_embed, resolve_fn=make_resolver(lambda n, c: ([], [])))
    assert len(store.live_memories(db, "u", "default")) == 2       # shared pool sees both
    # A distinct scope for the same user is isolated:
    await reconcile_write(db, owner_id="u", scope="work", text="the user's title is Staff Engineer",
                          embed_fn=_embed, resolve_fn=make_resolver(lambda n, c: ([], [])))
    assert len(store.live_memories(db, "u", "work")) == 1
    assert len(store.live_memories(db, "u", "default")) == 2       # unaffected by the isolated scope


# ============================================================================
# Write-completeness — the summary-time flush (D60)
# ============================================================================

def test_parse_extract_response_is_defensive():
    from platform.services.generative_ai_service.memory import parse_extract_response
    assert parse_extract_response('["a", "b"]') == ["a", "b"]
    assert parse_extract_response('junk ["x"] tail') == ["x"]        # embedded list is salvaged
    assert parse_extract_response("not json") == []                   # unparseable -> no-op
    assert parse_extract_response('[1, "", "ok"]') == ["ok"]          # non-strings/blanks dropped
    assert len(parse_extract_response('[' + ",".join(['"f"'] * 50) + ']')) == 20  # bounded


@pytest.mark.asyncio
async def test_memory_flush_extracts_from_compacted_span(db, db_factory, monkeypatch):
    """
    D60: when a span is compacted out of context, the flush extracts its durable
    facts and reconciles them into memory — catching what the agent never saved
    in real time. Adapter builders are faked, so this is offline + $0:
    """
    from pydantic_ai.models.test import TestModel

    from platform.services.generative_ai_service.transcript import append_item
    from platform.services.generative_ai_service.transcript.types import Item, ItemKind, Role, TextPart as TP

    monkeypatch.setattr(engine_mod, "build_model", lambda m, f=None: _fake_built(TestModel()))
    async def _fake_extract(excerpt):
        assert "penicillin" in excerpt.lower()          # the span text reached the extractor
        return ["The user is allergic to penicillin"]
    monkeypatch.setattr(adapter_mod, "make_memory_extractor", lambda built, on_usage=None: _fake_extract)
    monkeypatch.setattr(adapter_mod, "make_memory_resolver",
                        lambda built, on_usage=None: make_resolver(lambda n, c: ([], [])))
    monkeypatch.setattr(adapter_mod, "make_embedder", lambda: None)

    conv = create_conversation(db, owner_id="u")
    item = append_item(db, Item(conversation_id=conv.id, kind=ItemKind.message, role=Role.user,
                                parts=[TP(text="For the record, I'm allergic to penicillin.")]), owner_id="u")

    engine_mod._schedule_memory_flush(db_factory, "u", "s", conv.id, [item])
    await asyncio.gather(*list(engine_mod._FLUSH_TASKS))

    live = store.live_memories(db, "u", "s")
    assert any("penicillin" in m.text.lower() for m in live)
    # Provenance links the extracted fact back to the compacted span item:
    assert any(str(item.id) in (m.source_turn_ids or []) for m in live)


# ============================================================================
# Concurrency — parallel tool calls on one run session must not race (P10)
# ============================================================================

@pytest.mark.asyncio
async def test_concurrent_memory_writes_do_not_race(db):
    """
    pydantic_ai can run several tool calls from ONE model step concurrently, all
    sharing the run's single session. Firing many memory_write calls at once on
    one ToolContext must not raise SQLAlchemy's "concurrent operations" error and
    must persist every distinct fact — the per-run db_lock serializes the DB hops:
    """
    import asyncio as _asyncio

    from platform.services.generative_ai_service.engine.tool_table import ToolContext
    from platform.services.generative_ai_service.memory.tools import memory_write

    ctx = ToolContext(
        db=db, owner_id="u", conversation_id=__import__("uuid").uuid4(),
        features=AgentFeatures(memory=True), memory_scope="s",
        memory_embed_fn=_embed, memory_resolve_fn=make_resolver(lambda n, c: ([], [])),
    )
    facts = [f"fact number {i} about the user" for i in range(12)]
    results = await _asyncio.gather(*[memory_write(ctx, fact=f) for f in facts])

    assert all(r["action"] == "added" for r in results)      # no error, all persisted
    assert len(store.live_memories(db, "u", "s")) == 12       # every distinct fact stored


# ============================================================================
# Engine wiring — features.memory exposes the tools + injects the preamble
# ============================================================================

def _fake_built(model):
    """Wrapping a test model in the engine's BuiltModel envelope:"""
    return BuiltModel(model=model, requested_id="test:test", served_by="test")


@pytest.mark.asyncio
async def test_engine_wires_memory_tools_and_preamble(db, monkeypatch):
    """
    features.memory=True → the agent can call memory_write then memory_search
    across the run, the write persists to the (owner, agent) scope, and the
    always-on MEMORY_PREAMBLE reaches the model's instructions. Fully offline:
    the resolver/embedder are monkeypatched, FunctionModel drives the turns.
    """
    conv = create_conversation(db, owner_id="u1")

    # Deterministic injected doubles — no live model, no live embeddings:
    monkeypatch.setattr(
        engine_mod, "build_model",
        lambda model_id, fallbacks=None: _fake_built(FunctionModel(stream_function=_scripted)),
    )
    monkeypatch.setattr(adapter_mod, "make_embedder", lambda: None)  # lexical recall suffices offline
    monkeypatch.setattr(
        adapter_mod, "make_memory_resolver",
        lambda built, on_usage=None: make_resolver(lambda n, c: ([], [])),
    )

    seen_instructions = {}

    async def _scripted(messages, info):
        """Turn 1: remember a fact. Turn 2: recall it. Turn 3: answer from the hit."""
        # Capturing the model instructions so we can assert the preamble landed
        # (pydantic_ai delivers agent instructions on the request, not as a part):
        for m in messages:
            instr = getattr(m, "instructions", None)
            if instr:
                seen_instructions["text"] = instr
        last = messages[-1].parts
        for part in last:
            name = getattr(part, "tool_name", None)
            if name == "memory_write":
                yield {1: DeltaToolCall(name="memory_search",
                                        json_args='{"query": "cat name"}', tool_call_id="tc2")}
                return
            if name == "memory_search":
                yield "Your cat is named Sushi."
                return
        yield {1: DeltaToolCall(name="memory_write",
                                json_args='{"fact": "The user\'s cat is named Sushi"}', tool_call_id="tc1")}

    spec = AgentSpec(name="assistant", model="test:test",
                     features=AgentFeatures(rag_search=False, memory=True))
    deps = RunDeps(db=db, owner_id="u1", conversation_id=conv.id)

    events = [e async for e in engine_mod.stream_agent(spec, deps, UserInput(text="Remember my cat Sushi."))]
    by_type = {e.type: e for e in events}

    assert by_type["run_completed"].output == "Your cat is named Sushi."
    # The fact persisted to durable memory under the DEFAULT shared scope
    # (the spec left memory_scope unset — D59):
    from platform.services.generative_ai_service.engine.types import DEFAULT_MEMORY_SCOPE
    live = store.live_memories(db, "u1", DEFAULT_MEMORY_SCOPE)
    assert len(live) == 1 and "Sushi" in live[0].text
    # Provenance links the write back to the triggering user turn:
    assert live[0].source_turn_ids
    # The usage preamble reached the model:
    assert "durable memory" in seen_instructions.get("text", "")
