"""
Tool table — registration in CODE, selection in DATA (Phase 5, D33).

The trust boundary for agent tools: implementations can ONLY enter the system
through `register_tool(...)` calls in code (platform tools from
`platform_tools.py`, product tools from the product boundary at import/startup
time). An `AgentSpec` — which is data and may eventually come from a database
or an API — carries tool NAMES only, so a spec can never smuggle in an
implementation. A spec naming an unregistered tool fails agent construction
loudly (`ToolNotRegisteredError`), never silently.

No `pydantic_ai` import here (D3): the table stores plain async callables; the
adapter's `bind_tools` is the only place they are wrapped into SDK objects.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from uuid import UUID

if TYPE_CHECKING:  # Memory injected-callable types (P10) — typing only, no import cycle:
    from ..memory.reconcile import EmbedFn, ResolveFn

from sqlalchemy.orm import Session

from .types import MediaModels, ToolAlreadyRegisteredError, ToolNotRegisteredError

if TYPE_CHECKING:  # Import-cycle guard — these are only needed for typing:
    from ..registry.schema import Capabilities
    from ..sandbox.types import SkillRef
    from ..transcript.search import EmbedderProtocol
    from ..transcript.types import Item
    from .types import AgentFeatures, AgentSpec


@dataclass
class ToolContext:
    """
    What every tool implementation receives as its first argument — and the
    model never sees (D11 trust boundary): the adapter injects this object via
    a closure, so model-provided arguments can never override or even name it.
    Tool RESULTS flowing back are untrusted model input, not instructions —
    the transcript stores them as data items, never as prompts.
    """
    db: Session                            # The run's session; long work hops via to_thread
    owner_id: str
    conversation_id: UUID
    features: "AgentFeatures"              # e.g. rag_scope_all_branches gates D30 scope
    embedder: "EmbedderProtocol | None" = None  # Semantic search when available; text fallback when None
    # Memory scope + injected callables (P10/D59): memory is durable ACROSS
    # conversations, so it is keyed per (owner_id, memory_scope) — a free
    # partition string, NOT the conversation and NOT necessarily the agent, so
    # agents sharing a scope share memory. The resolve/embed fns are built by
    # the engine through the adapter and injected here so the memory tools never
    # import pydantic_ai (D3):
    memory_scope: str = ""
    current_turn_id: UUID | None = None    # The user turn that triggered this run (memory provenance)
    memory_embed_fn: "EmbedFn | None" = None
    memory_resolve_fn: "ResolveFn | None" = None
    # Per-run guard for the shared session (P10): pydantic_ai may execute several
    # tool calls from one model step CONCURRENTLY, and a SQLAlchemy Session is
    # not safe for concurrent use. Every tool that touches `db` holds this lock
    # around its DB hop, so their DB critical sections serialize while the slow
    # parts (network/LLM) still overlap. Created fresh per run (per ToolContext):
    db_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    extra: Any = None                      # Product extension slot (carried from RunDeps)
    # Speciality-model choices for media-producing tools (P7, D42):
    media: MediaModels = field(default_factory=MediaModels)
    # Items a tool wants persisted WITH the run's output (P7, D41): the
    # generate_image fill collects its MediaPart items here so the engine
    # appends them atomically with the outcome (and the model's tool result
    # stays a small reference — never megabytes of base64 in context):
    media_items: "list[Item]" = field(default_factory=list)
    # Skills attached to this agent (P9, D53): the open_skill tool reads their
    # SKILL.md bodies from here, and code_execution mounts their bundles into
    # the sandbox at /skills/<id>/ — the model reads/runs them on demand:
    skills: "list[SkillRef]" = field(default_factory=list)


# Tool implementations are async, take the injected ToolContext first, then the
# model-facing arguments (which define the schema the model sees):
ToolFn = Callable[..., Awaitable[Any]]


@dataclass(frozen=True)
class RegisteredTool:
    """
    One table entry: the callable plus the metadata the model will see. The
    parameters schema is normally DERIVED from the function signature by the
    adapter (pydantic does this better than hand-written dicts); an explicit
    `parameters_schema` is stored for future consumers (e.g. remote/MCP-style
    tools whose schema cannot be introspected):
    """
    name: str
    fn: ToolFn
    description: str
    parameters_schema: dict | None = None
    # Per-invocation approval gate (P8): a sync predicate over the MODEL-FACING
    # arguments (same signature as the tool minus ctx) returning True when THIS
    # call needs an explicit decision ("only above the threshold"). Registered
    # in code with the tool — the tool author knows which arg shapes are
    # consequential; specs stay data. Whole-tool gating is the spec's
    # `requires_approval` list instead:
    approval_predicate: Callable[..., bool] | None = None


# The global table — module-level on purpose: registration happens at import/
# startup (code), and every run reads the same table (D33). Tests snapshot and
# restore it around mutations:
_TOOL_TABLE: dict[str, RegisteredTool] = {}


def register_tool(
    name: str,
    fn: ToolFn,
    *,
    description: str,
    parameters_schema: dict | None = None,
    approval_predicate: Callable[..., bool] | None = None,
) -> None:
    """
    Registering a tool implementation under a name (import-time, code-only —
    D33). Duplicate names raise: two implementations silently shadowing each
    other is exactly the confusion the loud-failure policy exists to prevent:
    """
    if name in _TOOL_TABLE:
        raise ToolAlreadyRegisteredError(f"Tool {name!r} is already registered")
    _TOOL_TABLE[name] = RegisteredTool(
        name=name, fn=fn, description=description, parameters_schema=parameters_schema,
        approval_predicate=approval_predicate,
    )


def resolve_tools(names: list[str]) -> list[RegisteredTool]:
    """
    Resolving spec-declared names into registered implementations, preserving
    order and dropping duplicates. An unknown name raises at agent
    construction (loud, per D33) — a spec must never run with fewer tools than
    it declared:
    """
    resolved: list[RegisteredTool] = []
    seen: set[str] = set()
    for name in names:
        if name in seen:
            continue
        seen.add(name)
        entry = _TOOL_TABLE.get(name)
        if entry is None:
            raise ToolNotRegisteredError(
                f"Tool {name!r} is not registered (registered: {sorted(_TOOL_TABLE)})"
            )
        resolved.append(entry)
    return resolved


def registered_tool_names() -> list[str]:
    """Listing the currently registered tool names (introspection/diagnostics):"""
    return sorted(_TOOL_TABLE)


# ============================================================================
# D34 selection — one tool name, two implementations, the registry decides
# ============================================================================

# Tool names that exist BOTH as a provider-native capability and as our
# client-side fill. The names deliberately match the registry Capabilities
# field names, so the flag lookup is direct. Agents and transcripts are
# indifferent to which implementation runs:
DUAL_IMPLEMENTATION_TOOLS = ("web_search", "web_fetch", "code_execution")


def requested_tool_names(spec: "AgentSpec") -> list[str]:
    """
    Computing the full ordered tool-name list a spec asks for: its explicit
    `tools`, plus the implicit names its feature toggles imply —
    `features.rag_search` implies `search_conversation` (requirement 7) and
    `features.web_search` implies `web_search`. Order-preserving, no dupes:
    """
    names = list(spec.tools)
    if spec.features.rag_search and "search_conversation" not in names:
        names.append("search_conversation")
    if spec.features.web_search and "web_search" not in names:
        names.append("web_search")
    return names


def split_native_tools(names: list[str], capabilities: "Capabilities") -> tuple[list[str], list[str]]:
    """
    The D34 decision, in ONE place: splitting requested names into
    (native_names, fill_names). A dual-implementation name goes native when
    the run model's registry flag says the provider offers it; everything
    else — including all ordinary tools — resolves through the table as our
    implementation. Both the engine (fill resolution) and the adapter
    (capability construction) call this, so they can never disagree:
    """
    native: list[str] = []
    fills: list[str] = []
    for name in names:
        if name in DUAL_IMPLEMENTATION_TOOLS and getattr(capabilities, name, False):
            native.append(name)
        else:
            fills.append(name)
    return native, fills
