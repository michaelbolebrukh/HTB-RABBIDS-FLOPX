"""
Adapter — the Pydantic AI boundary (Phase 2).

FACADE BOUNDARY (D3): with providers.py, the only module allowed to import
`pydantic_ai`. It does four things and nothing else leaks out:

1. build_agent(spec, built_model): construct a Pydantic AI Agent from OUR
   AgentSpec (programmatic construction, D19).
2. to_message_history(items): TWO-LANE translation (D24) of our items into
   Pydantic AI messages — per-item `raw` fast lane for same-provider fidelity,
   universal-field translation otherwise. Our fields are always sufficient;
   `raw` only restores provider-specific part fields (e.g. thinking
   signatures) — the invariant from transcript/types.py.
3. translate_event(event): map Pydantic AI stream events onto OUR RunEvents.
4. items_from_messages(new_messages, ...): turn a completed run's new messages
   back into our transcript items, populating `raw`, `usage`, `model_used`.

Item.kind <-> Pydantic AI Part mapping (1:1 by design):
    message/user   <-> UserPromptPart      (request side)
    message/system <-> SystemPromptPart     (request side)
    tool_result    <-> ToolReturnPart       (request side)
    message/assistant <-> TextPart          (response side)
    thinking       <-> ThinkingPart         (response side)
    tool_call      <-> ToolCallPart          (response side)
"""

from __future__ import annotations

import functools
import inspect
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from pydantic import TypeAdapter
from pydantic_ai import (
    Agent,
    AgentRunResultEvent,
    ApprovalRequired,
    DeferredToolRequests,
    DeferredToolResults,
    Embedder,
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    ModelRetry,
    NativeOutput,
    PartDeltaEvent,
    PartStartEvent,
    PromptedOutput,
    RunContext,
    StructuredDict,
    Tool,
    ToolApproved,
    ToolDenied,
    ToolOutput,
    UsageLimits,
)
from pydantic_ai.capabilities import ImageGeneration, NativeTool, Thinking, WebFetch, WebSearch
from pydantic_ai.embeddings.openai import OpenAIEmbeddingModel
from pydantic_ai.messages import (
    FilePart,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    NativeToolCallPart,
    NativeToolReturnPart,
    RetryPromptPart,
    SystemPromptPart,
    TextPart,
    TextPartDelta,
    ThinkingPart,
    ThinkingPartDelta,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.native_tools import CodeExecutionTool
from pydantic_ai.providers.openai import OpenAIProvider

from ..registry.schema import ModelInfo
from ..transcript.search import EmbedderProtocol
from ..transcript.types import Item, ItemKind, Role, Usage
from .model_info import get_model_info
from .providers import BuiltModel, _api_key
from .tool_table import RegisteredTool, ToolContext, requested_tool_names, split_native_tools
from .types import (
    AgentSpec,
    ApprovalDecision,
    MediaGenerated,
    PendingApproval,
    RunEvent,
    TextDelta,
    ThinkingDelta,
    ToolCallFinished,
    ToolCallStarted,
    ToolExecutionError,
)


logger = logging.getLogger(__name__)

# The fixed embedding model (D35): cheap, priced in the registry feeds, and
# behind our EmbedderProtocol so the search layer never sees the SDK:
EMBEDDING_MODEL_ID = "openai:text-embedding-3-small"


@dataclass
class RunOutcome:
    """
    The end-of-run result the engine needs, in OUR types only (no pydantic_ai
    leaks out). Yielded last by `stream_run`; the engine persists `items` and
    builds RunCompleted from `output`/`usage`:
    """
    items: list[Item] = field(default_factory=list)
    output: Any = None
    usage: Usage = field(default_factory=Usage)
    # Non-empty when the run PAUSED for tool approval (P8): the SDK's
    # DeferredToolRequests output converted to OUR PendingApproval shapes here,
    # so the engine never sees the SDK type (D3). `output` stays None:
    approvals: list[PendingApproval] = field(default_factory=list)


# Per-part TypeAdapters for the raw fast lane (lossless same-provider replay):
_THINKING_ADAPTER = TypeAdapter(ThinkingPart)
_TOOLCALL_ADAPTER = TypeAdapter(ToolCallPart)
_TEXT_ADAPTER = TypeAdapter(TextPart)
_NATIVE_CALL_ADAPTER = TypeAdapter(NativeToolCallPart)
_NATIVE_RETURN_ADAPTER = TypeAdapter(NativeToolReturnPart)


# ============================================================================
# 1. AGENT CONSTRUCTION
# ============================================================================

def select_native_capabilities(spec: AgentSpec, info: ModelInfo) -> list[Any]:
    """
    Building the SDK capability list for a run (D34 native side + Thinking +
    MCP). The native-vs-fill decision itself lives in
    `tool_table.split_native_tools` — this function only CONSTRUCTS the SDK
    objects for the names that came out native, so adapter and engine can
    never disagree about the split:
    """
    capabilities: list[Any] = []

    # Provider-adaptive thinking, gated on the registry (unchanged from P2):
    if spec.features.thinking and info.supports_thinking:
        capabilities.append(Thinking(effort=spec.features.thinking_effort))

    # D34 native tools — only the names the registry marked native for this
    # model; every capability is native-only (no SDK-side local fallback: OUR
    # fill under the same name is the fallback, injected by the engine):
    native_names, _ = split_native_tools(requested_tool_names(spec), info.capabilities)
    for name in native_names:
        if name == "web_search":
            capabilities.append(WebSearch())
        elif name == "web_fetch":
            capabilities.append(WebFetch(native=True))
        elif name == "code_execution":
            capabilities.append(NativeTool(CodeExecutionTool()))

    # D41 image generation (supersedes D40's no-fill clause): the native
    # capability rides along when the registry flags the model; otherwise the
    # ENGINE injects the generate_image fill — this function simply adds
    # nothing, exactly like the D34 dual tools. D45: an EXPLICIT image_model
    # pick beats the native capability (a chat model with weak image output
    # can be paired with the best image model), so the native path is skipped
    # whenever the spec names one:
    if (
        spec.features.image_generation
        and info.capabilities.image_generation
        and spec.media.image_model is None
    ):
        capabilities.append(ImageGeneration())

    # MCP servers are NOT built as SDK capabilities (P9/D52): pydantic-ai's MCP
    # capability pulls fastmcp-slim, which requires starlette>=1.0.1 and conflicts
    # with our FastAPI. Instead the engine bridges each server's tools into our
    # tool table via `extensions.build_mcp_tools`, so they arrive here as ordinary
    # ctx-bound function tools (uniform P8 + billing + offline-testable):
    return capabilities


def choose_output_mode(schema: dict, info: ModelInfo) -> Any:
    """
    Closing D20 — picking the structured-output mode per the run model's
    registry entry: native JSON-schema when the provider supports it,
    tool-based extraction as the portable default, prompted extraction as the
    last resort for models with neither:
    """
    structured = StructuredDict(schema)
    if info.capabilities.structured_output:
        return NativeOutput(structured)
    if info.capabilities.tool_call:
        return ToolOutput(structured)
    return PromptedOutput(structured)


def build_agent(
    spec: AgentSpec,
    built: BuiltModel,
    info: ModelInfo | None = None,
    tools: list[Any] | None = None,
    allow_pause: bool = False,
    extra_system: str | None = None,
) -> Agent:
    """
    Constructing a Pydantic AI Agent from our spec (D19: programmatic, from
    data, never their YAML). `info` is the registry entry of the model that
    will actually serve (post-fallback) — it drives capability selection (D34)
    and output-mode choice (D20); `tools` are the ctx-bound function tools
    from `bind_tools` (the fills + all ordinary registered tools).

    `allow_pause` (P8): when any bound tool can demand approval, the output
    union admits DeferredToolRequests so a gated call ends the run PAUSED
    instead of erroring — agents with no approval surface keep the exact
    pre-P8 output contract:
    """
    if info is None:
        info = get_model_info(spec.model)

    capabilities = select_native_capabilities(spec, info)

    # Mapping our per-run limits onto Pydantic AI's UsageLimits (D23):
    limits = spec.limits

    # Composing instructions: the agent's own text plus any run-scoped system
    # addendum (P9/D53 skill L1 metadata preamble) — still instructions, never a
    # fake system message mid-history (D9/D25):
    instructions = spec.instructions or ""
    if extra_system:
        instructions = f"{instructions}\n\n{extra_system}".strip()

    agent_kwargs: dict[str, Any] = {
        "name": spec.name,
        "instructions": instructions or None,   # D9: instructions, never system_prompt
        "capabilities": capabilities or None,
        "tools": tools or None,
        "retries": max(limits.tool_retries, limits.output_retries),
    }
    if spec.model_settings:
        agent_kwargs["model_settings"] = spec.model_settings

    # Structured output when a schema is declared (D20 — mode per registry):
    output_type: Any = None
    if spec.output_schema is not None:
        output_type = choose_output_mode(spec.output_schema, info)
    if allow_pause:
        output_type = [output_type or str, DeferredToolRequests]
    if output_type is not None:
        agent_kwargs["output_type"] = output_type

    return Agent(built.model, **{k: v for k, v in agent_kwargs.items() if v is not None})


# ============================================================================
# 1b. TOOL BINDING — ctx injection (D11) and the SDK seam for tools
# ============================================================================

def bind_tools(
    resolved: list[RegisteredTool],
    ctx: ToolContext,
    requires_approval: frozenset[str] = frozenset(),
) -> list[Tool]:
    """
    Wrapping registered implementations into SDK Tool objects. The ToolContext
    is injected via a closure and STRIPPED from the model-facing signature, so
    the model sees only the real parameters (name/description/derived schema)
    and its arguments can never reach — or even name — ctx (D11).

    `requires_approval` (P8, D46) names the tools whose EVERY call must pause
    for a decision — only OUR bound tools can appear here by construction:
    native names never reach the fill lane, so native tools are structurally
    ungateable and untouched:
    """
    return [_bind_tool(entry, ctx, gated=entry.name in requires_approval) for entry in resolved]


def _bind_tool(entry: RegisteredTool, ctx: ToolContext, gated: bool = False) -> Tool:
    """
    Binding one implementation: closure-injected ctx + signature surgery for the
    schema. Whole-tool approval (P8, D46 — `gated`) is handled by the SDK's own
    `requires_approval=True` on the Tool, so those tools keep the plain
    ctx-closure wrapper. Only a PER-INVOCATION predicate needs the SDK
    RunContext (to conditionally raise ApprovalRequired and to detect a resumed,
    already-approved call), so only that path takes the SDK context:
    """
    fn = entry.fn
    predicate = entry.approval_predicate

    # Schema-defined tools (the MCP bridge, D52): no Python signature to
    # introspect, so build the SDK tool straight from the JSON schema. The body
    # closes over ctx (bridged MCP bodies ignore it); a tool-level failure still
    # becomes a ModelRetry so the model can react. Whole-tool approval (D46) is
    # best-effort here — `from_schema` has no `requires_approval` param, so it is
    # set post-hoc when the SDK allows it:
    if entry.parameters_schema is not None:
        @functools.wraps(fn)
        async def bound_schema(**kwargs: Any) -> Any:
            try:
                return await fn(ctx, **kwargs)
            except ToolExecutionError as exc:
                raise ModelRetry(str(exc)) from exc

        schema_tool = Tool.from_schema(
            bound_schema, name=entry.name, description=entry.description or "",
            json_schema=entry.parameters_schema, takes_ctx=False,
        )
        if gated:
            try:
                schema_tool.requires_approval = True  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001 — approval on MCP tools is best-effort (D46):
                pass
        return schema_tool

    signature = inspect.signature(fn)
    parameters = list(signature.parameters.values())

    if predicate is None:
        # The unchanged pre-P8 wrapper (also the whole-tool `gated` path — the
        # SDK defers the call before ever invoking this):
        @functools.wraps(fn)
        async def bound(*args: Any, **kwargs: Any) -> Any:
            try:
                return await fn(ctx, *args, **kwargs)
            except ToolExecutionError as exc:
                raise ModelRetry(str(exc)) from exc

        bound.__signature__ = signature.replace(parameters=parameters[1:])  # type: ignore[attr-defined]
        annotations = dict(getattr(fn, "__annotations__", {}))
        if parameters:
            annotations.pop(parameters[0].name, None)
        bound.__annotations__ = annotations
        return Tool(
            bound, name=entry.name, description=entry.description,
            takes_ctx=False, requires_approval=gated,
        )

    # Per-invocation predicate: the SDK RunContext replaces our ctx as the
    # first parameter (takes_ctx=True strips it from the model-facing schema
    # exactly as before). `tool_call_approved` is how a resumed, already-
    # approved call skips re-evaluating the predicate and just runs:
    @functools.wraps(fn)
    async def bound_ctx(run_ctx: RunContext, *args: Any, **kwargs: Any) -> Any:
        if not run_ctx.tool_call_approved and predicate(*args, **kwargs):
            raise ApprovalRequired
        try:
            return await fn(ctx, *args, **kwargs)
        except ToolExecutionError as exc:
            raise ModelRetry(str(exc)) from exc

    run_ctx_param = inspect.Parameter(
        "run_ctx", inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=RunContext,
    )
    bound_ctx.__signature__ = signature.replace(parameters=[run_ctx_param, *parameters[1:]])  # type: ignore[attr-defined]
    annotations = dict(getattr(fn, "__annotations__", {}))
    if parameters:
        annotations.pop(parameters[0].name, None)
    annotations["run_ctx"] = RunContext
    bound_ctx.__annotations__ = annotations
    return Tool(
        bound_ctx, name=entry.name, description=entry.description,
        takes_ctx=True, requires_approval=gated,
    )


# ============================================================================
# 1c. EMBEDDER — the D35 seam (SDK embeddings behind the P1 protocol)
# ============================================================================

class _SDKEmbedder:
    """
    OpenAI embeddings behind the P1 EmbedderProtocol. Accumulates the token
    usage of every call so the engine can price the backfill afterwards
    (spend recorded `kind="embedding"`, P4):
    """

    def __init__(self, embedder: Embedder):
        self._embedder = embedder
        self.total_usage = Usage()

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Embedding texts synchronously (callers hop via to_thread), tallying usage:"""
        result = self._embedder.embed_documents_sync(texts)
        self.total_usage.input_tokens += getattr(result.usage, "input_tokens", 0) or 0
        return [list(vector) for vector in result.embeddings]


def make_embedder() -> EmbedderProtocol | None:
    """
    Building the embedder for semantic conversation search (D35). Key passed
    explicitly per D17; returns None when no OpenAI key exists — search then
    degrades gracefully to text matching (never an error, never a run failure):
    """
    key = _api_key("openai")
    if key is None:
        return None
    model = OpenAIEmbeddingModel(
        EMBEDDING_MODEL_ID.split(":", 1)[1], provider=OpenAIProvider(api_key=key),
    )
    return _SDKEmbedder(Embedder(model))


def usage_limits_for(spec: AgentSpec, budget_output_cap: int | None = None) -> UsageLimits:
    """
    Building the per-run UsageLimits from the spec's AgentLimits, min-combined
    with the budget-derived output cap when one applies (D23 — the run may
    never spend more output tokens than the remaining budget affords):
    """
    limits = spec.limits
    output_cap = limits.max_output_tokens
    if budget_output_cap is not None:
        output_cap = min(output_cap, budget_output_cap) if output_cap else budget_output_cap
    return UsageLimits(
        request_limit=limits.max_requests,
        tool_calls_limit=limits.max_tool_calls,
        output_tokens_limit=output_cap,
    )


# ============================================================================
# 2. TWO-LANE TRANSLATION: our items -> Pydantic AI message_history
# ============================================================================

# Which item kinds are request-side (to the model) vs response-side (from it):
_REQUEST_KINDS = {ItemKind.tool_result}
_RESPONSE_KINDS = {ItemKind.thinking, ItemKind.tool_call}


def _is_request_side(item: Item) -> bool:
    """Deciding whether an item's part belongs in a ModelRequest (vs ModelResponse):"""
    if item.kind == ItemKind.message:
        return item.role in (Role.user, Role.system, Role.tool)
    # Native (server-side) tool results live in the RESPONSE — the provider
    # executed the tool itself and reported the return inline (D34):
    if item.kind == ItemKind.tool_result and item.meta.get("implementation") == "native":
        return False
    return item.kind in _REQUEST_KINDS


def _same_provider(item: Item, provider: str) -> bool:
    """Checking whether an item was produced by the current run's provider (fast-lane gate):"""
    if not item.model_used:
        return False
    return item.model_used.split(":", 1)[0] == provider


def _part_from_item(item: Item, provider: str):
    """
    Building the Pydantic AI part for one item — the two lanes:
    - fast lane: same-provider item with a stored `raw` part -> reconstruct it
      exactly (restores signatures/provider fields the universal form drops);
    - translation lane: build the part from our universal fields.
    """
    text = item.extract_text() or ""
    is_native = item.meta.get("implementation") == "native"

    # Fast lane — reconstruct the provider-specific part verbatim (D24):
    if item.raw is not None and _same_provider(item, provider):
        try:
            if item.kind == ItemKind.thinking:
                return _THINKING_ADAPTER.validate_python(item.raw)
            if item.kind == ItemKind.tool_call:
                return (_NATIVE_CALL_ADAPTER if is_native else _TOOLCALL_ADAPTER).validate_python(item.raw)
            if item.kind == ItemKind.tool_result and is_native:
                return _NATIVE_RETURN_ADAPTER.validate_python(item.raw)
            if item.kind == ItemKind.message and item.role == Role.assistant:
                return _TEXT_ADAPTER.validate_python(item.raw)
        except Exception:  # noqa: BLE001 — raw is a bonus; fall through to translation:
            pass

    # Native tool internals (D34) are provider-specific by nature: replaying a
    # provider's server-side search/fetch trace to a DIFFERENT provider would
    # send it another vendor's wire format as if it were a function tool. The
    # visible outcome already lives in the assistant text, so cross-provider
    # these items are simply omitted (never mistranslated):
    if is_native and item.kind in (ItemKind.tool_call, ItemKind.tool_result):
        return None

    # Translation lane — always sufficient, works cross-provider:
    if item.kind == ItemKind.message:
        if item.role == Role.user:
            return UserPromptPart(content=text)
        if item.role == Role.system:
            return SystemPromptPart(content=text)
        if item.role == Role.assistant:
            # A media-only assistant item (D40 generated image) has no text to
            # replay; sending an empty TextPart would be worse than omitting —
            # the adjacent assistant text already references the image:
            if not text and any(getattr(p, "type", None) == "media" for p in item.parts):
                return None
            return TextPart(content=text)
        if item.role == Role.tool:
            return ToolReturnPart(tool_name=item.tool_name or "", content=text, tool_call_id=item.tool_call_id or "")
    if item.kind == ItemKind.thinking:
        return ThinkingPart(content=text)
    if item.kind == ItemKind.tool_call:
        args = next((p.data for p in item.parts if getattr(p, "type", None) == "data"), {})
        return ToolCallPart(tool_name=item.tool_name or "", args=args, tool_call_id=item.tool_call_id or "")
    if item.kind == ItemKind.tool_result:
        result = next((p.data for p in item.parts if getattr(p, "type", None) == "data"), text)
        return ToolReturnPart(tool_name=item.tool_name or "", content=result, tool_call_id=item.tool_call_id or "")
    return None  # summary_marker / event items carry no model-facing part


def to_message_history(
    items: list[Item],
    provider: str,
    summary_text: str | None = None,
    resume: bool = False,
) -> list[ModelMessage]:
    """
    Translating rendered items into a Pydantic AI message history, grouping
    consecutive same-direction parts into ModelRequest/ModelResponse messages.

    A summary preamble (from the renderer) is prepended as a delimited user
    context block — not instructions (agent identity) and not a fake system
    message mid-history (D25 injection form).

    Approval states (P8): a provider must NEVER see a bare tool_call without
    its result — except on a resume run, where the deferred results supply
    them. So a `pending` call gets a synthetic "not executed" return on
    ordinary runs (resume=False) and rides bare on resume; an `abandoned`
    call (the conversation moved on) is always paired synthetically:
    """
    messages: list[ModelMessage] = []
    request_parts: list[Any] = []
    response_parts: list[Any] = []

    def flush_request():
        """Emitting the buffered request-side parts as one ModelRequest, then clearing."""
        if request_parts:
            messages.append(ModelRequest(parts=list(request_parts)))
            request_parts.clear()

    def flush_response():
        """Emitting the buffered response-side parts as one ModelResponse, then clearing."""
        if response_parts:
            messages.append(ModelResponse(parts=list(response_parts)))
            response_parts.clear()

    # Prepending the summary as the first request's context block:
    if summary_text:
        request_parts.append(UserPromptPart(
            content=f"[Conversation summary so far]\n{summary_text}\n[End summary]"
        ))

    for item in items:
        part = _part_from_item(item, provider)
        if part is None:
            continue
        if _is_request_side(item):
            flush_response()
            request_parts.append(part)
        else:
            flush_request()
            response_parts.append(part)

        # Pairing an unresolved approval-gated call with a synthetic return so
        # provider pairing never breaks (P8) — see the docstring's state table:
        approval_state = item.meta.get("approval") if item.kind == ItemKind.tool_call else None
        if approval_state == "abandoned" or (approval_state == "pending" and not resume):
            flush_response()
            request_parts.append(ToolReturnPart(
                tool_name=item.tool_name or "",
                content="not executed: approval was not granted before the conversation continued",
                tool_call_id=item.tool_call_id or "",
            ))

    flush_request()
    flush_response()
    return messages


def _args_as_dict(args: Any) -> dict | None:
    """
    Normalizing a tool call's arguments to a dict for the PendingApproval shape
    (P8): the SDK carries deferred-call args as either a dict or a JSON string,
    so a string is parsed; anything unparseable surfaces as None (the UI still
    has the tool name to render the decision prompt):
    """
    if isinstance(args, dict):
        return args
    if isinstance(args, str):
        import json
        try:
            parsed = json.loads(args)
            return parsed if isinstance(parsed, dict) else None
        except (ValueError, TypeError):
            return None
    return None


def make_deferred_results(decisions: list[ApprovalDecision]) -> DeferredToolResults:
    """
    Converting OUR approval decisions into the SDK's resume payload (P8): an
    approval optionally carries edited args; a denial becomes the ToolDenied
    message the model reads as the tool result (D48 — data, never an error):
    """
    results = DeferredToolResults()
    for decision in decisions:
        if decision.approved:
            results.approvals[decision.tool_call_id] = (
                ToolApproved(override_args=decision.override_args)
                if decision.override_args is not None else True
            )
        else:
            results.approvals[decision.tool_call_id] = ToolDenied(
                decision.reason or "The user denied this tool call."
            )
    return results


# ============================================================================
# 3. EVENT TRANSLATION: Pydantic AI stream events -> our RunEvents
# ============================================================================

def translate_event(event: Any, run_id: UUID) -> RunEvent | None:
    """
    Mapping a Pydantic AI stream event onto our RunEvent, or None when the
    event has no conversation-level meaning (start/end markers we synthesize
    ourselves). The whole translation table lives HERE and nowhere else:
    """
    # Streaming text/thinking deltas arrive as PartDeltaEvents:
    if isinstance(event, PartDeltaEvent):
        delta = event.delta
        if isinstance(delta, TextPartDelta) and delta.content_delta:
            return TextDelta(run_id=run_id, delta=delta.content_delta)
        if isinstance(delta, ThinkingPartDelta) and delta.content_delta:
            return ThinkingDelta(run_id=run_id, delta=delta.content_delta)
        return None

    # A new part starting: text/thinking carry initial content; native
    # (provider-executed) tool activity streams as part starts rather than
    # function events, and is surfaced through the SAME ToolCallStarted/
    # Finished events so UIs are indifferent to which implementation ran (D34):
    if isinstance(event, PartStartEvent):
        part = event.part
        if isinstance(part, TextPart) and part.content:
            return TextDelta(run_id=run_id, delta=part.content)
        if isinstance(part, ThinkingPart) and part.content:
            return ThinkingDelta(run_id=run_id, delta=part.content)
        # Generated media (D40) surfaces the moment the part arrives so a
        # streaming UI can render the image without waiting for run end:
        if isinstance(part, FilePart):
            media, mime, data_b64 = _media_from_file_part(part)
            return MediaGenerated(run_id=run_id, media=media, mime_type=mime, data_b64=data_b64)
        if isinstance(part, NativeToolCallPart):
            return ToolCallStarted(
                run_id=run_id, tool_name=part.tool_name,
                tool_call_id=part.tool_call_id,
                args=part.args if isinstance(part.args, dict) else None,
            )
        if isinstance(part, NativeToolReturnPart):
            return ToolCallFinished(
                run_id=run_id, tool_call_id=part.tool_call_id,
                result_preview=str(part.content)[:200],
            )
        return None

    # A tool being invoked by the model:
    if isinstance(event, FunctionToolCallEvent):
        call = event.part
        args = call.args if isinstance(call.args, dict) else None
        return ToolCallStarted(
            run_id=run_id, tool_name=call.tool_name,
            tool_call_id=call.tool_call_id, args=args,
        )

    # A tool returning its result (the event carries the content that will be
    # sent back to the model plus the resolving call id):
    if isinstance(event, FunctionToolResultEvent):
        preview = "" if event.content is None else str(event.content)[:200]
        return ToolCallFinished(
            run_id=run_id,
            tool_call_id=event.tool_call_id,
            result_preview=preview,
        )

    return None


# ============================================================================
# 4. RESULT -> our transcript items (raw / usage / model_used populated)
# ============================================================================

def _usage_from_response(response: ModelResponse) -> Usage | None:
    """Extracting our Usage from a ModelResponse's usage block (None when absent):"""
    u = getattr(response, "usage", None)
    if u is None:
        return None
    return Usage(
        input_tokens=getattr(u, "input_tokens", 0) or 0,
        output_tokens=getattr(u, "output_tokens", 0) or 0,
        cache_read_tokens=getattr(u, "cache_read_tokens", None),
        cache_write_tokens=getattr(u, "cache_write_tokens", None),
    )


def items_from_messages(
    new_messages: list[ModelMessage],
    conversation_id: UUID,
    model_served: str,
) -> list[Item]:
    """
    Converting a completed run's NEW messages into our transcript items,
    one item per part, with `raw` capturing the provider-specific part for the
    fast lane, `usage` attached to the primary response item, and `model_used`
    set to the model that actually served (post-fallback). The initial user
    request is NOT re-emitted here — the engine appends the user item before
    the run (see engine.py).
    """
    items: list[Item] = []
    for message in new_messages:
        if isinstance(message, ModelResponse):
            usage = _usage_from_response(message)
            usage_attached = False
            for part in message.parts:
                item = _response_part_to_item(part, conversation_id, model_served)
                if item is None:
                    continue
                # Attaching usage to the first item of the response (avoids double counting):
                if usage is not None and not usage_attached:
                    item.usage = usage
                    usage_attached = True
                items.append(item)
        elif isinstance(message, ModelRequest):
            for part in message.parts:
                # A normal tool return -> tool_result item (ours = our bound
                # function tools ran it, as opposed to a provider-native run):
                if isinstance(part, ToolReturnPart):
                    items.append(Item(
                        conversation_id=conversation_id,
                        kind=ItemKind.tool_result,
                        role=Role.tool,
                        tool_name=part.tool_name,
                        tool_call_id=part.tool_call_id,
                        parts=_data_parts(part.content),
                        model_used=model_served,
                        meta={"implementation": "ours"},
                    ))
                # A tool-scoped retry (invalid args / ModelRetry) also RESOLVES a
                # prior tool_call — it must be stored as a tool_result, else the
                # failed tool_call is left unpaired and every later same-provider
                # run is rejected by the provider (orphan tool_use -> 400). A
                # RetryPromptPart WITHOUT a tool_name is an output-level retry,
                # not tool-scoped, so it is not a tool_result and is skipped:
                elif isinstance(part, RetryPromptPart) and part.tool_name and part.tool_call_id:
                    items.append(Item(
                        conversation_id=conversation_id,
                        kind=ItemKind.tool_result,
                        role=Role.tool,
                        tool_name=part.tool_name,
                        tool_call_id=part.tool_call_id,
                        parts=_data_parts({"retry": True, "error": str(part.content)}),
                        model_used=model_served,
                    ))
    return items


def _response_part_to_item(part: Any, conversation_id: UUID, model_served: str) -> Item | None:
    """
    Building one item from a ModelResponse part, storing `raw` for the fast
    lane. Tool items carry `meta.implementation` ("ours" for our bound
    function tools, "native" for provider-executed tools) so analytics and
    cross-provider rendering can tell which ran (D34):
    """
    from ..transcript.types import DataPart, TextPart as OurTextPart

    if isinstance(part, TextPart):
        return Item(
            conversation_id=conversation_id, kind=ItemKind.message, role=Role.assistant,
            parts=[OurTextPart(text=part.content)], model_used=model_served,
            raw=_TEXT_ADAPTER.dump_python(part, mode="json"),
        )
    if isinstance(part, ThinkingPart):
        return Item(
            conversation_id=conversation_id, kind=ItemKind.thinking, role=Role.assistant,
            parts=[OurTextPart(text=part.content)], model_used=model_served,
            raw=_THINKING_ADAPTER.dump_python(part, mode="json"),   # Preserves signature (fast lane)
        )
    # NativeToolCallPart subclasses BaseToolCallPart, not ToolCallPart, so the
    # isinstance checks are disjoint — but native is tested first defensively:
    if isinstance(part, NativeToolCallPart):
        args = part.args if isinstance(part.args, dict) else {"raw_args": part.args}
        return Item(
            conversation_id=conversation_id, kind=ItemKind.tool_call, role=Role.assistant,
            tool_name=part.tool_name, tool_call_id=part.tool_call_id,
            parts=[DataPart(data=args)], model_used=model_served,
            raw=_NATIVE_CALL_ADAPTER.dump_python(part, mode="json"),
            meta={"implementation": "native"},
        )
    if isinstance(part, NativeToolReturnPart):
        return Item(
            conversation_id=conversation_id, kind=ItemKind.tool_result, role=Role.tool,
            tool_name=part.tool_name, tool_call_id=part.tool_call_id,
            parts=_data_parts(part.content), model_used=model_served,
            raw=_NATIVE_RETURN_ADAPTER.dump_python(part, mode="json"),
            meta={"implementation": "native"},
        )
    if isinstance(part, ToolCallPart):
        args = part.args if isinstance(part.args, dict) else {"raw_args": part.args}
        return Item(
            conversation_id=conversation_id, kind=ItemKind.tool_call, role=Role.assistant,
            tool_name=part.tool_name, tool_call_id=part.tool_call_id,
            parts=[DataPart(data=args)], model_used=model_served,
            raw=_TOOLCALL_ADAPTER.dump_python(part, mode="json"),
            meta={"implementation": "ours"},
        )
    # Generated media (D40): persisted as a MediaPart message item — the
    # universal form IS the image, so no `raw` fast lane is needed:
    if isinstance(part, FilePart):
        from ..transcript.types import MediaPart as OurMediaPart
        media, mime, data_b64 = _media_from_file_part(part)
        return Item(
            conversation_id=conversation_id, kind=ItemKind.message, role=Role.assistant,
            parts=[OurMediaPart(media=media, mime_type=mime, data_b64=data_b64)],
            model_used=model_served, meta={"generated": True},
        )
    return None


def _media_from_file_part(part: FilePart) -> tuple[str, str | None, str | None]:
    """
    Extracting (our media kind, mime type, base64 data) from a FilePart's
    binary content — defensive on the SDK object shape, defaulting to image
    (the only media models generate through capabilities today):
    """
    import base64

    content = part.content
    mime = getattr(content, "media_type", None)
    data = getattr(content, "data", None)
    data_b64 = base64.b64encode(data).decode() if isinstance(data, (bytes, bytearray)) else None
    kind = (mime or "image/").split("/", 1)[0]
    media = kind if kind in ("image", "audio", "video") else "file"
    return media, mime, data_b64


def _data_parts(content: Any) -> list:
    """Wrapping arbitrary tool-return content as our part list (dict -> DataPart, else text):"""
    from ..transcript.types import DataPart, TextPart as OurTextPart
    if isinstance(content, dict):
        return [DataPart(data=content)]
    return [OurTextPart(text=str(content))]


# ============================================================================
# 5. HIGH-LEVEL RUN — owns the whole pydantic_ai streaming loop (keeps D3 clean)
# ============================================================================

async def stream_run(
    spec: AgentSpec,
    built: BuiltModel,
    prompt: str | None,
    history_items: list[Item],
    summary_text: str | None,
    run_id: UUID,
    conversation_id: UUID,
    model_served: str,
    budget_output_cap: int | None = None,
    info: ModelInfo | None = None,
    tools: list[Any] | None = None,
    allow_pause: bool = False,
    resume_decisions: list[ApprovalDecision] | None = None,
    extra_system: str | None = None,
) -> AsyncIterator[RunEvent | RunOutcome]:
    """
    Running the agent and yielding OUR events as they stream, then a final
    RunOutcome. This is where the entire `pydantic_ai` run lifecycle lives, so
    the engine never imports the SDK (D3): it builds the agent (with the
    ctx-bound `tools` from bind_tools and the served model's registry `info`
    driving D34/D20 selection), translates the rendered history via the
    two-lane rule, drives `run_stream_events`, converts events, and turns the
    completed run's new messages into our items.

    P8: `allow_pause` admits DeferredToolRequests output (approval-gated
    tools); `resume_decisions` makes this a RESUME run — no new prompt, the
    history's bare pending calls resolved by the converted decisions:
    """
    agent = build_agent(spec, built, info=info, tools=tools, allow_pause=allow_pause, extra_system=extra_system)
    message_history = to_message_history(
        history_items, provider=built.served_by, summary_text=summary_text,
        resume=resume_decisions is not None,
    )

    final_result = None
    async with agent.run_stream_events(
        # `prompt` is a string (possibly "") for a fresh turn and None only on
        # resume, where the deferred results + history carry the run — passing
        # it verbatim preserves the pre-P8 empty-prompt behavior:
        prompt,
        message_history=message_history or None,
        deferred_tool_results=make_deferred_results(resume_decisions) if resume_decisions else None,
        usage_limits=usage_limits_for(spec, budget_output_cap),
    ) as events:
        async for event in events:
            translated = translate_event(event, run_id)
            if translated is not None:
                yield translated
            if isinstance(event, AgentRunResultEvent):
                final_result = event.result

    # Building the outcome in our types (items carry raw/usage/model_used):
    outcome = RunOutcome()
    if final_result is not None:
        outcome.items = items_from_messages(final_result.new_messages(), conversation_id, model_served)
        outcome.output = final_result.output
        # A paused run (P8): the SDK output is its DeferredToolRequests —
        # converted HERE to our PendingApproval shapes so nothing above this
        # module ever sees an SDK type (D3). The visible output stays None:
        if isinstance(outcome.output, DeferredToolRequests):
            outcome.approvals = [
                PendingApproval(
                    tool_call_id=call.tool_call_id,
                    tool_name=call.tool_name,
                    args=_args_as_dict(call.args),
                )
                for call in outcome.output.approvals
            ]
            outcome.output = None
        # Taking the run TOTAL from pydantic_ai's authoritative RunUsage rather
        # than re-summing per-item usage: the per-item sum drops the usage of
        # responses whose parts we don't store (e.g. native web-search turns)
        # and only carries input/output, missing reasoning/cache dimensions:
        outcome.usage = _run_usage(final_result)
    yield outcome


def _run_usage(final_result: Any) -> Usage:
    """Mapping pydantic_ai's authoritative RunUsage total onto our Usage (all dimensions):"""
    try:
        # `usage` is a property (RunUsage) in pydantic-ai 2.5 but was a method
        # in earlier versions — handling both so a silent zero can't recur:
        u = final_result.usage
        if callable(u):
            u = u()
    except Exception:  # noqa: BLE001 — usage is best-effort, never fail a run over it:
        return Usage()
    return Usage(
        input_tokens=getattr(u, "input_tokens", 0) or 0,
        output_tokens=getattr(u, "output_tokens", 0) or 0,
        cache_read_tokens=getattr(u, "cache_read_tokens", None),
        cache_write_tokens=getattr(u, "cache_write_tokens", None),
    )


def make_summarizer(built: BuiltModel, on_usage=None):
    """
    Building the injected summarize callback (O6.1: a cheap fixed model, never
    the conversation model). Owns the pydantic_ai Agent so the renderer/engine
    stay SDK-free; returns an `async (items, budget) -> str` the renderer calls.
    `on_usage` (optional async callback) receives the summarizer run's Usage so
    the engine can attribute its cost to the conversation owner (O6.8/P4):
    """
    from .renderer import summarization_input_text

    summarizer = Agent(
        built.model,
        name="summarizer",
        instructions=(
            "You compress conversation history. Preserve decisions made, facts, names, "
            "numbers, user preferences, open questions, and current task state. Drop "
            "pleasantries and redundancy. Output a dense factual summary, no preamble."
        ),
    )

    async def summarize(items: list[Item], budget: int) -> str:
        """Compressing items to <= budget tokens via the cheap summarizer model (renderer callback)."""
        text = summarization_input_text(items)
        result = await summarizer.run(
            f"Summarize this conversation in at most ~{budget} tokens:\n\n{text}",
            usage_limits=UsageLimits(output_tokens_limit=budget),
        )
        # Reporting the summarizer's own token spend for cost attribution (P4):
        if on_usage is not None:
            await on_usage(_run_usage(result))
        return str(result.output)

    return summarize


def make_memory_resolver(built: BuiltModel, on_usage=None):
    """
    Building the injected memory `ResolveFn` (P10): a cheap fixed model that,
    given a new fact and indexed candidates, returns which are duplicates and
    which are contradicted. Owns the pydantic_ai Agent so `memory/` stays
    SDK-free (D3); returns an `async (new_text, candidate_texts) -> ResolveResult`
    the reconciler calls. `on_usage` (optional async) receives the resolve run's
    Usage for cost attribution, exactly like the summarizer (O6.8/P4):
    """
    from ..memory.reconcile import (
        RESOLVE_INSTRUCTIONS,
        ResolveResult,
        build_resolve_prompt,
        parse_resolve_response,
    )

    resolver = Agent(built.model, name="memory-resolver", instructions=RESOLVE_INSTRUCTIONS)

    async def resolve(new_text: str, candidate_texts: list[str]) -> ResolveResult:
        """Classifying candidates via the cheap resolver model; parsing its JSON defensively."""
        if not candidate_texts:
            return ResolveResult()
        result = await resolver.run(
            build_resolve_prompt(new_text, candidate_texts),
            usage_limits=UsageLimits(output_tokens_limit=512),
        )
        if on_usage is not None:
            await on_usage(_run_usage(result))
        return parse_resolve_response(str(result.output), len(candidate_texts))

    return resolve


def make_memory_extractor(built: BuiltModel, on_usage=None):
    """
    Building the injected memory fact-extractor (P10/D60): a cheap fixed model
    that reads a to-be-archived conversation span and returns durable facts to
    persist. Owns the pydantic_ai Agent so `memory/` stays SDK-free (D3); returns
    an `async (excerpt) -> list[str]`. `on_usage` attributes its spend like the
    resolver/summarizer (O6.8/P4):
    """
    from ..memory.extract import (
        EXTRACT_INSTRUCTIONS,
        build_extract_prompt,
        parse_extract_response,
    )

    extractor = Agent(built.model, name="memory-extractor", instructions=EXTRACT_INSTRUCTIONS)

    async def extract(excerpt: str) -> list[str]:
        """Extracting durable facts from an archived span; parsing the JSON defensively."""
        if not excerpt.strip():
            return []
        result = await extractor.run(
            build_extract_prompt(excerpt),
            usage_limits=UsageLimits(output_tokens_limit=1024),
        )
        if on_usage is not None:
            await on_usage(_run_usage(result))
        return parse_extract_response(str(result.output))

    return extract
