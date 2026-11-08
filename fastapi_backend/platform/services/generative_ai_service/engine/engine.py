"""
Engine — the single run path (Phase 2, D10).

Orchestration per run:
1. append the user's message to the transcript (source of truth first);
2. render the active path into a per-model context (renderer: full history or
   summary + tail, conditional on the target model's window — requirement 6);
3. build the model (providers, D28 fallback) and stream the run through the
   adapter (which owns the whole pydantic_ai lifecycle);
4. translate each event into OUR event schema as it arrives (D10);
5. append the run's output items (assistant text, thinking, tool calls/results)
   with usage + raw populated;
6. emit RunCompleted.

The engine imports ONLY our types plus the sanctioned adapter/providers
helpers — never `pydantic_ai` (D3). Sync DB work hops to a thread so the event
loop keeps serving other streams (D29).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from uuid import UUID, uuid4

from ..billing import PessimisticRates, check_budget, derive_run_caps, price_usage, record_spend
from ..extensions import (
    build_mcp_tools,
    build_skill_preamble,
    resolve_mcp_configs,
    resolve_skills,
    resolve_user_mcp_configs,
    resolve_user_skill_configs,
)
from ..transcript import (
    append_item,
    append_items,
    embed_pending_items,
    get_conversation,
    get_current_path,
    get_summary,
    put_summary,
    set_items_meta,
)
from ..transcript.types import Item, ItemKind, MediaPart, Role, TextPart, Usage
from . import adapter as _adapter
from .model_info import DEFAULT_SUMMARIZER_MODEL, get_model_info
from .providers import build_model
from .renderer import RenderedContext, render_context
from .tool_table import ToolContext, requested_tool_names, resolve_tools, split_native_tools
from infrastructure.settings import settings

from .types import (
    DEFAULT_MEMORY_SCOPE,
    AgentSpec,
    ApprovalDecision,
    ApprovalResolutionError,
    BudgetCheckUnavailableError,
    BudgetExceededError,
    MediaGenerated,
    ModelSwitched,
    PendingApproval,
    RunCompleted,
    RunDeps,
    RunEvent,
    RunFailed,
    RunPaused,
    RunResult,
    RunStarted,
    UserInput,
)


logger = logging.getLogger(__name__)


def _user_item(conversation_id: UUID, user_input: UserInput) -> Item:
    """Building the transcript item for the incoming user turn (text + any media):"""
    parts: list = []
    if user_input.text:
        parts.append(TextPart(text=user_input.text))
    for media in user_input.media:
        parts.append(MediaPart(**media))
    return Item(
        conversation_id=conversation_id, kind=ItemKind.message, role=Role.user,
        parts=parts, meta=dict(user_input.meta),
    )


def _served_id(spec_model: str, served_by: str) -> str:
    """Composing the canonical id of the model that actually served (post-fallback):"""
    name = spec_model.split(":", 1)[1] if ":" in spec_model else spec_model
    return f"{served_by}:{name}"


def _join_preambles(*parts: str | None) -> str | None:
    """Joining the non-empty system preambles (skills L1, memory usage) into one block:"""
    present = [p for p in parts if p]
    return "\n\n".join(present) if present else None


def _memory_usage_recorder(deps: RunDeps, conversation_id: UUID):
    """
    Building the async on_usage callback for the memory resolver (P10): it
    attributes the cheap resolver model's token spend to the conversation owner
    as `kind="memory"`, exactly like the summarizer's O6.8 attribution. Ledger
    failures never fail the write (P4 posture):
    """
    async def _record(usage: Usage) -> None:
        """Recording one resolver call's spend against the owner (best-effort)."""
        try:
            price = price_usage(usage, get_model_info(DEFAULT_SUMMARIZER_MODEL), pessimistic=PessimisticRates())
            if price.cost_microusd is not None:
                await asyncio.to_thread(
                    record_spend, deps.db,
                    owner_id=deps.owner_id, model_id=DEFAULT_SUMMARIZER_MODEL,
                    usage=usage, price=price, kind="memory",
                    conversation_id=conversation_id,
                )
        except Exception:  # noqa: BLE001 — memory writes must not fail over ledger writes:
            logger.exception("memory resolver spend recording failed")

    return _record


async def _preflight_budget(spec: AgentSpec, deps: RunDeps):
    """
    The Phase-4/D7 budget gate shared by fresh runs and resumes: BEFORE any
    side effect, and fail-CLOSED when enforcement is on (an unverifiable budget
    blocks rather than spends). Returns the decision (or None when enforcement
    is off) for the per-run output-token cap:
    """
    if not settings.AI_BUDGET_ENFORCEMENT:
        return None
    try:
        decision = await asyncio.to_thread(
            check_budget, deps.db,
            owner_id=deps.owner_id, conversation_id=deps.conversation_id, agent_name=spec.name,
        )
    except Exception as exc:  # noqa: BLE001 — fail-closed is the deliberate policy:
        logger.exception("budget check failed; blocking run (fail-closed)")
        raise BudgetCheckUnavailableError(str(exc)) from exc
    if not decision.allowed:
        raise BudgetExceededError(decision.binding_budget)
    return decision


def _pending_approvals_on_path(path: list[Item]) -> list[Item]:
    """Collecting the path's tool_call items still awaiting a decision (P8):"""
    return [
        it for it in path
        if it.kind == ItemKind.tool_call and it.meta.get("approval") == "pending"
    ]


async def stream_agent(
    spec: AgentSpec,
    deps: RunDeps,
    user_input: UserInput,
) -> AsyncIterator[RunEvent]:
    """
    THE run path (a fresh user turn). Yields our RunEvents; non-streaming
    callers use `run_agent`. Cancellation (client disconnect) propagates into
    the run and stops token spend (D29). If the model calls an approval-gated
    tool (P8, D46), the run ends with RunPaused instead of RunCompleted and the
    caller resumes via `resume_agent`.
    """
    run_id = uuid4()
    conversation_id = deps.conversation_id
    try:
        decision = await _preflight_budget(spec, deps)

        # Abandoning any approval-gated calls the previous turn left pending: the
        # user chose to send a new message instead of deciding, so those calls
        # are retracted (D47) — marked "abandoned" so history renders them with
        # a synthetic "not executed" return and they can never be resumed later:
        path0 = await asyncio.to_thread(get_current_path, deps.db, conversation_id, owner_id=deps.owner_id)
        stale = {it.id: {"approval": "abandoned"} for it in _pending_approvals_on_path(path0)}
        if stale:
            await asyncio.to_thread(set_items_meta, deps.db, stale, owner_id=deps.owner_id)

        # Persisting the user turn first (source of truth before any model call):
        await asyncio.to_thread(
            append_item, deps.db, _user_item(conversation_id, user_input), owner_id=deps.owner_id,
        )

        async for event in _execute(
            spec, deps, run_id, decision,
            prompt=user_input.text or "", is_resume=False, resume_decisions=None, pending_ids=[],
        ):
            yield event
    except Exception as exc:  # noqa: BLE001 — surfacing failures as a typed terminal event:
        logger.exception("run %s failed", run_id)
        yield RunFailed(run_id=run_id, error=f"{type(exc).__name__}: {exc}")


def find_pending_approvals(deps: RunDeps) -> list[PendingApproval]:
    """
    The synchronous pre-check the approvals endpoint runs (P8): returning the
    calls awaiting a decision on the caller's current path, after verifying the
    conversation is theirs. Raising ApprovalResolutionError otherwise — the
    router maps it to a clean 409 before opening any stream:
    """
    # Owner-scoped by construction — an unknown id and another owner's id raise
    # the SAME ConversationNotFoundError, which the router maps to one 404:
    get_conversation(deps.db, deps.conversation_id, owner_id=deps.owner_id)
    path = get_current_path(deps.db, deps.conversation_id, owner_id=deps.owner_id)
    pending = _pending_approvals_on_path(path)
    if not pending:
        raise ApprovalResolutionError("There are no tool calls waiting for your approval on this conversation.")
    return [
        PendingApproval(
            tool_call_id=it.tool_call_id or "",
            tool_name=it.tool_name or "",
            args=next((p.data for p in it.parts if getattr(p, "type", None) == "data"), None),
        )
        for it in pending
    ]


async def resume_agent(
    spec: AgentSpec,
    deps: RunDeps,
    decisions: list[ApprovalDecision],
) -> AsyncIterator[RunEvent]:
    """
    Resuming a paused run with the caller's decisions (P8, D47/D48). The
    decisions must cover EXACTLY the calls pending on the current path — a
    subset, an unknown id, or a stale (branched-away) set is a typed
    ApprovalResolutionError. Approved calls execute now (with any edited args),
    denials become the tool result the model reads; the run then continues as
    an ordinary engine run over the checkpointed transcript, and may itself
    complete, fail, or pause again.
    """
    run_id = uuid4()
    conversation_id = deps.conversation_id
    try:
        decision = await _preflight_budget(spec, deps)

        # Re-finding and validating the pending set (defense — the router
        # pre-checked, but the state may have moved between the two):
        path = await asyncio.to_thread(
            get_current_path, deps.db, conversation_id, owner_id=deps.owner_id,
        )
        pending = _pending_approvals_on_path(path)
        if not pending:
            raise ApprovalResolutionError("There are no tool calls waiting for your approval on this conversation.")
        pending_by_id = {it.tool_call_id: it for it in pending}
        decided_ids = {d.tool_call_id for d in decisions}
        if decided_ids != set(pending_by_id):
            raise ApprovalResolutionError(
                "Your approval decisions don't match the tool calls waiting on this conversation. Refresh and try again."
            )
        pending_ids = [it.id for it in pending]

        async for event in _execute(
            spec, deps, run_id, decision,
            prompt=None, is_resume=True, resume_decisions=decisions, pending_ids=pending_ids,
        ):
            yield event
    except Exception as exc:  # noqa: BLE001 — surfacing failures as a typed terminal event:
        logger.exception("resume %s failed", run_id)
        yield RunFailed(run_id=run_id, error=f"{type(exc).__name__}: {exc}")


async def _execute(
    spec: AgentSpec,
    deps: RunDeps,
    run_id: UUID,
    decision,
    *,
    prompt: str | None,
    is_resume: bool,
    resume_decisions: list[ApprovalDecision] | None,
    pending_ids: list[UUID],
) -> AsyncIterator[RunEvent]:
    """
    The shared run core for both a fresh turn and a resume (P8). Builds the
    model, resolves tools (passing the approval scope), renders context,
    streams through the adapter, persists atomically, prices, and emits the
    terminal event — RunCompleted, or RunPaused when the model called an
    approval-gated tool. `pending_ids` (resume only) are the transcript items
    whose "pending" marker is flipped to "resolved" in the SAME commit as their
    now-arriving results, so a crash can never leave a duplicate or a bare call.
    """
    conversation_id = deps.conversation_id

    # Building the model (D28 fallback happens here) and announcing the run:
    built = build_model(spec.model, spec.fallback_models)
    served_id = _served_id(spec.model, built.served_by)
    yield RunStarted(run_id=run_id, agent=spec.name, model=served_id)
    if built.switched_reason:
        logger.warning("model fallback at construction: %s", built.switched_reason)
        yield ModelSwitched(
            run_id=run_id, from_model=spec.model, to_model=served_id, reason=built.switched_reason,
        )

    info = get_model_info(spec.model)
    # The SERVED model's registry entry drives D34 native-vs-fill and D20
    # output-mode selection — after a fallback it can differ from the spec:
    served_info = get_model_info(served_id)

    # Converting remaining budget into a per-run output-token ceiling (D23);
    # the adapter min-combines it with the agent's own limits:
    run_caps = derive_run_caps(decision.remaining_microusd, info) if decision else None

    # Resolving tools (P5, D33/D34): the spec's names (plus implicit ones from
    # feature toggles) split into provider-native capabilities and our
    # registered implementations; unknown names raise loudly, before any spend:
    tools = None
    tool_ctx = None
    resolved = []
    requested = requested_tool_names(spec)
    _, fill_names = split_native_tools(requested, served_info.capabilities)
    # D41/D45 image-generation fill (unchanged from P7):
    if spec.features.image_generation and (
        spec.media.image_model is not None
        or not served_info.capabilities.image_generation
    ):
        fill_names.append("generate_image")

    # P9 (D53): attached skills add the open_skill disclosure tool and their L1
    # metadata preamble; their bundles ride into every code_execution session.
    # User-added skills merge in when the agent's toggle is on (D51):
    skill_configs = resolve_skills(spec.skills)
    if spec.allow_user_skills:
        skill_configs = skill_configs + await asyncio.to_thread(
            resolve_user_skill_configs, deps.db, deps.owner_id, spec.name,
        )
    skill_refs = [c.to_ref() for c in skill_configs]
    skill_preamble = build_skill_preamble(skill_configs) if skill_configs else None
    if skill_configs and "open_skill" not in fill_names:
        fill_names.append("open_skill")

    # P10 (features.memory): expose the durable-memory tools and inject their
    # always-on usage preamble. Memory is agent-scoped (durable across
    # conversations) and reconciles bi-temporally on write:
    memory_preamble = None
    if spec.features.memory:
        from ..memory import MEMORY_PREAMBLE, MEMORY_TOOL_NAMES
        for name in MEMORY_TOOL_NAMES:
            if name not in fill_names:
                fill_names.append(name)
        memory_preamble = MEMORY_PREAMBLE

    if requested or fill_names:
        resolved = resolve_tools(fill_names)

    # P9 (D52): bridge attached MCP servers' tools into the tool list. Dev-added
    # servers resolve from the catalog by id (raw URLs stay back-compatible);
    # user-added servers merge in Slice 3. Each becomes an ordinary fill, so it
    # inherits P8 approval, billing, and the native-or-fill path with no
    # special-casing below. Listing happens now (before the model call) so a
    # broken server fails loudly, before any spend:
    mcp_configs = resolve_mcp_configs(spec.mcp_servers)
    if spec.allow_user_mcp:
        mcp_configs = mcp_configs + await asyncio.to_thread(
            resolve_user_mcp_configs, deps.db, deps.owner_id, spec.name,
        )
    if mcp_configs:
        resolved = resolved + await build_mcp_tools(mcp_configs)

    if resolved:
        embedder = _adapter.make_embedder() if "search_conversation" in fill_names else None
        # P10 injected callables (built through the adapter so memory/ stays
        # SDK-free, D3): a shared embedder for semantic recall + a cheap-model
        # resolver for the supersede-vs-accrue decision, cost-attributed like
        # the summarizer (O6.8/P4). Both None-safe — memory degrades, never fails:
        memory_embed_fn = None
        memory_resolve_fn = None
        if spec.features.memory:
            mem_embedder = embedder or _adapter.make_embedder()
            memory_embed_fn = mem_embedder.embed if mem_embedder is not None else None
            resolver_built = build_model(DEFAULT_SUMMARIZER_MODEL)
            memory_resolve_fn = _adapter.make_memory_resolver(
                resolver_built, on_usage=_memory_usage_recorder(deps, conversation_id),
            )
        tool_ctx = ToolContext(
            db=deps.db, owner_id=deps.owner_id, conversation_id=conversation_id,
            features=spec.features, embedder=embedder, extra=deps.extra,
            media=spec.media, skills=skill_refs,
            memory_scope=deps.memory_scope or DEFAULT_MEMORY_SCOPE,
            memory_embed_fn=memory_embed_fn, memory_resolve_fn=memory_resolve_fn,
        )
        # HITL scope (P8, D46): only OUR fill/registered tools can ever be
        # gated — a spec name that resolved native never reaches `resolved`, so
        # naming it here is inert (logged, never a forced lane):
        approval_scope = frozenset(spec.requires_approval)
        inert = approval_scope - {e.name for e in resolved}
        if inert:
            logger.debug("requires_approval names not our tools (inert, D46): %s", sorted(inert))
        tools = _adapter.bind_tools(resolved, tool_ctx, requires_approval=approval_scope)

    # The run can PAUSE when any bound tool is approval-gated (whole-tool via
    # the spec, or per-invocation via a registration predicate), or always on a
    # resume (a resumed tool may pause again). Agents with no approval surface
    # keep the exact pre-P8 output contract (allow_pause=False):
    approval_scope = frozenset(spec.requires_approval)
    allow_pause = is_resume or any(
        e.name in approval_scope or e.approval_predicate is not None for e in resolved
    )

    # Rendering context: load the active path, summarize only if the model
    # needs it (requirement 6). A fresh turn's last item is the user turn (the
    # live prompt); a resume has no new prompt — the pending calls are the tail
    # and the whole path is history (rendered "bare" so the deferred results
    # pair with them):
    path = await asyncio.to_thread(
        get_current_path, deps.db, conversation_id, owner_id=deps.owner_id,
    )
    rendered, flush_items = await _render(spec, deps, path, info)
    if is_resume:
        history_items = rendered.items
    else:
        history_items = rendered.items[:-1] if rendered.items else []

    # P10 provenance: tag memory writes this turn with the user item that
    # triggered them (source_turn_ids). A resume has no fresh user turn:
    if tool_ctx is not None and not is_resume:
        last_user = next((it for it in reversed(path) if it.role == Role.user), None)
        if last_user is not None:
            tool_ctx.current_turn_id = last_user.id

    # Releasing the DB connection before the (potentially minutes-long) model
    # stream so it is not pinned idle-in-transaction (the P2 concurrency rule);
    # rendered items are detached pydantic copies:
    await asyncio.to_thread(deps.db.rollback)

    # Streaming the run through the adapter (all pydantic_ai lives there):
    outcome = None
    async for produced in _adapter.stream_run(
        spec=spec, built=built, prompt=prompt,
        history_items=history_items, summary_text=rendered.summary_text,
        run_id=run_id, conversation_id=conversation_id, model_served=served_id,
        budget_output_cap=run_caps.max_output_tokens if run_caps else None,
        info=served_info, tools=tools,
        allow_pause=allow_pause, resume_decisions=resume_decisions,
        extra_system=_join_preambles(skill_preamble, memory_preamble),
    ):
        if isinstance(produced, _adapter.RunOutcome):
            outcome = produced
        else:
            yield produced

    run_items = list(outcome.items) if outcome is not None else []
    media_items = list(tool_ctx.media_items) if tool_ctx is not None else []
    paused_approvals = list(outcome.approvals) if outcome is not None else []

    # Marking each newly-persisted gated tool_call as "pending" (P8): this is
    # the checkpoint — the call is committed WITHOUT a result, and the marker
    # tells later renders to synthesize a return (abandonment) or resume to
    # supply one. Provider pair-atomicity is preserved: resolved pairs are
    # complete, pending calls are explicitly flagged, never bare-and-unknown:
    pending_call_ids = {a.tool_call_id for a in paused_approvals}
    for item in run_items:
        if item.kind == ItemKind.tool_call and item.tool_call_id in pending_call_ids:
            item.meta = {**item.meta, "approval": "pending"}

    # Persisting output items ATOMICALLY (one commit). On a resume, the same
    # commit flips the OLD pending markers to "resolved" as their results land:
    item_ids: list[UUID] = []
    meta_updates = {pid: {"approval": "resolved"} for pid in pending_ids} if is_resume else None
    if run_items or media_items or meta_updates:
        stored = await asyncio.to_thread(
            append_items, deps.db, run_items + media_items, meta_updates, owner_id=deps.owner_id,
        )
        item_ids = [s.id for s in stored]

    # Surfacing fill-generated media (D41) + its per-unit spend row (D44):
    for item in media_items:
        part = next((p for p in item.parts if getattr(p, "type", None) == "media"), None)
        if part is None:
            continue
        yield MediaGenerated(
            run_id=run_id, media=part.media, mime_type=part.mime_type, data_b64=part.data_b64,
        )
        await _record_media_spend(deps, spec, run_id, conversation_id, item)

    # Post-run stages that only make sense for a COMPLETED turn — a paused run
    # has no final text to speak and its turn is not yet done (P8):
    if not paused_approvals:
        if (
            spec.features.speech_output
            and outcome is not None and outcome.output
            and not _has_audio_part(run_items + media_items)
        ):
            speech_event = await _speak_output(
                deps, spec, run_id, conversation_id, str(outcome.output), item_ids,
            )
            if speech_event is not None:
                yield speech_event

    # Pricing the tokens spent (Phase 4) — a paused run still consumed input +
    # any pre-pause output, so it bills exactly like a completed one:
    run_usage = outcome.usage if outcome is not None else Usage()
    cost_usd, priced = await _price_run(deps, spec, run_id, conversation_id, served_id, served_info, run_usage, outcome)

    # Scheduling the D35 embedding backfill LAST (completed turns only): the
    # detached task opens its own fresh session, so starting it only after the
    # run's own writes have committed keeps it from racing the request session
    # for a shared connection (a test-harness concern; production pools give
    # each its own connection):
    if not paused_approvals and spec.features.rag_search and deps.session_factory is not None:
        _schedule_embedding_backfill(deps.session_factory, conversation_id, deps.owner_id)

    # Scheduling the D60 memory flush (completed turns only): when this render
    # freshly compacted a span out of context AND memory is on, extract its
    # durable facts before they are lost — the write-completeness safety net for
    # facts the agent did not save in real time. Detached + fresh session, like
    # the backfill; anything already remembered dedups in reconcile_write:
    if (not paused_approvals and spec.features.memory and flush_items
            and deps.session_factory is not None):
        _schedule_memory_flush(
            deps.session_factory, deps.owner_id,
            deps.memory_scope or DEFAULT_MEMORY_SCOPE, conversation_id, flush_items,
        )

    # The terminal event: paused for approval, or completed (P8):
    if paused_approvals:
        yield RunPaused(
            run_id=run_id, approvals=paused_approvals, item_ids=item_ids,
            usage=run_usage, cost_usd=cost_usd, priced=priced,
        )
    else:
        yield RunCompleted(
            run_id=run_id,
            output=outcome.output if outcome is not None else None,
            usage=run_usage, item_ids=item_ids, cost_usd=cost_usd, priced=priced,
        )


async def _price_run(deps, spec, run_id, conversation_id, served_id, served_info, run_usage, outcome):
    """
    Pricing a run's usage and recording the spend (Phase 4): registry rates,
    pessimistic fallback for unpriced models, plus the native-web-search
    per-call surcharge (P5). A ledger-write failure is Sentry-worthy but never
    fails the user's run. Returns (cost_usd, priced):
    """
    cost_usd: float | None = None
    priced = True
    try:
        price = price_usage(run_usage, get_model_info(served_id), pessimistic=PessimisticRates())
        per_call = served_info.cost.web_search_per_call
        native_searches = sum(
            1 for item in (outcome.items if outcome else [])
            if item.kind == ItemKind.tool_call
            and item.meta.get("implementation") == "native"
            and item.tool_name and "web_search" in item.tool_name
        )
        if native_searches and per_call and price.cost_microusd is not None:
            surcharge = round(native_searches * per_call * 1_000_000)
            price.cost_microusd += surcharge
            price.breakdown["web_search_calls"] = surcharge
        if price.cost_microusd is not None:
            await asyncio.to_thread(
                record_spend, deps.db,
                owner_id=deps.owner_id, model_id=served_id, usage=run_usage, price=price,
                kind="run", conversation_id=conversation_id, run_id=run_id, agent_name=spec.name,
            )
            cost_usd = round(price.cost_microusd / 1_000_000, 6)
            priced = price.priced
            if not price.priced:
                logger.warning("unpriced model %s billed at pessimistic rates", served_id)
    except Exception:  # noqa: BLE001 — deliberate: runs are not failed over ledger writes:
        logger.exception("spend recording failed for run %s (cost data lost)", run_id)
    return cost_usd, priced


async def _render(spec: AgentSpec, deps: RunDeps, path: list[Item], info) -> tuple[RenderedContext, list[Item]]:
    """
    Rendering context with the summary cache wired to the store and the
    summarizer wired to the cheap model (built through the sanctioned adapter/
    providers helpers, so no pydantic_ai import reaches this module).

    Returns `(rendered, flush_items)`: `flush_items` are the span items that were
    FRESHLY summarized (compacted) this render — non-empty only on a summary
    cache MISS, so the P10/D60 memory flush fires exactly once per span, as it
    leaves the model's context, and never on the cache-hit re-renders after:
    """
    conversation_id = deps.conversation_id
    fresh_summary = {"fired": False}

    if spec.features.summarization:
        summarizer_built = build_model(DEFAULT_SUMMARIZER_MODEL)

        async def on_summarizer_usage(usage: Usage) -> None:
            """Attributing summarization cost to the conversation owner (O6.8)."""
            try:
                price = price_usage(usage, get_model_info(DEFAULT_SUMMARIZER_MODEL), pessimistic=PessimisticRates())
                if price.cost_microusd is not None:
                    await asyncio.to_thread(
                        record_spend, deps.db,
                        owner_id=deps.owner_id, model_id=DEFAULT_SUMMARIZER_MODEL,
                        usage=usage, price=price, kind="summarization",
                        conversation_id=conversation_id,
                    )
            except Exception:  # noqa: BLE001 — summaries must not fail over ledger writes:
                logger.exception("summarizer spend recording failed")

        summarize = _adapter.make_summarizer(summarizer_built, on_usage=on_summarizer_usage)
    else:
        summarize = _no_summarize

    def cache_get(span_end_item_id, budget: int, family: str) -> str | None:
        """Reading a cached summary for a span; None on miss (renderer callback)."""
        # Looking up by the SAME span-end the renderer writes under, so a cached
        # summary actually hits (previously keyed on a different item, so the
        # summarizer re-ran on every render — wasted, non-deterministic paid calls):
        row = get_summary(deps.db, conversation_id, span_end_item_id, budget, family)
        return row.summary_text if row else None

    def cache_put(budget: int, family: str, summary_text: str, span_end_item_id) -> None:
        """Persisting a freshly generated summary; flagging this as a fresh compaction (D60)."""
        fresh_summary["fired"] = True
        put_summary(deps.db, conversation_id, span_end_item_id, budget, family, summary_text)

    rendered = await render_context(
        path, info,
        summarize=summarize,
        cache_get=cache_get,
        cache_put=cache_put,
        allow_summarization=spec.features.summarization,
        alert=lambda msg: logger.error("summarization: %s", msg),
    )
    # The freshly-compacted span (only on a cache miss) is the D60 flush input:
    flush_items: list[Item] = []
    if fresh_summary["fired"] and rendered.summarized_item_ids:
        ids = set(rendered.summarized_item_ids)
        flush_items = [it for it in path if it.id in ids]
    return rendered, flush_items


async def _no_summarize(items, budget):  # pragma: no cover - guarded by allow_summarization
    """Placeholder summarizer for agents with summarization disabled (never called)."""
    raise RuntimeError("summarization disabled")


def _has_audio_part(items: list[Item]) -> bool:
    """Checking whether any item already carries audio (native audio-out ran):"""
    return any(
        getattr(part, "type", None) == "media" and getattr(part, "media", None) == "audio"
        for item in items for part in item.parts
    )


async def _record_media_spend(deps: RunDeps, spec: AgentSpec, run_id: UUID, conversation_id: UUID, item: Item) -> None:
    """
    Recording one fill-generated image as a `kind="image"` ledger row (D44):
    per-unit pricing at the image model's registry rate, pessimistic for
    unpriced models. Ledger failures never fail the completed run (P4 posture):
    """
    from ..billing import PessimisticMediaRates, price_media
    try:
        info = get_model_info(item.model_used or "")
        price = price_media(1, "images", info, pessimistic=PessimisticMediaRates())
        if price.cost_microusd is not None:
            await asyncio.to_thread(
                record_spend, deps.db,
                owner_id=deps.owner_id, model_id=item.model_used or "unknown",
                usage=Usage(), price=price, kind="image",
                conversation_id=conversation_id, run_id=run_id, agent_name=spec.name,
            )
    except Exception:  # noqa: BLE001 — deliberate: completed runs are not failed over ledger writes:
        logger.exception("image spend recording failed for run %s", run_id)


async def _speak_output(
    deps: RunDeps, spec: AgentSpec, run_id: UUID, conversation_id: UUID,
    text: str, item_ids: list[UUID],
) -> MediaGenerated | None:
    """
    The D41 speech stage: voicing the run's output text through the spec's TTS
    model (media port), persisting the audio as a MediaPart item, billing it
    `kind="voice"`, and returning the MediaGenerated event to emit. Any
    failure logs a warning and returns None — silence, never a dead run:
    """
    import base64

    from ..media import price_speech, synthesize_speech
    try:
        result = await synthesize_speech(text, spec.media)
        audio_b64 = base64.b64encode(result.audio).decode()
        item = Item(
            conversation_id=conversation_id, kind=ItemKind.message, role=Role.assistant,
            parts=[MediaPart(media="audio", mime_type=result.mime_type, data_b64=audio_b64)],
            model_used=result.model_id,
            meta={"generated": True, "modality": "voice"},
        )
        stored = await asyncio.to_thread(append_item, deps.db, item, owner_id=deps.owner_id)
        if stored.id is not None:
            item_ids.append(stored.id)

        price = price_speech(result)
        if price.cost_microusd is not None:
            await asyncio.to_thread(
                record_spend, deps.db,
                owner_id=deps.owner_id, model_id=result.model_id,
                usage=result.usage or Usage(), price=price, kind="voice",
                conversation_id=conversation_id, run_id=run_id, agent_name=spec.name,
            )
        return MediaGenerated(
            run_id=run_id, media="audio", mime_type=result.mime_type, data_b64=audio_b64,
        )
    except Exception:  # noqa: BLE001 — deliberate: speech is supplementary, the text already succeeded:
        logger.warning("speech_output stage failed for run %s", run_id, exc_info=True)
        return None


# Keeping strong references to in-flight backfill tasks — asyncio only holds
# weak references, so a fire-and-forget task without one can be garbage
# collected mid-run:
_BACKFILL_TASKS: set[asyncio.Task] = set()


def _schedule_embedding_backfill(session_factory, conversation_id: UUID, owner_id: str) -> None:
    """
    Scheduling the D35 background embedding pass: a detached task with a FRESH
    session embeds every not-yet-vectorized item of the conversation, then
    records the embedding spend (`kind="embedding"`, P4). Strictly best-effort:
    any failure logs a warning (degraded-but-handled — search falls back to
    text matching) and never touches the completed run:
    """
    embedder = _adapter.make_embedder()
    if embedder is None:
        return  # No embeddings key — semantic search simply stays dormant (D35)

    async def _backfill() -> None:
        """Running the embed-and-record pass in a worker thread with its own session."""
        def _work() -> int:
            db = session_factory()
            try:
                count = embed_pending_items(db, conversation_id, embedder, owner_id=owner_id)
                # Attributing the embedding tokens to the owner while the
                # fresh session is still open (same P4 path as runs):
                usage = getattr(embedder, "total_usage", None)
                if count and usage and usage.input_tokens:
                    price = price_usage(
                        usage, get_model_info(_adapter.EMBEDDING_MODEL_ID), pessimistic=PessimisticRates(),
                    )
                    if price.cost_microusd is not None:
                        record_spend(
                            db, owner_id=owner_id, model_id=_adapter.EMBEDDING_MODEL_ID,
                            usage=usage, price=price, kind="embedding",
                            conversation_id=conversation_id,
                        )
                return count
            finally:
                db.close()

        try:
            await asyncio.to_thread(_work)
        except Exception:  # noqa: BLE001 — deliberate: backfill is best-effort by design (D35):
            logger.warning("embedding backfill failed for conversation %s", conversation_id, exc_info=True)

    task = asyncio.create_task(_backfill())
    _BACKFILL_TASKS.add(task)
    task.add_done_callback(_BACKFILL_TASKS.discard)


# Strong refs to in-flight flush tasks (asyncio holds only weak ones):
_FLUSH_TASKS: set[asyncio.Task] = set()


def _schedule_memory_flush(session_factory, owner_id: str, scope: str, conversation_id: UUID, items: list[Item]) -> None:
    """
    Scheduling the D60 memory flush: a detached task with a FRESH session extracts
    durable facts from a just-compacted span and reconciles them into memory —
    the write-completeness safety net for facts the agent did not save in real
    time. Best-effort (a failure logs a warning, never touches the completed
    run); runs the cheap fixed model like the summarizer; anything already
    remembered dedups in reconcile_write, so real-time + flush never double-write:
    """
    from ..memory import reconcile_write
    from .renderer import summarization_input_text

    embedder = _adapter.make_embedder()
    built = build_model(DEFAULT_SUMMARIZER_MODEL)
    valid_at = max((it.created_at for it in items if it.created_at is not None), default=None)
    source_ids = [str(it.id) for it in items if it.id is not None]
    excerpt = summarization_input_text(items)

    async def _flush() -> None:
        """Extracting + reconciling durable facts on a private session (single-threaded, no lock)."""
        db = session_factory()
        try:
            async def _record(usage: Usage) -> None:
                """Attributing the flush's model spend to the owner (kind='memory', P4)."""
                try:
                    price = price_usage(usage, get_model_info(DEFAULT_SUMMARIZER_MODEL), pessimistic=PessimisticRates())
                    if price.cost_microusd is not None:
                        await asyncio.to_thread(
                            record_spend, db, owner_id=owner_id, model_id=DEFAULT_SUMMARIZER_MODEL,
                            usage=usage, price=price, kind="memory", conversation_id=conversation_id,
                        )
                except Exception:  # noqa: BLE001 — the flush must not fail over a ledger write:
                    logger.exception("memory flush spend recording failed")

            extractor = _adapter.make_memory_extractor(built, on_usage=_record)
            resolver = _adapter.make_memory_resolver(built, on_usage=_record)
            for fact in await extractor(excerpt):
                await reconcile_write(
                    db, owner_id=owner_id, scope=scope, text=fact,
                    valid_at=valid_at, source_turn_ids=source_ids,
                    embed_fn=embedder.embed if embedder is not None else None,
                    resolve_fn=resolver,
                )
        except Exception:  # noqa: BLE001 — deliberate: the flush is best-effort by design (D60):
            logger.warning("memory flush failed for conversation %s", conversation_id, exc_info=True)
        finally:
            db.close()

    task = asyncio.create_task(_flush())
    _FLUSH_TASKS.add(task)
    task.add_done_callback(_FLUSH_TASKS.discard)


async def run_agent(spec: AgentSpec, deps: RunDeps, user_input: UserInput) -> RunResult:
    """
    Convenience wrapper (D10): consume the stream and return the final result.
    A run that PAUSED for approval (P8) is not an error — it returns with
    `pending_approvals` populated and `output` None; the caller resumes via
    `resume_agent`:
    """
    return await _collect(stream_agent(spec, deps, user_input))


async def resume_agent_sync(spec: AgentSpec, deps: RunDeps, decisions: list[ApprovalDecision]) -> RunResult:
    """Non-streaming resume (P8): apply decisions, consume the continuation, return the result:"""
    return await _collect(resume_agent(spec, deps, decisions))


async def _collect(stream: AsyncIterator[RunEvent]) -> RunResult:
    """Draining a run stream into a RunResult, honoring both completion and pause (P8):"""
    run_id = None
    output = None
    usage = Usage()
    item_ids: list[UUID] = []
    model_served = ""
    pending: list[PendingApproval] = []
    async for event in stream:
        if isinstance(event, RunStarted):
            run_id, model_served = event.run_id, event.model
        elif isinstance(event, RunCompleted):
            output, usage, item_ids = event.output, event.usage, event.item_ids
        elif isinstance(event, RunPaused):
            usage, item_ids, pending = event.usage, event.item_ids, event.approvals
        elif isinstance(event, RunFailed):
            raise RuntimeError(event.error)
    return RunResult(
        run_id=run_id or uuid4(), output=output, usage=usage,
        item_ids=item_ids, model_served=model_served, pending_approvals=pending,
    )
