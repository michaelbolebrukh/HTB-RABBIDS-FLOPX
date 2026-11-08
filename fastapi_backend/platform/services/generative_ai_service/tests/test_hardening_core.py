"""
Hardening tests (Phases 1-4): adversarial edge cases the phase suites do not
cover — deep branch surgery (edits of edits, mid-branch regeneration), atomic
append failure, summarization boundary conditions, fallback-chain exhaustion,
fail-closed budget checks, pricing extremes, and registry merge/default
semantics. All offline and deterministic.
"""

import json
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from pydantic_ai.models.fallback import FallbackModel
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.test import TestModel

from platform.services.generative_ai_service.billing import (
    PessimisticRates,
    check_budget,
    price_usage,
    record_spend,
    window_spend,
    window_start,
)
from platform.services.generative_ai_service.billing.pricing import PriceResult
from platform.services.generative_ai_service.engine import (
    AgentFeatures,
    AgentSpec,
    RunDeps,
    UserInput,
    run_agent,
    stream_agent,
)
from platform.services.generative_ai_service.engine import engine as engine_mod
from platform.services.generative_ai_service.engine import providers
from platform.services.generative_ai_service.engine.model_info import ModelInfo as EngineModelInfo
from platform.services.generative_ai_service.engine.providers import BuiltModel
from platform.services.generative_ai_service.engine.renderer import render_context
from platform.services.generative_ai_service.exceptions import (
    InvalidItemError,
    ItemNotFoundError,
)
from platform.services.generative_ai_service.models import AIBudget
from platform.services.generative_ai_service.registry import Cost, ModelInfo, get_model_info
from platform.services.generative_ai_service.registry.loader import _FLOOR, _build_index, _merge_entry
from platform.services.generative_ai_service.transcript import (
    append_item,
    append_items,
    branch_from,
    create_conversation,
    full_history,
    get_conversation,
    get_current_path,
    get_path,
    set_current,
)
from platform.services.generative_ai_service.transcript.types import (
    Item,
    ItemKind,
    Role,
    TextPart,
    Usage,
)


# A fixed "now" so window math never flakes (mid-month, mid-week, mid-day UTC):
NOW = datetime(2026, 7, 15, 12, 0, 0, tzinfo=timezone.utc)

# Chat-path specs disable tools (TestModel calls every tool it is given):
_NO_TOOLS = AgentFeatures(rag_search=False)


def _msg(conversation_id, text, role=Role.user, estimate=None):
    """Building a minimal message item, optionally pinning its token estimate:"""
    item = Item(conversation_id=conversation_id, kind=ItemKind.message, role=role, parts=[TextPart(text=text)])
    if estimate is not None:
        item.token_estimates = {"test": estimate}
    return item


def _spend(db, owner, micro, when, **kw):
    """Recording one deterministic ledger row for budget tests:"""
    return record_spend(
        db, owner_id=owner, model_id=kw.pop("model_id", "t:m"), usage=Usage(),
        price=PriceResult(cost_microusd=micro, priced=True), created_at=when, **kw,
    )


# ============================================================================
# TRANSCRIPT — BRANCH SURGERY (edits of edits, mid-branch regeneration)
# ============================================================================

def test_edit_of_an_edit_keeps_every_branch_reachable(db):
    conv = create_conversation(db, owner_id="u1")
    q1 = append_item(db, _msg(conv.id, "q1"), owner_id="u1")
    a1 = append_item(db, _msg(conv.id, "a1", Role.assistant), owner_id="u1")
    q2 = append_item(db, _msg(conv.id, "q2"), owner_id="u1")

    # Editing q2, then editing the edit — three siblings under a1:
    edit1 = branch_from(db, a1.id, _msg(conv.id, "q2-edit"), owner_id="u1")
    edit2 = branch_from(db, a1.id, _msg(conv.id, "q2-edit-of-edit"), owner_id="u1")
    assert q2.parent_id == edit1.parent_id == edit2.parent_id == a1.id

    # The active path follows the newest edit; nothing was lost:
    assert [i.id for i in get_current_path(db, conv.id, owner_id="u1")] == [q1.id, a1.id, edit2.id]
    assert len(full_history(db, conv.id, owner_id="u1")) == 5

    # Every sibling branch remains navigable, including the middle edit:
    set_current(db, conv.id, edit1.id, owner_id="u1")
    assert [i.id for i in get_current_path(db, conv.id, owner_id="u1")] == [q1.id, a1.id, edit1.id]
    set_current(db, conv.id, q2.id, owner_id="u1")
    assert [i.id for i in get_current_path(db, conv.id, owner_id="u1")] == [q1.id, a1.id, q2.id]


def test_regenerate_mid_branch_keeps_old_subtree_intact(db):
    conv = create_conversation(db, owner_id="u1")
    chain = [append_item(db, _msg(conv.id, t, Role.user if i % 2 == 0 else Role.assistant), owner_id="u1")
             for i, t in enumerate(["q1", "a1", "q2", "a2", "q3", "a3"])]
    q2, a3 = chain[2], chain[5]

    # Regenerating a2 mid-branch (a sibling assistant answer under q2), then
    # continuing the conversation on the regenerated branch:
    regen = branch_from(db, q2.id, _msg(conv.id, "a2-regenerated", Role.assistant), owner_id="u1")
    q4 = append_item(db, _msg(conv.id, "q4"), owner_id="u1")
    assert q4.parent_id == regen.id

    # The new path shares the prefix up to q2 and never crosses the old branch:
    new_path = [i.id for i in get_current_path(db, conv.id, owner_id="u1")]
    assert new_path == [chain[0].id, chain[1].id, q2.id, regen.id, q4.id]

    # The abandoned subtree stays fully reconstructable from its old leaf:
    old_path = [i.id for i in get_path(db, a3.id, owner_id="u1")]
    assert old_path == [i.id for i in chain]

    # Full history holds everything from both branches, in insertion order:
    assert [i.id for i in full_history(db, conv.id, owner_id="u1")] == [i.id for i in chain] + [regen.id, q4.id]


def test_append_items_failure_persists_nothing(db):
    conv_a = create_conversation(db, owner_id="u1")
    conv_b = create_conversation(db, owner_id="u1")
    anchor = append_item(db, _msg(conv_a.id, "anchor"), owner_id="u1")

    # A batch mixing conversations must fail atomically — the valid first item
    # must NOT survive the invalid second one (one transaction, one commit):
    with pytest.raises(InvalidItemError):
        append_items(db, [_msg(conv_a.id, "ok"), _msg(conv_b.id, "wrong conversation")], owner_id="u1")
    assert [i.id for i in full_history(db, conv_a.id, owner_id="u1")] == [anchor.id]
    assert get_conversation(db, conv_a.id, owner_id="u1").current_item_id == anchor.id

    # An empty batch is a no-op, not an error:
    assert append_items(db, [], owner_id="u1") == []
    assert get_conversation(db, conv_a.id, owner_id="u1").current_item_id == anchor.id


def test_unknown_parent_and_cross_conversation_set_current_raise(db):
    conv_a = create_conversation(db, owner_id="u1")
    conv_b = create_conversation(db, owner_id="u1")
    foreign = append_item(db, _msg(conv_b.id, "elsewhere"), owner_id="u1")

    # An explicit parent that does not exist anywhere is a loud failure:
    orphan = _msg(conv_a.id, "orphan")
    orphan.parent_id = uuid4()
    with pytest.raises(ItemNotFoundError):
        append_item(db, orphan, owner_id="u1")

    # Pointing a conversation's leaf at another conversation's item is too:
    with pytest.raises(ItemNotFoundError):
        set_current(db, conv_a.id, foreign.id, owner_id="u1")


# ============================================================================
# RENDERER — SUMMARIZATION BOUNDARY CONDITIONS
# ============================================================================

def _window(window):
    return EngineModelInfo(id="test:m", provider="test", context_window=window, tokenizer_family="test")


@pytest.mark.asyncio
async def test_exactly_at_trigger_boundary_flips_to_summarization():
    # window=1000, trigger_pct=0.75 -> trigger=750. Pinning estimates via the
    # token_estimates cache so the boundary is exact, not approximate:
    conv = uuid4()
    budgets = []

    async def summarize(items, budget):
        budgets.append(budget)
        return "SUMMARY"

    # One token UNDER the trigger -> the full path, untouched:
    under = [_msg(conv, "a", Role.user, estimate=375), _msg(conv, "b", Role.assistant, estimate=374)]
    for item in under:
        item.id = uuid4()
    ctx = await render_context(under, _window(1000), summarize=summarize,
                               cache_get=lambda s, b, f: None, cache_put=lambda b, f, s, e: None)
    assert not ctx.was_summarized and len(ctx.items) == 2

    # EXACTLY at the trigger -> summarization engages (>= semantics, not >):
    at = [_msg(conv, "a", Role.user, estimate=375), _msg(conv, "b", Role.assistant, estimate=375)]
    for item in at:
        item.id = uuid4()
    ctx = await render_context(at, _window(1000), summarize=summarize,
                               cache_get=lambda s, b, f: None, cache_put=lambda b, f, s, e: None)
    assert ctx.was_summarized and ctx.summary_text == "SUMMARY"
    # The summary budget was quantized up to the smallest cache bucket (O6.6):
    assert budgets == [2000]


@pytest.mark.asyncio
async def test_second_render_reuses_cached_summary_through_put_get_keys():
    # A dict-backed cache proves cache_put writes under the SAME key cache_get
    # reads (span_end_item_id, budget, family) — the D24 hit-rate contract:
    conv = uuid4()
    path = [_msg(conv, "word " * 120, Role.user if i % 2 == 0 else Role.assistant) for i in range(20)]
    for item in path:
        item.id = uuid4()

    store = {}
    calls = {"n": 0}

    async def counting_summarize(items, budget):
        calls["n"] += 1
        return f"SUMMARY #{calls['n']}"

    def cache_get(span, budget, family):
        return store.get((span, budget, family))

    def cache_put(budget, family, text, span):
        store[(span, budget, family)] = text

    first = await render_context(path, _window(2000), summarize=counting_summarize,
                                 cache_get=cache_get, cache_put=cache_put)
    second = await render_context(path, _window(2000), summarize=counting_summarize,
                                  cache_get=cache_get, cache_put=cache_put)
    # One paid summarizer call, ever — the second render hit the cache:
    assert calls["n"] == 1
    assert first.summary_text == second.summary_text == "SUMMARY #1"
    assert [i.id for i in first.items] == [i.id for i in second.items]


# A SINGLE item larger than the whole window must still raise the typed
# ContextOverflowError when summarization is disabled — the `len(path) <= 1`
# short-circuit used to return the full path BEFORE the allow_summarization
# check, silently sending an overflowing request to the provider (found by
# this test as a real bypass, then fixed by reordering the renderer's guards):
@pytest.mark.asyncio
async def test_single_item_overflow_with_summarization_disabled_should_raise():
    from platform.services.generative_ai_service.engine.types import ContextOverflowError

    conv = uuid4()
    giant = _msg(conv, "x", Role.user, estimate=500_000)
    giant.id = uuid4()
    with pytest.raises(ContextOverflowError):
        await render_context([giant], _window(1000), summarize=None,
                             cache_get=lambda s, b, f: None, cache_put=lambda b, f, s, e: None,
                             allow_summarization=False)


# ============================================================================
# ENGINE — FALLBACK CHAINS (D28) AND FAIL-CLOSED PATHS
# ============================================================================

def test_construction_fallback_builds_openrouter_equivalent(monkeypatch):
    from infrastructure.settings import settings
    from pydantic_ai.models.openrouter import OpenRouterModel

    # The direct key is missing but OpenRouter's exists — the D28 build-time swap:
    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", None, raising=False)
    monkeypatch.setattr(settings, "OPENROUTER_API_KEY", "sk-or-test", raising=False)

    built = providers.build_model("anthropic:claude-sonnet-4-6")
    assert built.served_by == "openrouter"
    assert built.switched_reason and "anthropic" in built.switched_reason
    # The primary IS OpenRouter, so no further wrapping happens (nothing to fall to):
    assert isinstance(built.model, OpenRouterModel)


@pytest.mark.asyncio
async def test_model_switched_event_reports_the_served_id(db, monkeypatch):
    # A construction-time fallback must surface as ModelSwitched, never silently:
    monkeypatch.setattr(
        engine_mod, "build_model",
        lambda model_id, fallbacks=None: BuiltModel(
            model=TestModel(custom_output_text="ok"), requested_id=model_id,
            served_by="openrouter", switched_reason="anthropic key missing at construction",
        ),
    )
    conv = create_conversation(db, owner_id="u1")
    spec = AgentSpec(name="a", model="anthropic:claude-sonnet-4-6", features=_NO_TOOLS)
    deps = RunDeps(db=db, owner_id="u1", conversation_id=conv.id)

    events = [e async for e in stream_agent(spec, deps, UserInput(text="hi"))]
    by_type = {e.type: e for e in events}
    switched = by_type["model_switched"]
    # The event names both sides honestly, and RunStarted already carries the
    # post-fallback id (the model that will ACTUALLY serve):
    assert switched.from_model == "anthropic:claude-sonnet-4-6"
    assert switched.to_model == "openrouter:claude-sonnet-4-6"
    assert by_type["run_started"].model == "openrouter:claude-sonnet-4-6"
    assert by_type["run_completed"].output == "ok"


@pytest.mark.asyncio
async def test_runtime_fallback_rolls_over_to_next_model(db, monkeypatch):
    async def broken_stream(messages, info):
        raise RuntimeError("primary provider down")
        yield ""  # pragma: no cover — making this an async generator:

    chain = FallbackModel(
        FunctionModel(stream_function=broken_stream),
        TestModel(custom_output_text="served by fallback"),
        fallback_on=(RuntimeError,),
    )
    monkeypatch.setattr(
        engine_mod, "build_model",
        lambda model_id, fallbacks=None: BuiltModel(model=chain, requested_id=model_id, served_by="test"),
    )
    conv = create_conversation(db, owner_id="u1")
    deps = RunDeps(db=db, owner_id="u1", conversation_id=conv.id)

    result = await run_agent(AgentSpec(name="a", model="test:test", features=_NO_TOOLS), deps, UserInput(text="hi"))
    # The mid-call rollover produced a normal completed run:
    assert result.output == "served by fallback"
    hist = full_history(db, conv.id, owner_id="u1")
    assert [i.role.value for i in hist] == ["user", "assistant"]


@pytest.mark.asyncio
async def test_exhausted_fallback_chain_fails_without_partial_output(db, monkeypatch):
    async def broken_stream(messages, info):
        raise RuntimeError("provider down")
        yield ""  # pragma: no cover — making this an async generator:

    chain = FallbackModel(
        FunctionModel(stream_function=broken_stream),
        FunctionModel(stream_function=broken_stream),
        fallback_on=(RuntimeError,),
    )
    monkeypatch.setattr(
        engine_mod, "build_model",
        lambda model_id, fallbacks=None: BuiltModel(model=chain, requested_id=model_id, served_by="test"),
    )
    conv = create_conversation(db, owner_id="u1")
    deps = RunDeps(db=db, owner_id="u1", conversation_id=conv.id)

    events = [e async for e in stream_agent(
        AgentSpec(name="a", model="test:test", features=_NO_TOOLS), deps, UserInput(text="hi"),
    )]
    # Every model in the chain failed -> a terminal run_failed, never run_completed:
    assert events[-1].type == "run_failed"
    assert all(e.type != "run_completed" for e in events)
    # No partial assistant output was persisted — only the user turn:
    hist = full_history(db, conv.id, owner_id="u1")
    assert [i.role.value for i in hist] == ["user"]


@pytest.mark.asyncio
async def test_no_keys_anywhere_fails_run_loudly(db, monkeypatch):
    from infrastructure.settings import settings

    # No direct key AND no OpenRouter key -> nothing to build, nothing to fall to:
    for key in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GOOGLE_API_KEY", "XAI_API_KEY", "OPENROUTER_API_KEY"):
        monkeypatch.setattr(settings, key, None, raising=False)

    conv = create_conversation(db, owner_id="u1")
    deps = RunDeps(db=db, owner_id="u1", conversation_id=conv.id)
    events = [e async for e in stream_agent(
        AgentSpec(name="a", model="anthropic:claude-sonnet-4-6", features=_NO_TOOLS), deps, UserInput(text="hi"),
    )]
    assert events[-1].type == "run_failed"
    assert "No API key" in events[-1].error


@pytest.mark.asyncio
async def test_budget_check_failure_fails_closed_before_any_side_effect(db, monkeypatch):
    from infrastructure.settings import settings

    monkeypatch.setattr(settings, "AI_BUDGET_ENFORCEMENT", True)

    def broken_check(*args, **kwargs):
        raise RuntimeError("budget database unreachable")

    # The check itself failing with enforcement ON must block (fail-closed —
    # if we cannot verify budget, we do not spend), typed for the router:
    monkeypatch.setattr(engine_mod, "check_budget", broken_check)

    conv = create_conversation(db, owner_id="u1")
    deps = RunDeps(db=db, owner_id="u1", conversation_id=conv.id)
    events = [e async for e in stream_agent(
        AgentSpec(name="a", model="test:test", features=_NO_TOOLS), deps, UserInput(text="hi"),
    )]
    assert events[-1].type == "run_failed"
    assert "BudgetCheckUnavailableError" in events[-1].error
    # Blocked BEFORE any side effect — not even the user turn persisted:
    assert full_history(db, conv.id, owner_id="u1") == []


@pytest.mark.asyncio
async def test_context_overflow_surfaces_as_typed_run_failure(db, monkeypatch):
    monkeypatch.setattr(
        engine_mod, "build_model",
        lambda model_id, fallbacks=None: BuiltModel(model=TestModel(), requested_id=model_id, served_by="test"),
    )
    conv = create_conversation(db, owner_id="u1")
    # Two turns whose pinned estimates overflow the default 128k window's
    # trigger (96k) for the unknown "test:test" model (tokenizer family "test"):
    append_item(db, _msg(conv.id, "old question", Role.user, estimate=80_000), owner_id="u1")
    append_item(db, _msg(conv.id, "old answer", Role.assistant, estimate=80_000), owner_id="u1")

    spec = AgentSpec(name="a", model="test:test", features=AgentFeatures(rag_search=False, summarization=False))
    deps = RunDeps(db=db, owner_id="u1", conversation_id=conv.id)
    events = [e async for e in stream_agent(spec, deps, UserInput(text="hi"))]
    # summarization=False + overflow -> the typed error, never silent truncation:
    assert events[-1].type == "run_failed"
    assert "ContextOverflowError" in events[-1].error


@pytest.mark.asyncio
async def test_empty_user_text_still_completes(db, monkeypatch):
    monkeypatch.setattr(
        engine_mod, "build_model",
        lambda model_id, fallbacks=None: BuiltModel(
            model=TestModel(custom_output_text="ok"), requested_id=model_id, served_by="test",
        ),
    )
    conv = create_conversation(db, owner_id="u1")
    deps = RunDeps(db=db, owner_id="u1", conversation_id=conv.id)

    events = [e async for e in stream_agent(
        AgentSpec(name="a", model="test:test", features=_NO_TOOLS), deps, UserInput(text=""),
    )]
    assert events[-1].type == "run_completed"
    # The empty turn persisted as a partless user item (no phantom TextPart):
    user_turn = full_history(db, conv.id, owner_id="u1")[0]
    assert user_turn.role == Role.user and user_turn.parts == []
    assert user_turn.extract_text() is None


# ============================================================================
# BILLING — PRICING EXTREMES, SCOPES, FAIL-LOUD INPUTS, LEDGER INTEGRITY
# ============================================================================

def test_pricing_extremes_and_microusd_rounding():
    priced = ModelInfo(id="t:m", provider="t", cost=Cost(input=3.0, output=15.0, cache_read=0.3))

    # Zero-token usage prices to exactly zero and still counts as priced:
    zero = price_usage(Usage(), priced)
    assert zero.cost_microusd == 0 and zero.priced is True

    # Enormous usage stays exact — integer micro-USD, no float drift:
    huge = price_usage(Usage(input_tokens=10**12), priced)
    assert huge.cost_microusd == 3 * 10**12

    # Sub-micro-USD dimensions round to zero PER DIMENSION (1 cached token at
    # $0.30/Mtok is 0.3 micro-USD -> 0), while whole-micro dimensions survive:
    tiny = price_usage(Usage(input_tokens=1, cache_read_tokens=1), priced)
    assert tiny.breakdown == {"input": 3, "output": 0, "cache_read": 0}

    # Half-micro values follow Python's round-half-to-even (banker's rounding):
    half = ModelInfo(id="t:h", provider="t", cost=Cost(input=0.5, output=1.5))
    result = price_usage(Usage(input_tokens=1, output_tokens=1), half)
    assert result.breakdown == {"input": 0, "output": 2} and result.cost_microusd == 2


def test_global_budget_blocks_unrelated_owner(db):
    # One global cap; one owner exhausts it; EVERY other owner is blocked too:
    db.add(AIBudget(scope_type="global", scope_id="*", window="daily", limit_microusd=1_000_000))
    db.commit()
    _spend(db, "big-spender", 1_000_000, NOW)

    decision = check_budget(db, owner_id="innocent-bystander", now=NOW)
    assert not decision.allowed
    assert decision.binding_budget == "global/daily"
    assert decision.remaining_microusd == 0


def test_conversation_and_agent_scopes_bind_only_when_named(db):
    conv_id = uuid4()
    db.add(AIBudget(scope_type="conversation", scope_id=str(conv_id), window="total", limit_microusd=100))
    db.add(AIBudget(scope_type="agent", scope_id="researcher", window="total", limit_microusd=200))
    db.commit()
    _spend(db, "u1", 100, NOW, conversation_id=conv_id)
    _spend(db, "u1", 50, NOW, agent_name="researcher")

    # A bare owner check sees neither scoped budget (nothing applies -> unlimited):
    bare = check_budget(db, owner_id="u1", now=NOW)
    assert bare.allowed and bare.remaining_microusd is None

    # Naming the exhausted conversation binds it and blocks:
    scoped = check_budget(db, owner_id="u1", conversation_id=conv_id, now=NOW)
    assert not scoped.allowed and scoped.binding_budget == "conversation/total"

    # Naming the agent binds its own remaining allowance:
    agent = check_budget(db, owner_id="u1", agent_name="researcher", now=NOW)
    assert agent.allowed and agent.remaining_microusd == 150
    assert agent.binding_budget == "agent/total"


def test_unknown_window_and_scope_fail_loudly(db):
    # A typo'd window or scope must raise, never silently sum to zero (money):
    with pytest.raises(ValueError, match="window"):
        window_start("hourly", NOW)
    with pytest.raises(ValueError, match="scope"):
        window_spend(db, scope_type="team", scope_id="x", window="daily", now=NOW)


def test_ledger_row_integrity_and_unpriced_rows_count(db):
    conv_id, run_id = uuid4(), uuid4()
    usage = Usage(input_tokens=7, output_tokens=3, cache_read_tokens=2)
    event = record_spend(
        db, owner_id="u1", model_id="t:mystery", usage=usage,
        price=PriceResult(cost_microusd=555, priced=False, breakdown={"input": 555}),
        kind="run", conversation_id=conv_id, run_id=run_id, agent_name="a", created_at=NOW,
    )
    # Every audit field survives the append exactly, including the full usage JSON:
    assert event.priced is False and event.cost_microusd == 555
    assert event.conversation_id == conv_id and event.run_id == run_id and event.agent_name == "a"
    assert Usage.model_validate(event.usage) == usage

    # Pessimistically-priced (priced=False) rows still count toward enforcement
    # sums — unknown models deplete budgets, never escape them:
    assert window_spend(db, scope_type="owner", scope_id="u1", window="daily", now=NOW) == 555


# ============================================================================
# REGISTRY — OVERRIDE MERGE SEMANTICS AND DEFAULT RESOLUTION
# ============================================================================

def test_override_merge_is_per_field_one_level_deep():
    base = {"context_window": 100, "cost": {"input": 1.0, "output": 2.0}, "capabilities": {"reasoning": True}}
    merged = _merge_entry(base, {"cost": {"input": 9.0}, "lifecycle": "beta"})

    # A nested override touches only the field it carries; siblings survive:
    assert merged["cost"] == {"input": 9.0, "output": 2.0}
    assert merged["context_window"] == 100 and merged["lifecycle"] == "beta"
    assert merged["capabilities"] == {"reasoning": True}
    # The base entry itself was not mutated by the merge:
    assert base["cost"] == {"input": 1.0, "output": 2.0}

    # Unlike the sync-layer feed merge, an explicit non-dict override value
    # replaces wholesale — a hand-pin means exactly what it says:
    assert _merge_entry(base, {"cost": None})["cost"] is None


def test_unqualified_model_id_defaults_conservatively():
    # An id with no "provider:" prefix at all still resolves (day-one policy):
    info = get_model_info("totally-unknown-model")
    assert info.id == "totally-unknown-model"
    assert info.provider == "openai" and info.source == "default"
    assert info.context_window == 128_000
    assert not info.cost.is_priced()

    # ...and its runs are billed at the deliberately-expensive pessimistic rates:
    result = price_usage(Usage(input_tokens=1_000_000), info, pessimistic=PessimisticRates())
    assert result.priced is False and result.cost_microusd == 15_000_000


def test_floor_survives_missing_data_and_invalid_override_entries(tmp_path, monkeypatch):
    from platform.services.generative_ai_service.registry import loader

    overrides = tmp_path / "overrides.json"
    overrides.write_text(json.dumps({
        "_comment": "doc keys must be skipped",
        "bad:entry": {"context_window": "not-a-number"},
        "good:entry": {"context_window": 42_000, "cost": {"input": 1.0, "output": 2.0}},
    }))
    # Pointing the loader at a MISSING data file plus the overrides above:
    monkeypatch.setattr(loader, "_DATA_PATH", tmp_path / "missing.json")
    monkeypatch.setattr(loader, "_OVERRIDES_PATH", overrides)

    index = _build_index()
    # The baked-in floor keeps every core model alive despite the dead data file:
    assert set(_FLOOR) <= set(index)
    # One invalid entry is skipped without sinking the rest; doc keys never load:
    assert "bad:entry" not in index and "_comment" not in index
    good = index["good:entry"]
    assert good.source == "overrides" and good.context_window == 42_000 and good.cost.input == 1.0
