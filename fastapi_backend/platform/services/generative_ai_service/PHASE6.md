# Phase 6 — Realtime Voice Port + Sandbox Runtime: Full Development Spec

Status: BUILT (July 2026). Companion to `README.md` (D1–D40).
The two provider-specialty surfaces D15 planned for: voice-to-voice
(requirement 10 completes) and containerized agent execution (requirement 11).
Nothing in Pydantic AI covers either — these are OUR ports with thin provider
adapters behind them, exactly the D1 layer-3 shape.

## Goals / non-goals

**Goals**:
- A **voice port**: one `VoiceSession` interface serving both provider-native
  realtime (OpenAI Realtime, Gemini Live) and an STT→agent→TTS pipeline
  fallback for every other model — same events, same transcript, same billing.
- Voice sessions live in the SAME conversation transcript as text turns (the
  P1 `event` item kind + transcribed `message` items) — mid-conversation
  switching between text and voice, with RAG/summarization/cost intact.
- A **sandbox port**: isolated containerized execution for model-generated
  code, closing the D34 `code_execution` fill gap (P5 left it loudly
  unimplemented) and giving agents a workspace that adheres to the open specs
  (AGENTS.md rules, agentskills.io skills, MCP — D14, requirement 11).
- **Image generation** wired as a capability per registry flags (the last
  P5/P6-tagged coverage row), outputs persisted as `MediaPart` items.
- Voice + sandbox usage metered into the P4 ledger (`kind="voice"`, audio
  token rates already in the registry schema).

**Non-goals (explicitly out of P6)**:
- Computer use / browser control — deferred to the post-P6 gap review (the
  sandbox port is designed so a Claude-Agent-SDK or Cursor-SDK backend can
  implement it later without a new abstraction; O12 stays open there).
- A voice UI — P6 delivers the backend WebSocket surface; the frontend
  consumer is product work.
- HITL tool approval, moderation, durable execution, batch — post-P6 list.
- Video generation; cross-conversation RAG.

## Design decisions (proposed here, confirmed by building)

**D36 — Voice is a PORT beside the engine, not a run mode inside it.** A
`voice/` package owns `VoiceSession` + our `VoiceEvent` schema. Realtime
providers manage their own conversation state server-side during a session —
forcing that through the engine's render-then-run loop would fight both
designs. Instead the port BOOKENDS the engine's world: it renders context in
(instructions + recent transcript on connect) and persists results out
(transcribed turns as ordinary `message` items, session lifecycle as `event`
items). Text and voice therefore share one conversation, and every later text
run sees what was said.

**D37 — Backend WebSocket relay first; registry decides native vs pipeline.**
The client speaks ONE WebSocket to us (`/ai/conversations/{id}/voice`); we
relay to the provider. Rationale: provider keys never reach the client, usage
events are metered server-side into the ledger, and transcripts persist even
if the client dies. The same D34 logic picks the implementation: model has
`capabilities.realtime` (new flag, override-set) → native adapter; otherwise
→ the STT→`stream_agent`→TTS pipeline under the same `VoiceSession`
interface — every model is voice-capable from day one, degraded gracefully to
turn-based latency. Ephemeral-token direct WebRTC is a recorded later
optimization, not P6.

**D38 — Sandbox is a port with pluggable backends; dormant without config.**
`SandboxBackend.start(workspace) -> SandboxSession` (exec/put_file/get_file/
close). First backend: **Docker** (dev machines and any docker-capable host).
Render's runtime has no Docker daemon, so in prod the port is DORMANT until a
backend is configured (`AI_SANDBOX_BACKEND` unset → the `code_execution` fill
raises its typed error; config-minimalism fail-closed, nothing half-works). A
hosted-execution adapter (E2B/Modal/Fly Machines — new org-services row when
chosen) is the designated prod backend, added when a product needs it. The
workspace materializes AGENTS.md, agentskills.io skill folders, and MCP
config before the agent runs (D14 adherence — requirement 11).

**D39 — Provider-SDK quarantine extends the facade rule to voice.** The voice
adapters (`voice/adapters/*.py`) are the ONLY modules that may import
provider realtime SDKs / speak provider wire formats, exactly as adapter.py/
providers.py are for `pydantic_ai`. `test_facade_boundary` grows a matching
grep: `voice/types.py`, `voice/port.py`, the engine, and the router never see
an SDK type; everything crossing the port is OUR `VoiceEvent`.

**D40 — Image generation is a D34 native capability with no fill.** Enabled
per registry flag (`capabilities.image_generation`, override-set) through the
SDK's `ImageGeneration` capability; generated images persist as
`MediaPart(media="image")` on the assistant item and surface as a new
`MediaEvent` run event. No client-side fill exists (nothing sane to fill
with) — a spec requesting it on a non-capable model fails loudly, like
`code_execution` did in P5.

## Module layout (additions)

```
generative_ai_service/
  voice/
    __init__.py        # public surface: build_voice_session, VoiceEvent types
    types.py           # VoiceConfig + VoiceEvent union (OURS — no SDK imports)
    port.py            # VoiceSession ABC + build_voice_session (D37 selection)
    transcriptor.py    # session -> transcript items (message + event kinds)
    adapters/
      openai_realtime.py  # sanctioned SDK seam (WS relay to OpenAI Realtime)
      gemini_live.py      # sanctioned SDK seam (WS relay to Gemini Live)
      pipeline.py         # STT -> engine.stream_agent -> TTS fallback (D37)
  sandbox/
    __init__.py
    types.py           # WorkspaceSpec, ExecResult, SandboxLimits
    port.py            # SandboxBackend protocol + SandboxSession ABC
    workspace.py       # AGENTS.md / skills / MCP materialization (D14)
    docker_backend.py  # first backend (dev / docker-capable hosts)
    code_tool.py       # registers the `code_execution` fill into the P5 table
  routers.py           # + WS /ai/conversations/{id}/voice
  registry/schema.py   # + capabilities.realtime, capabilities.image_generation
  billing/             # (unchanged) — voice/audio spend rows, kind="voice"
```

## Interfaces (normative)

```python
# voice/types.py — everything ours, mirroring the P2 event philosophy:
class VoiceConfig(BaseModel):
    spec: AgentSpec                    # Same agent definition drives voice runs
    voice: str = "default"             # Provider-mapped voice name
    input_sample_rate: int = 16_000    # PCM16 in; adapters transcode as needed
    interruptions: bool = True         # Barge-in (native adapters only)

class VoiceSessionStarted(BaseModel): session_id; conversation_id; implementation: Literal["native", "pipeline"]; model
class AudioDelta(BaseModel):          session_id; audio_b64: str            # Outbound audio to play
class InputTranscript(BaseModel):     session_id; text: str; final: bool    # What the user said
class OutputTranscript(BaseModel):    session_id; text: str; final: bool    # What the agent said
class VoiceUsage(BaseModel):          session_id; usage: Usage              # Audio+text tokens (periodic + final)
class VoiceSessionEnded(BaseModel):   session_id; reason: str; item_ids: list[UUID]
class VoiceFailed(BaseModel):         session_id; error: str
VoiceEvent = Annotated[Union[...], Field(discriminator="type")]

# voice/port.py:
class VoiceSession(ABC):
    async def send_audio(self, audio_b64: str) -> None: ...   # Caller mic chunks
    async def end_turn(self) -> None: ...                     # Explicit turn commit (pipeline mode)
    def events(self) -> AsyncIterator[VoiceEvent]: ...        # THE output stream
    async def close(self) -> None: ...

async def build_voice_session(config: VoiceConfig, deps: RunDeps) -> VoiceSession:
    """D37 selection: capabilities.realtime -> native adapter; else pipeline.
    Renders recent context in; wires transcriptor + billing callbacks."""

# sandbox/types.py + port.py:
class SandboxLimits(BaseModel):
    max_seconds: int = 60; max_memory_mb: int = 512
    network: Literal["none", "allowlist"] = "none"; allowed_hosts: list[str] = []

@dataclass
class ExecResult:
    exit_code: int; stdout: str; stderr: str; duration_ms: int; truncated: bool

class SandboxSession(ABC):
    async def exec(self, command: list[str], timeout_s: int | None = None) -> ExecResult: ...
    async def put_file(self, path: str, content: bytes) -> None: ...
    async def get_file(self, path: str) -> bytes: ...
    async def close(self) -> None: ...

class SandboxBackend(Protocol):
    async def start(self, workspace: WorkspaceSpec, limits: SandboxLimits) -> SandboxSession: ...

# sandbox/workspace.py — requirement 11 / D14:
class WorkspaceSpec(BaseModel):
    files: dict[str, bytes] = {}       # Seed files (uploads to process, etc.)
    agents_md: str | None = None       # Rules the agent must adhere to
    skills: list[SkillRef] = []        # agentskills.io folders to materialize
    mcp_servers: list[str] = []        # MCP config written for in-sandbox agents

# sandbox/code_tool.py — closing the P5 gap (registers via register_tool):
async def code_execution(ctx: ToolContext, code: str, language: str = "python") -> dict:
    """Our D34 fill: runs model-generated code in the configured sandbox
    backend; returns {exit_code, stdout, stderr, duration_ms}. Raises the
    typed ToolExecutionError when no backend is configured (dormant, D38)."""
```

## Flows

**Voice (native adapter)**: WS connect (auth = the same user dependency) →
`build_voice_session` renders instructions + a bounded recent-transcript
context into the provider session config → relay loop (client audio up,
`VoiceEvent`s down) → on transcript events, `transcriptor` appends message
items (`meta.modality="voice"`) → on usage events + close, ledger row
`kind="voice"` priced at the registry's audio/text rates → `VoiceSessionEnded`
carries the appended item ids.

**Voice (pipeline fallback)**: buffered turn audio → STT model → the ordinary
`engine.stream_agent` run (ALL P2–P5 machinery applies: budgets, tools, RAG,
summarization) → TTS on the final text → `AudioDelta`s down. Same events,
same persistence; higher latency, universal coverage.

**Sandbox**: agent's `code_execution` tool call → backend from
`AI_SANDBOX_BACKEND` → workspace materialized (D14 artifacts) → exec under
`SandboxLimits` → ExecResult back as the tool result (untrusted model input,
as ever) → session closed; nothing persists on the host.

## Security notes (both surfaces are hard boundaries)

- **Sandbox executes hostile input by definition** (model-generated code):
  no network by default (explicit allowlist opt-in), non-root user, CPU/
  memory/time/disk caps, no host mounts, output size caps (a bomb of stdout
  must not flood the context), secrets NEVER injected unless the workspace
  spec grants a named one explicitly.
- **Voice relay**: provider keys stay server-side (D37); per-message audio
  size caps and a session duration cap (budget pre-flight runs at connect,
  and `VoiceUsage` events re-check mid-session so a long call cannot outrun
  an enforced budget).
- Voice transcripts are ordinary untrusted user input; sandbox stdout is
  ordinary untrusted tool output — both flow through the existing item model.

## Tests (offline; adapters faked, Docker integration env-gated)

- Port contract suite run against BOTH voice implementations (a scripted fake
  native adapter + the pipeline with TestModel and fake STT/TTS): same event
  sequences, same transcript items, same ledger rows.
- Transcriptor: voice turns appear as message items with `meta.modality`;
  a following TEXT run sees them in its rendered history (the cross-modality
  acceptance test).
- D37 selection goldens: `capabilities.realtime` → native; absent → pipeline.
- Billing: audio token usage → `kind="voice"` rows; pipeline mode bills STT/
  TTS/agent models separately (existing `record_spend` kinds).
- Sandbox: FakeBackend contract tests (exec/put/get/limits/close); workspace
  materialization writes AGENTS.md/skills/MCP config; `code_execution`
  dormant-without-config raises the typed error; Docker backend integration
  test skipped unless a daemon is present (never in CI).
- Facade: extended grep — no SDK imports outside the sanctioned seams (D39).

## Build order

1. `voice/types.py` + port + scripted fake adapter + WS endpoint (offline e2e).
2. Pipeline fallback (STT→engine→TTS) + transcriptor + billing — every model
   becomes voice-capable here; the cross-modality acceptance test passes.
3. OpenAI Realtime adapter (relay), usage metering; `capabilities.realtime`
   overrides for the Realtime-capable models.
4. Gemini Live adapter (second native proof — the port is real).
5. Sandbox port + workspace materialization + FakeBackend + `code_execution`
   fill registration (P5 gap closes).
6. Docker backend + limits enforcement (+ env-gated integration test).
7. Image generation capability (D40) + FEATURE_COVERAGE ticks + docs.

## Done criteria

A conversation can alternate text and voice turns with both visible in one
transcript and priced in one ledger; a model WITHOUT native realtime still
holds a voice conversation through the pipeline under the same WS contract;
an agent's `code_execution` call runs in an isolated container with AGENTS.md
and skills materialized, and is a loud typed error when no backend is
configured; a registry-flagged model generates images persisted as media
items; no SDK type crosses any port boundary (facade greps green).

## Owner steps

None to start building: OpenAI + Google keys already provisioned (voice
adapters), Docker only needed locally for the backend integration test. A
hosted execution provider (E2B/Modal) is a LATER decision — it adds an
org-services ledger row + one key when a product actually needs prod
sandboxing.
