# Phase 2 — Engine Facade: Full Development Spec

Status: spec for review (July 2026). Companion to `README.md` (D1–D30) and
`PHASE1.md` (transcript store, built). This is the phase where "send a request
to AI" works end-to-end through OUR interface.

## Goals / non-goals

**Goals**: our `AgentSpec` / run API / event schema; Pydantic AI hidden behind
one adapter; renderer with two-lane translation and conditional summarization;
every run persisted to the transcript; usage captured per item; provider
construction with D28 fallback; a minimal streaming HTTP endpoint proving the
async story (D29).

**Non-goals (later phases)**: registry sync feeds (P3 — a static seed stands in),
budget *enforcement* (P4 — usage is captured now, enforced later), RAG tool
exposure & native-tool fills (P5), realtime voice / sandbox (P6).

## The facade rule, restated for P2 (D3)

`pydantic_ai` may be imported ONLY inside `engine/adapter.py` and
`engine/providers.py`. Everything else — types, renderer, endpoints, tests
above the adapter — sees only our types. CI-able check: grep for
`pydantic_ai` outside those two files fails the build.

## Package layout (additions)

```
generative_ai_service/
  engine/
    __init__.py        # public: run_agent / stream_agent + facade types
    types.py           # AgentSpec, RunDeps, RunOptions, RunEvent union, RunResult
    model_info.py      # ModelInfo + static seed lookup (P3 replaces the backend, keeps the interface)
    providers.py       # [pydantic_ai allowed] registry-driven Model construction + D28 fallback
    renderer.py        # transcript path -> per-model context (two-lane + conditional summarization)
    adapter.py         # [pydantic_ai allowed] AgentSpec -> Agent; run; their events -> ours; outputs -> Items
    engine.py          # orchestration: append user msg -> render -> run -> append outputs -> yield events
  routers.py           # thin platform router: create conversation, SSE run endpoint
  tests/ (extended)
```

Legacy modules (`agents/`, `memory/`, `llms.py`, `types.py`, `utils/`) are
**deleted** in this phase — the salvage notes in README carry their ideas.

## Facade types (`engine/types.py`) — normative interfaces

```python
class AgentSpec(BaseModel):
    """OUR persisted agent definition (D13/D19) — never Pydantic AI's YAML format."""
    name: str
    model: str                          # e.g. "openai:gpt-5.2" (our canonical id = provider:model)
    fallback_models: list[str] = []     # D28 runtime fallbacks, in order
    instructions: str                   # static instructions (D9: never system_prompt)
    output_schema: dict | None = None   # JSON schema for structured output (StructuredDict path)
    tools: list[str] = []               # names resolved from the engine's tool table
    features: AgentFeatures = AgentFeatures()   # per-agent toggles (req. 8)
    limits: AgentLimits = AgentLimits()         # per-run guardrails (D23)
    model_settings: dict = {}           # temperature etc.; provider-scoped keys allowed


class AgentFeatures(BaseModel):
    """Per-agent feature toggles — the req.-8 surface. All default ON except opt-ins."""
    summarization: bool = True          # False = fail loudly when context overflows
    rag_search: bool = True             # expose search_conversation tool (P5 wires it)
    rag_scope_all_branches: bool = False  # D30 opt-in
    thinking: bool = False              # provider-adaptive thinking capability
    web_search: bool = False


class AgentLimits(BaseModel):
    """Per-run circuit breakers, mapped onto Pydantic AI UsageLimits by the adapter."""
    max_requests: int = 10              # model round-trips per run (tool-loop bound)
    max_tool_calls: int | None = 20
    max_output_tokens: int | None = None
    tool_retries: int = 1
    output_retries: int = 1


@dataclass
class RunDeps:
    """The D22 platform deps container (trust boundary, D11)."""
    db: Session                          # sync session; adapter hops via to_thread (D29)
    owner_id: str
    conversation_id: UUID
    extra: Any = None                    # product extension slot


# Our event schema (O5 resolved by this spec) — discriminated on `type`:
class RunStarted(BaseModel):    type: Literal["run_started"];    run_id: UUID; agent: str; model: str
class TextDelta(BaseModel):     type: Literal["text_delta"];     run_id: UUID; delta: str
class ThinkingDelta(BaseModel): type: Literal["thinking_delta"]; run_id: UUID; delta: str
class ToolCallStarted(BaseModel):  type: Literal["tool_call_started"];  run_id: UUID; tool_name: str; tool_call_id: str; args: dict | None
class ToolCallFinished(BaseModel): type: Literal["tool_call_finished"]; run_id: UUID; tool_call_id: str; result_preview: str
class ModelSwitched(BaseModel): type: Literal["model_switched"]; run_id: UUID; from_model: str; to_model: str; reason: str   # D28 fallback surfaced, never silent
class RunCompleted(BaseModel):  type: Literal["run_completed"];  run_id: UUID; output: Any; usage: Usage; item_ids: list[UUID]
class RunFailed(BaseModel):     type: Literal["run_failed"];     run_id: UUID; error: str

RunEvent = Annotated[Union[...all of the above...], Field(discriminator="type")]
```

Naming rationale: events describe conversation-level happenings (our domain),
not SDK part-indexes. AG-UI / Vercel-stream adapters map from these in a later
phase; the UI never sees Pydantic AI event classes.

## Engine API (`engine/engine.py`)

```python
async def stream_agent(spec: AgentSpec, deps: RunDeps, user_input: UserInput) -> AsyncIterator[RunEvent]:
    """THE single run path (D10). Non-streaming callers collect to RunCompleted."""

async def run_agent(spec: AgentSpec, deps: RunDeps, user_input: UserInput) -> RunResult:
    """Convenience wrapper: consumes stream_agent, returns the final result."""
```

Flow per run: append the user message item (to_thread) → renderer builds
(instructions_extra, message_history) for spec.model → adapter runs Pydantic AI
`run_stream_events()` → events translated as they arrive → on completion, all
new items (assistant message, tool calls/results, thinking) appended with
`usage` + `raw` populated → `RunCompleted` carries the new item ids.
Client disconnect cancels the async generator → adapter cancels the run (D29).

## Model construction (`engine/providers.py`)

- Canonical model id stays `provider:model` (matches Pydantic AI strings, but
  OURS is the stored contract).
- Keys read from settings and passed explicitly (D17); `openai:` → Responses
  API (D18).
- **D28 at construction**: provider key missing → build the same model via
  OpenRouter instead, emit `ModelSwitched` at run start.
- **D28 at runtime**: wrap in `FallbackModel(direct, openrouter, *spec.fallback_models)`;
  fallback trigger logged to Sentry + surfaced as `ModelSwitched`.

## Model info seed (`engine/model_info.py`)

P3 will sync registries; P2 needs windows/tokenizer NOW. Interface + static
seed for models we actually use (the big four + OpenRouter equivalents):

```python
class ModelInfo(BaseModel):
    id: str; provider: str; context_window: int; max_output_tokens: int
    tokenizer_family: str               # summary-cache key component (D24)
    supports_structured_output: bool = True

def get_model_info(model_id: str) -> ModelInfo: ...   # P3 swaps the backend, not the signature
```

Token estimation for window math: `len(text)//4 + fixed per-item overhead`,
cached in `Item.token_estimates[tokenizer_family]`; provider-reported usage
corrects drift over time. Deliberately rough — the thresholds have wide
hysteresis precisely so estimation error is absorbed (O6.8).

## Renderer (`engine/renderer.py`)

1. Load active path (`get_current_path`, to_thread).
2. Estimate path tokens for the target model's tokenizer family.
3. Fits under `target`? → full history (req. 6: large-context models are never
   summarized, even if a smaller model summarized earlier — cache rows are
   per-budget, the transcript is untouched).
4. Else: conditional summarization per O6 — snap boundary to turn seams
   (O6.3, DECIDED), check summary cache (quantized budgets, O6.6), summarize
   with the configured cheap model (O6.1) using the structured content spec
   (O6.4), `put_summary`, render as: summary block + verbatim tail.
   Failure → truncate + Sentry, never inject errors (O6.7, DECIDED).
5. Two-lane translation (D24): same-provider items with `raw` replay via the
   fast lane; everything else builds from universal fields.
6. `features.summarization=False` → raise a typed overflow error instead.

Summary injection form (decided by this spec): a clearly-delimited block at
the TOP of `message_history` as a user-role context preamble — not
instructions (instructions are the agent's identity, not conversation state)
and not a fake system message mid-history.

## Adapter (`engine/adapter.py`)

- Builds the Pydantic AI `Agent` from `AgentSpec` programmatically (D19):
  explicit Model from providers.py, `instructions=`, tools resolved from the
  engine tool table, `output_type` from `output_schema` via the mode chosen by
  model info (D20, ToolOutput default), `UsageLimits` from `AgentLimits`,
  capability list from `features` (Thinking, WebSearch when enabled).
- Agents cached per spec-hash (stateless singletons, D19).
- Event translation table (theirs → ours) lives here and nowhere else.
- Output items built from the run's new messages; `raw` = their serialized
  message; `usage` from RunUsage; `model_used` = the model that actually
  served (post-fallback).

## HTTP surface (`routers.py`, minimal on purpose)

- `POST /ai/conversations` → create (owner from auth).
- `POST /ai/conversations/{id}/runs` → SSE stream of RunEvents (async def,
  StreamingResponse; disconnect cancels — the D29 proof).
- Wired via existing DI (`db_dependency`, `user_dependency` → `RunDeps`).
- Agent specs for QUANTSOC live as platform defaults for now (one general
  assistant spec); product-specific agents come via `product/`.

## Dependencies

`pydantic-ai-slim[openai,anthropic,google,xai]` + the OpenRouter extra —
**pinned exact** (`==2.x.y`); upgrades are deliberate PRs (3-month breaking
window, D3). Note: requires pydantic upgrade compatibility check against the
backend's pinned pydantic 2.10.6.

## Tests (all offline; TestModel/FunctionModel)

- Adapter: event translation completeness (every their-event maps or is
  consciously dropped), output items round-trip with usage/raw/model_used.
- Renderer: full-history vs summarized paths on fake ModelInfo windows; turn-
  seam snapping; cache hit/miss; failure→truncation; two-lane selection.
- Providers: key-missing → OpenRouter construction; FallbackModel wiring.
- Engine: user msg + assistant msg persisted after a TestModel run; cancel
  mid-stream leaves transcript consistent (interrupted state).
- Endpoint: SSE happy path via httpx ASGI client.
- One flag-gated live smoke test per provider (skipped by default/CI).

## Build order within P2

1. `model_info.py` seed + `providers.py` (+ tests) — models constructible.
2. `types.py` facade — events/spec/deps.
3. `adapter.py` + `engine.py` minimal text run — **"send a request to AI" works**.
4. `renderer.py` two-lane + conditional summarization (+ O6 defaults confirmed here).
5. Tools + structured output modes.
6. `routers.py` SSE + QUANTSOC DI wiring.
7. Legacy module deletion + docs/status updates.

## Adversarial review (July 2026) — fixes applied

A 6-dimension adversarial review (each finding verified by an independent
skeptic) confirmed 10 defects; all fixed with regression tests
(`tests/test_review_fixes.py`):

- **[HIGH] Orphan tool_call** — a tool retry (`RetryPromptPart`) was dropped, so
  a failed tool_call had no result and every later same-provider turn hit a
  provider 400. Fix: tool-scoped retries now persist as `tool_result` items.
- **[HIGH] Connection pinned during stream** — the sync Session held its pooled
  connection idle-in-transaction for the whole generation. Fix: release
  (`rollback`) after rendering, before the stream.
- **[MEDIUM] Non-atomic output persistence** — items committed one-per-row, so
  an interruption could leave a tool_call without its result. Fix: `append_items`
  persists a run's outputs in one transaction.
- **[MEDIUM] Summary cache never hit** — read key (`path[-2]`) ≠ write key
  (`head[-1]`), so the summarizer re-ran every render. Fix: cache_get keyed on
  the renderer's `span_end_item_id`.
- **[MEDIUM] Providers** — bare `provider` id built an empty model (now raises);
  a keyless spec fallback crashed with a raw SDK error (now skipped).
- **[LOW] Usage accuracy** — run total re-summed per-item (dropped native-tool
  responses; missed cache/reasoning tokens). Fix: use pydantic_ai's
  authoritative `RunUsage`.
- **[LOW] seq determinism** — `full_history` now orders by `(seq, created_at, id)`
  so a rare duplicate seq is still deterministic.

## Done criteria

A QUANTSOC user can stream a multi-turn conversation with any configured
model, switch models mid-conversation, see thinking/tool events, and every
turn is in the transcript with usage + raw; renderer summarizes only when the
target model needs it; all offline tests green; no `pydantic_ai` import
outside the two sanctioned files; legacy modules gone.
