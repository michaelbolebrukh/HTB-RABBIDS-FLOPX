"""
Engine facade types — OUR interface (Phase 2).

These are the ONLY types callers above the engine see. No `pydantic_ai` import
here (D3): specs, options, deps, events, and results are all ours, so a future
framework swap changes only adapter.py/providers.py, never this contract or the
code built on it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal, Union
from uuid import UUID

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..transcript.types import Usage


# ============================================================================
# AGENT SPEC — our persisted agent definition (D13/D19)
# ============================================================================

# The shared default memory namespace (D59): every agent whose spec leaves
# `memory_scope` unset lands here, so a product's agents share one memory pool
# per user out of the box. Isolation is opt-in (set a distinct scope):
DEFAULT_MEMORY_SCOPE = "default"


class AgentFeatures(BaseModel):
    """
    Per-agent feature toggles — the requirement-8 surface. Everything the
    engine can turn on or off for a single agent lives here, defaulting to a
    safe general-assistant profile:
    """
    summarization: bool = True            # False = raise on context overflow instead of compacting
    rag_search: bool = True               # Expose the search_conversation tool (P5 wires the implementation)
    # P10: durable, cross-conversation, agent-authored memory. On = the agent
    # gets the memory_write/search/supersede/delete tools and an always-on
    # usage preamble; writes reconcile bi-temporally (supersede vs accrue).
    # Off by default — a fresh clone opts an agent in deliberately:
    memory: bool = False
    rag_scope_all_branches: bool = False  # D30 opt-in: let RAG see edited-away branches
    thinking: bool = False                # Provider-adaptive thinking/reasoning
    thinking_effort: Literal["low", "medium", "high"] = "medium"
    web_search: bool = False              # Provider-native web search where available
    # D41 (supersedes D40's no-fill clause): image generation rides the model
    # natively when the registry flags it, otherwise OUR generate_image fill
    # (backed by the spec's pickable image model) supplies it:
    image_generation: bool = False
    # D41: speak the run's output text through the spec's TTS model. Off by
    # default (owner decision, July 2026) — textual UIs use the read-aloud
    # endpoint instead; this toggle is for voice-first agents:
    speech_output: bool = False


# The platform-default speciality models (P7, D42): every one runs on a key
# the factory already provisions (OpenAI/Google), so a fresh clone speaks and
# draws day one. Native realtime voice has no default here — the voice port
# selects it from the agent's own model (capabilities.realtime); these cover
# the pipeline legs and the D41 fills:
DEFAULT_TTS_MODEL = "openai:gpt-4o-mini-tts"
DEFAULT_STT_MODEL = "openai:gpt-4o-mini-transcribe"
DEFAULT_IMAGE_MODEL = "google:gemini-3.1-flash-image"


class MediaModels(BaseModel):
    """
    Per-agent speciality-model choices (D42) — picked exactly the way the chat
    model is picked: canonical `provider:model` ids, None = platform default.
    D45: None also means "prefer the chat model's NATIVE capability when the
    registry flags it"; an explicit id FORCES our path with that model even on
    a natively capable chat model (pair a weak-image chat model with the best
    image model, or a realtime model with a picked ElevenLabs voice).
    Voices are provider-scoped strings passed through verbatim (OpenAI's 13
    names, Google's 30, ElevenLabs voice/clone ids) — the platform never
    maintains a voice list of its own:
    """
    tts_model: str | None = None
    tts_voice: str = "default"
    stt_model: str | None = None
    image_model: str | None = None

    # Only None (genuinely unset) applies the platform default — an explicit
    # empty/malformed string is a deliberate pick and must reach the port's
    # validator to fail loudly, never be swapped silently for the default (the
    # `or` idiom would collapse "" to the default and hide the misconfig):
    def resolved_tts(self) -> str:
        """Returning the effective TTS model id (default applied only when unset):"""
        return DEFAULT_TTS_MODEL if self.tts_model is None else self.tts_model

    def resolved_stt(self) -> str:
        """Returning the effective STT model id (default applied only when unset):"""
        return DEFAULT_STT_MODEL if self.stt_model is None else self.stt_model

    def resolved_image(self) -> str:
        """Returning the effective image model id (default applied only when unset):"""
        return DEFAULT_IMAGE_MODEL if self.image_model is None else self.image_model


class AgentLimits(BaseModel):
    """
    Per-run circuit breakers (D23). The adapter maps these onto Pydantic AI's
    UsageLimits; the cross-run dollar budget is a separate concern (P4):
    """
    max_requests: int = 10                # Model round-trips per run (bounds tool loops)
    max_tool_calls: int | None = 20       # Successful tool executions per run
    max_output_tokens: int | None = None  # Hard output cap per run
    tool_retries: int = 1
    output_retries: int = 1


class AgentSpec(BaseModel):
    """
    OUR agent definition — persisted in our schema, never Pydantic AI's YAML
    format (D13/D19). The engine constructs a Pydantic AI Agent from this at
    run time; product agents are just data.
    """
    name: str
    model: str                             # Canonical "provider:model" (D17)
    fallback_models: list[str] = Field(default_factory=list)  # D28 runtime fallbacks, in order
    instructions: str = ""                 # Static instructions — never system_prompt (D9)
    output_schema: dict | None = None      # JSON schema for structured output (D20); None = free text
    tools: list[str] = Field(default_factory=list)  # Tool NAMES resolved from the tool table (P5, D33)
    # HITL approval scope (P8, D46): tool NAMES whose every invocation pauses
    # the run for an explicit decision. Applies ONLY to tools WE execute — a
    # name that resolves provider-native is inert (native tools run inside the
    # provider and are never forced into the fill lane). Empty = fully dormant.
    # Per-invocation gating (only above a threshold, etc.) is a code-level
    # predicate registered with the tool, not spec data:
    requires_approval: list[str] = Field(default_factory=list)
    # MCP servers attached to this agent (P9, D52): catalog IDs (dev-registered,
    # credential-safe) — a raw "provider://host" URL is back-compat-accepted as
    # an anonymous no-auth remote server. Bridged into the tool table at run:
    mcp_servers: list[str] = Field(default_factory=list)
    # Skills attached to this agent (P9, D53): catalog IDs. L1 metadata is
    # always in context; the body loads via open_skill and scripts run in the
    # sandbox — progressive disclosure keeps the window lean:
    skills: list[str] = Field(default_factory=list)
    # Bring-your-own extension toggles (P9, D51): when on, this agent's users
    # may add their OWN MCP servers / skills (scoped to owner_id, no allowlist).
    # The developer of the user-facing product controls these; default off:
    allow_user_mcp: bool = False
    allow_user_skills: bool = False
    features: AgentFeatures = Field(default_factory=AgentFeatures)
    limits: AgentLimits = Field(default_factory=AgentLimits)
    media: MediaModels = Field(default_factory=MediaModels)  # Speciality models (P7, D42)
    model_settings: dict = Field(default_factory=dict)  # temperature etc.; provider-scoped keys allowed


# ============================================================================
# RUN INPUTS
# ============================================================================

@dataclass
class RunDeps:
    """
    The D22 platform deps container and the D11 trust boundary: everything the
    model must NEVER control (db handle, the real owner/conversation identity)
    lives here, injected per run, invisible to the model.
    """
    db: Session                            # Sync session; adapter hops via to_thread (D29)
    owner_id: str
    conversation_id: UUID
    # Durable-memory partition (P10/D59), an OPAQUE app-supplied string alongside
    # owner_id — the service models no users, agents-as-entities, or how they
    # relate; the INTEGRATING APP decides the memory-sharing topology by choosing
    # what scope each run passes (same scope => shared, distinct => isolated).
    # None => a single neutral namespace (DEFAULT_MEMORY_SCOPE) as a zero-config
    # fallback, not an opinion. Always within owner_id — never crosses it (D54):
    memory_scope: str | None = None
    extra: Any = None                      # Product extension slot
    # Fresh-session factory for detached background work (D35 embedding
    # backfill) — the request's session must never be used after the response.
    # A host coupling injected by the router (D26); None disables background
    # embedding gracefully (search falls back to text matching):
    session_factory: Callable[[], Session] | None = None


class UserInput(BaseModel):
    """
    A single user turn's input. Text plus optional media references; the engine
    turns this into a transcript message item before the run:
    """
    text: str = ""
    # Media as (kind, url-or-data) — kept minimal for P2; richer in later phases:
    media: list[dict] = Field(default_factory=list)
    # Meta carried onto the persisted user item (P6: the voice pipeline tags
    # its transcribed turns {"modality": "voice"} here) — never model-facing:
    meta: dict = Field(default_factory=dict)


class ApprovalDecision(BaseModel):
    """
    One resolution for one paused tool call (P8, D47/D48). Denial is DATA, not
    an error: `reason` becomes the tool result the model reads, so the agent
    adapts instead of crashing. `override_args` lets the approver edit the
    call's arguments before execution ("approve, but only 10 rows"):
    """
    tool_call_id: str
    approved: bool
    reason: str | None = None              # Surfaced to the model on denial (D48)
    override_args: dict | None = None      # Approve with edited arguments


# ============================================================================
# EVENT SCHEMA — our run events (O5 resolved)
# ============================================================================
# Events describe conversation-level happenings (our domain), not SDK part
# indexes. AG-UI / Vercel-stream adapters map FROM these later; the UI never
# sees a Pydantic AI event class.

class RunStarted(BaseModel):
    type: Literal["run_started"] = "run_started"
    run_id: UUID
    agent: str
    model: str                             # The model that will actually serve (post construction-fallback)


class TextDelta(BaseModel):
    type: Literal["text_delta"] = "text_delta"
    run_id: UUID
    delta: str


class ThinkingDelta(BaseModel):
    type: Literal["thinking_delta"] = "thinking_delta"
    run_id: UUID
    delta: str


class ToolCallStarted(BaseModel):
    type: Literal["tool_call_started"] = "tool_call_started"
    run_id: UUID
    tool_name: str
    tool_call_id: str
    args: dict | None = None


class ToolCallFinished(BaseModel):
    type: Literal["tool_call_finished"] = "tool_call_finished"
    run_id: UUID
    tool_call_id: str
    result_preview: str                    # Truncated stringified result for UIs


class MediaGenerated(BaseModel):
    """
    The model produced non-text media (D40 image generation). The full bytes
    ride the event so a streaming UI can render immediately; the same content
    persists as a MediaPart on the run's transcript items:
    """
    type: Literal["media_generated"] = "media_generated"
    run_id: UUID
    media: Literal["image", "audio", "video", "file"] = "image"
    mime_type: str | None = None
    data_b64: str | None = None


class ModelSwitched(BaseModel):
    """Emitted when D28 fallback changes the serving model — never silent."""
    type: Literal["model_switched"] = "model_switched"
    run_id: UUID
    from_model: str
    to_model: str
    reason: str


class RunCompleted(BaseModel):
    type: Literal["run_completed"] = "run_completed"
    run_id: UUID
    output: Any                            # Final text or validated structured object
    usage: Usage
    item_ids: list[UUID]                   # Transcript items appended by this run
    # Cost surfacing (Phase 4) — None until the billing layer prices the run;
    # priced=False means the pessimistic unpriced-model policy was applied:
    cost_usd: float | None = None
    priced: bool = True


class RunFailed(BaseModel):
    type: Literal["run_failed"] = "run_failed"
    run_id: UUID
    error: str


class PendingApproval(BaseModel):
    """One tool call awaiting an explicit decision (P8): what, with which args."""
    tool_call_id: str
    tool_name: str
    args: dict | None = None


class RunPaused(BaseModel):
    """
    Terminal event of a paused run (P8, D47): the model called one or more
    approval-gated tools, the run persisted through the pending tool_call
    items (the transcript checkpoint) and ended cleanly — no worker blocks.
    The client resolves via the approvals endpoint, which resumes as an
    ordinary engine run over the checkpointed transcript:
    """
    type: Literal["run_paused"] = "run_paused"
    run_id: UUID
    approvals: list[PendingApproval]
    item_ids: list[UUID]                   # Transcript items the paused run persisted
    usage: Usage
    cost_usd: float | None = None          # Tokens spent up to the pause (already recorded)
    priced: bool = True


# Discriminated union — clients switch on `type`:
RunEvent = Annotated[
    Union[
        RunStarted, TextDelta, ThinkingDelta, ToolCallStarted, ToolCallFinished,
        MediaGenerated, ModelSwitched, RunCompleted, RunPaused, RunFailed,
    ],
    Field(discriminator="type"),
]


@dataclass
class RunResult:
    """The collected outcome of a run for non-streaming callers (D10 convenience path)."""
    run_id: UUID
    output: Any
    usage: Usage
    item_ids: list[UUID] = field(default_factory=list)
    model_served: str = ""
    # Non-empty when the run PAUSED for tool approval instead of completing
    # (P8): the caller resolves via resume_agent / the approvals endpoint:
    pending_approvals: list[PendingApproval] = field(default_factory=list)


# ============================================================================
# ENGINE ERRORS
# ============================================================================

class EngineError(Exception):
    """Base for engine-layer errors (kept framework-free like the transcript layer)."""


class BudgetExceededError(EngineError):
    """
    Raised at pre-flight when a budget window has no remaining allowance
    (Phase 4, D7). Expected, user-facing behavior — the router maps it to a
    429 with the binding budget named; it is NEVER a Sentry error:
    """

    def __init__(self, binding_budget: str | None):
        super().__init__("You've reached your AI usage limit for now. Try again later.")
        self.binding_budget = binding_budget


class BudgetCheckUnavailableError(EngineError):
    """
    Raised when the budget check itself fails while enforcement is on —
    fail-CLOSED (PHASE4): if we cannot verify budget, we do not spend. The
    router maps it to a 503; the underlying failure IS Sentry-worthy:
    """


class ContextOverflowError(EngineError):
    """
    Raised when a run's context exceeds the model window AND the agent has
    summarization disabled (features.summarization=False) — a deliberate,
    typed signal rather than a silent truncation:
    """


class ToolNotRegisteredError(EngineError):
    """
    Raised at agent construction when a spec names a tool the table does not
    hold (D33) — a spec is data and must never run with fewer tools than it
    declared, so the failure is loud and immediate, never a degraded run:
    """


class ToolAlreadyRegisteredError(EngineError):
    """
    Raised when code registers two implementations under one name — silent
    shadowing between platform and product tools is exactly what the
    code-registration rule (D33) exists to prevent:
    """


class CapabilityUnavailableError(EngineError):
    """
    Raised at agent construction when a spec demands a native-only capability
    (D40 image generation) on a model whose registry entry lacks it — there is
    no fill to degrade to, so the failure is loud and immediate:
    """


class ApprovalResolutionError(EngineError):
    """
    Raised when approval decisions cannot be applied (P8): nothing is pending,
    a decision names an unknown/already-resolved call, decisions do not cover
    every pending call, or the conversation is not the caller's. Expected,
    user-facing (the router maps it to a 409) — never a Sentry error:
    """


class ToolExecutionError(EngineError):
    """
    A tool failing in an EXPECTED way (blocked URL, oversized page, upstream
    HTTP error). The adapter translates this into the SDK's retry mechanism so
    the MESSAGE reaches the model as the tool result — the model can react —
    instead of the whole run failing. Unexpected exceptions still fail loudly:
    """
