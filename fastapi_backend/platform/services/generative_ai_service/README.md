# Generative AI Service — Goal, Requirements & Decisions

Status: **re-specification in progress** (July 2026). The full research record lives in
`planning/AI_SDK_RESEARCH.md`; this README is the working summary for anyone (human or
agent) touching this scope.

> **Legacy modules deleted in P2 (July 2026).** The original `agents/`, `memory/`,
> `llms.py`, `types.py`, and `utils/` modules (OpenAI-only reference implementation)
> were removed once the engine replaced their role; their worthwhile ideas were
> carried forward (see "Legacy code review" below and the P1/P2 specs). The live
> package is now `transcript/` (P1), `engine/` (P2), `models.py`, `routers.py`.

## Goal

A sophisticated, provider-independent AI baseline — the platform layer every product
instance builds on. Single Python-object-level interface for models, tools, structured
output, and streaming across all major providers, with our own conversation, cost,
registry, and runtime layers on top. Frontier-capable: whatever the best provider
feature is, we reach it either through the unified layer or a thin custom adapter.

## Requirements

1. All major providers, with on-demand **mid-conversation model switching**.
2. **Provider-independent chat history** stored internally (our schema, our DB).
3. Per-model / per-provider **settings surfacing**.
4. **Token cost calculation and hard limit enforcement** (blocks before spend, not
   report-after).
5. Intelligently maintained **registry** of model capabilities / costs / settings,
   auto-updated (scheduled reconciler agent), never hand-maintained-only.
6. **Context-window-aware conditional summarization** that preserves the full
   source-of-truth transcript: small-context model → summarized view; large-context
   model → full history — even mid-conversation, even if summarization was previously
   triggered.
7. **RAG over the entire conversation**, including content that has been summarized away
   from a given model's view.
8. **Per-agent feature toggles** for everything above and below.
9. Single interface for response formats, tools, structured output — plain Python
   objects.
10. Frontier features across providers: thinking, streaming, file/web search, agentic
    browser control, image/video input + generation, voice-to-voice + realtime — custom
    thin adapters where the unified layer has gaps.
11. **Containerized agent execution**: cursor-like file/folder indexing, AGENTS.md /
    skills / rules adherence, file processing in-container, secret management.

## Decisions — FINALIZED

| # | Decision | Rationale (short) |
|---|---|---|
| D1 | **Three-layer split**: (1) model I/O = wrapped SDK; (2) conversation/memory/cost/registry = built by us; (3) agent runtime = assembled on open specs (MCP, AGENTS.md, Agent Skills). | No SDK covers layers 2–3 provider-neutrally; layer 2 is our moat. |
| D2 | **Pydantic AI is the model-I/O engine** (v2.x). | Only provider-neutral + agent-capable + lean + well-maintained Python option; day-one new-model support via string model names; migration-direction evidence (teams leave LangChain for it). |
| D3 | **Facade rule (absolute)**: nothing above the adapter module imports `pydantic_ai`. Our own `Agent`/`Run`/`Stream`/event types at the boundary. | Contains their 3-month breaking-change window to one module; their message format never becomes our persistence format. |
| D4 | **Canonical transcript = our own append-only, item-based store** (Open-Responses-aligned items: message, tool_call, tool_result, reasoning, media, summary-marker). Per-model context views are *rendered*, never destructive. Summaries are cache entries keyed by (item-range, token budget). | Flat message lists can't round-trip reasoning/provider items; rendering makes req. 6 fall out of the design. |
| D5 | **No Pydantic AI Gateway** (and no LiteLLM proxy / OpenRouter as sole path). BYOK direct per provider. | No middleman on the hot path; per-provider credits/discounts; gateway rejects unknown-pricing models (kills day-one models); its cost limits duplicate our layer. `FallbackModel` covers failover. |
| D6 | **Registry synced from three feeds** — models.dev JSON, LiteLLM `model_prices_and_context_window.json`, OpenRouter `/models` — merged into our schema; scheduled agent is a *reconciler* (diffs, PRs), not primary source; hand-override entries win. | Fresh without hand-maintenance; agent-as-source is unreliable. |
| D7 | **Cost meter + budgets are ours, in-process, transactional in Postgres** (per user/agent/conversation/day/month). Pydantic AI `UsageLimits` used as per-run guardrails only. Must count validation-retry tokens. | Hard enforcement can't live in observability tools or external proxies. |
| D8 | **Observability: Sentry** in production (has a named Pydantic AI integration + AI Agents dashboard; unifies frontend/backend/AI). `logfire` lib optional for local dev only. Neither is the cost-enforcement layer. | One platform across the factory; OTel-standard so backend is swappable. |
| D9 | **`instructions`, never `system_prompt`.** | System prompts persist in message history and are re-sent on later runs (even under other agents/models); instructions are re-evaluated per run and never ride along in replayed history — required for model/agent switching. |
| D10 | **Single run path: `run_stream_events()`**, translated into *our* event schema at the facade. Non-streaming callers consume the same stream. Avoids the `run_stream()` dangling-tool-call trap; set `end_strategy` explicitly anyway. | One code path feeds chat UI (thinking deltas, tool calls) and API alike; maps onto AG-UI / Vercel stream later. |
| D11 | **`deps` = trust boundary.** Model-should-know → instructions; model-should-choose → tool parameter; model-must-never-control (db session, user_id, secrets) → deps. Authorization by construction. | Model cannot act outside injected identity regardless of prompt. |
| D12 | **Structured-output mode chosen per model from the registry** (tool-calling default vs native vs prompted), not hardcoded. | Support varies per provider/model (`supports_response_schema` etc.). |
| D13 | **Our own persisted agent-spec schema** (YAML/JSON, per-agent feature toggles); engine *constructs* Pydantic AI agents from it. Pydantic's spec format is not our stored contract. | Facade rule applies to config too. |
| D14 | **Open standards**: MCP for tools, AGENTS.md for rules, agentskills.io Agent Skills format for skills (all Linux-Foundation/vendor-neutral). | All-provider-backed; portable across coding agents. |
| D15 | **Provider-specialty surfaces via adapters behind our ports**, not via switching foundation: realtime voice (OpenAI Realtime / Gemini Live + STT→agent→TTS fallback), computer use / container harness (option to embed Claude Agent SDK for that surface only), video gen. Native-tool gaps per provider filled by our client-side implementations chosen via registry flags. | These are permanent provider specialties, not neutral-framework lag. |
| D16 | **Pydantic AI coding skill vendored** at this scope (`.cursor/skills/building-pydantic-ai-agents/`, synced to `.claude`/`.codex` via `npm run agent:sync`). Snapshot v1.1.1 — refresh manually for now. | Agents editing this scope get framework-correct patterns. |

## Phases (hard build order — do not reorder without updating this file)

| Phase | Deliverable | Status |
|---|---|---|
| P1 | **Transcript store**: item schema, append-only persistence, embedding index, `search_conversation` tool | **built** (July 2026 — `transcript/`, `models.py`, migration `20260704_01`, 24 tests) |
| P2 | **Engine facade**: our `Agent`/`Run`/`Stream` types + our event schema; Pydantic AI adapter behind it (spec→Agent, transcript→`message_history`, events→our events, usage→meter) | **built** (July 2026 — `engine/`, `routers.py`, seq migration `20260705_01`, 32 P2 tests; pydantic 2.10→2.13, pydantic-ai 2.5.0) |
| P3 | **Registry**: schema, three-feed sync (models.dev / LiteLLM JSON / OpenRouter), overrides, query API | **built** (July 2026 — `registry/`, 636 models synced from live feeds, 10 tests; `model_info.py` is now a shim) |
| P4 | **Cost meter + budgets**: transactional Postgres budgets; pre-flight check derives per-run `UsageLimits` | **built** (July 2026 — `billing/`, migration `20260711_01`, 12 tests; fail-closed enforcement, pessimistic unpriced policy, `/ai/usage`) |
| P5 | **RAG tool + client-side native-tool fills** as our capabilities | **built** (July 2026 — `engine/tool_table.py`, `platform_tools.py`, adapter tool binding + embedder + D20 output-mode selection, D35 background embeddings, 21 P5 tests) |
| P6 | **Realtime voice port + sandbox runtime** (separate ports, nothing in Pydantic AI to reuse) | **built** (July 2026 — `voice/` port + OpenAI Realtime/Gemini Live adapters + STT→agent→TTS pipeline, WS `/ai/conversations/{id}/voice`, `sandbox/` port + Docker backend + `code_execution` fill, D40 image generation, 19 P6 tests) |
| P7 | **Output-modality supplementation**: every agent speaks + draws regardless of chat model — native per registry, speciality-model fills otherwise; TTS/STT/image models + voices pickable data; fal.ai/OpenRouter media long-tail lanes; unit-correct media billing | **built** (July 2026 — `media/` port + OpenAI/Google/ElevenLabs/fal/OpenRouter-audio adapters, `generate_image` fill, `speech_output` stage, `POST /ai/speech` read-aloud, pickable STT/TTS in the voice pipeline, D44 unit pricing, 8 P7 tests; market research in `MEDIA_PROVIDERS.md`) |
| P8 | **HITL tool approval** (pause/resume tool loop): approval-gated tools pause the run at a transcript checkpoint and resume as an ordinary run over the checkpointed transcript — approve / approve-with-edited-args / deny, scoped per-agent + per-tool (+ per-invocation predicate); only OUR tools are gateable (D46); the same checkpoint is the turn-granular durable-resume story | **built** (July 2026 — `AgentSpec.requires_approval` + `approval_predicate`, adapter `DeferredToolRequests` seam, engine `resume_agent` + `RunPaused`, `GET /ai/.../pending-approvals` + `POST /ai/.../runs/resume`, voice auto-deny, 15 P8 tests; PHASE8.md) |
| P9 | **Extensibility: MCP + Skills**: developers attach MCP servers + Skills to agents by id; when they enable a per-agent toggle, users bring their own (no allowlist, scoped per owner). MCP via the official `mcp` SDK bridged into the tool table; skills are containerized with progressive disclosure | **built** (July 2026 — `extensions/` package [mcp_client/mcp_catalog/skill_catalog/store], `engine/agent_registry.py`, `ai_user_extensions` table + migration `20260713_09`, `open_skill` tool, `AgentSpec.{mcp_servers,skills,allow_user_mcp,allow_user_skills}`, `GET/POST/DELETE /ai/agents/{id}/extensions`, 19 P9 tests; PHASE9.md, D50–D54) |
| P10 | **Durable agent-authored memory**: non-rotting, cross-conversation, bi-temporal fact store — the agent writes/searches/supersedes/deletes facts; writes reconcile (supersede vs accrue) instead of piling up; recall answers "what is true now" and "what was true at date T". Frontier-informed by literal code study (Graphiti/mem0/cognee/Letta/LangMem), benchmarked live and **beating mem0 23/23 vs 16/23** on the hard categories | **built** (July 2026 — `memory/` package [store/reconcile/tools], `ai_memories` table + migration `20260713_10`, `AgentFeatures.memory`, injected resolver/embedder through the facade, 15 P10 tests; MEMORY.md provenance, BENCHMARK.md head-to-head, D55–D58) |

Reaching "send a request to AI" happens inside P2, but P1 lands first so the
foundation never needs re-laying.

## Decisions — FINALIZED (from Pydantic AI design review, July 2026)

| # | Decision |
|---|---|
| D62 | **pgvector is the production recall path** (P10, owner decision July 2026): SQLite portability was never a requirement for the AI service — PostgreSQL is its production dialect, and anything below indexed ANN falls under the frontier (mem0→Qdrant/HNSW, Graphiti→Neo4j vector index, LangMem→pgvector). Recall's semantic leg runs on an **HNSW** index (`embedding_vec vector(1536)`, cosine) and the lexical leg on a **GIN full-text** index (`to_tsvector('simple', text)`, OR-joined tokens matching the Python overlap semantics), both sub-linear, fused by the same RRF + corroboration tiebreak as before — two engines, one ranking contract. Implementation is deliberately **raw SQL with a migration-owned column** (`20260713_13`: guarded `CREATE EXTENSION`, column, backfill from the JSONB embeddings, indexes; no ORM column, no pgvector pip dep) so `create_all()` never emits a `vector` type before the extension exists. Non-PG engines and non-1536 vectors degrade to the exact Python-cosine scan — a test/fallback path, not production; a missing extension logs and falls back rather than failing boot. The JSON `embedding` stays the source of truth; `embedding_vec` mirrors it on write. Index width follows `EMBEDDING_MODEL_ID` (text-embedding-3-small, 1536) — changing models to another width is a new migration. CI runs `pgvector/pgvector:pg16` + the `test_memory_pg.py` integration suite on the real path. |
| D61 | **Per-run session lock for concurrent tool calls** (P10): pydantic_ai may execute several tool calls from one model step concurrently, and a SQLAlchemy `Session` is not concurrency-safe — a real race (surfaced at ~0.8% under adversarial load). A per-run `asyncio.Lock` on `ToolContext` (`db_lock`, created fresh per run) guards every tool's DB hop, so the DB critical sections of memory tools + `search_conversation` serialize while the slow parts (network/LLM) still overlap. Chosen over per-tool-call fresh sessions (Approach A) because one small change covers ALL `ctx.db` tools with no connection-pool pressure or transaction-boundary change. `reconcile_write` takes the lock as an injected param and holds it only around each DB op, never across the embed/resolve calls. |
| D60 | **Two-phase write-completeness: real-time write + summary-time flush** (P10): `memory_write` (phase one) depends on the agent choosing to save a fact — adversarial testing showed it misses ~30% of terse asides. Phase two is a safety net: when a span is FRESHLY compacted out of context (summary cache miss), a detached best-effort task extracts that span's durable facts (a cheap-model extractor, `make_memory_extractor`) and reconciles them into memory before they are lost. Fires exactly once per span (gated on the cache miss), never on cache-hit re-renders; anything the agent already wrote dedups in `reconcile_write`, so the two phases never double-write. This IS the deferred background-reflection pass, triggered at compaction. Honest limit: extraction is LLM-bound + "important" is a judgment call, so it lifts completeness toward the frontier's high-but-imperfect operating point, not to 100%. |
| D59 | **Memory scope is an opaque, app-supplied partition string** (P10): durable memory is keyed `(owner_id, scope)`, and BOTH are per-run inputs the integrating app passes on `RunDeps` — the service models no users, no agents-as-entities, and no relationship between them (that is the app's concern, not ours; D26/D54, and the whole service graduates to its own thing carrying only these two strings). The app chooses the memory-sharing topology purely by which `scope` each run passes: same scope ⇒ shared, distinct ⇒ isolated — so any memory attaches to any conversation/agent by matching scope. `None` ⇒ a single neutral `DEFAULT_MEMORY_SCOPE` (`"default"`) as a zero-config fallback, NOT an opinion about how agents relate to users. Never crosses `owner_id` (D54). Replaces the earlier per-`agent_name` keying (column renamed `agent_name`→`scope`, migration `20260713_12`). |
| D58 | **Injected resolver + embedder keep memory SDK-free** (P10): the write-time LLM contradiction-resolver and the query embedder are built by the engine *through the adapter* (`make_memory_resolver` / `make_embedder`, the facade seam D3) and injected onto `ToolContext` (`memory_resolve_fn` / `memory_embed_fn`). The whole `memory/` package therefore imports no `pydantic_ai` and is 100% offline-testable — every P10 unit test runs with a deterministic fake resolver + hash embedder at **zero live cost**; only the benchmark spends. Cost of the resolver is attributed to the owner as `kind="memory"`, exactly like the summarizer (O6.8). |
| D57 | **Write-time reconciliation, not append-only** (P10): `memory_write` does NOT just insert. It runs exact-dedup (normalized md5 fast path, merge provenance) → hybrid candidate recall → LLM resolve (duplicate vs contradiction, integer-indexed with an anti-hallucination id remap, mem0's mechanism) → **temporal-overlap arithmetic** ported near-verbatim from Graphiti's `resolve_edge_contradictions`: a contradiction supersedes ONLY when the validity windows overlap and the old fact started first; sequential truths stay both-live. This is the supersede-vs-accrue decision mem0 (non-deterministic prose-merge) and cognee (first-write-wins) get wrong — and the reason we beat mem0 5/5 vs 0/5 on knowledge-update. Built frontier-informed, NOT adopted as a black box (mem0's OSS value is thin: it dropped write-time reconciliation in v3, has no bi-temporal model, no memory→message provenance, and drags qdrant). |
| D56 | **Bi-temporal fact store on the portable stack** (P10): memory is a `ai_memories` table with valid-time (`valid_at`/`invalid_at`, when the fact was true in the world) + transaction-time (`recorded_at`/`superseded_at`, when we believed it) — Graphiti's model, but relationally, since fact-memory needs no graph traversal. "Invalidate, don't delete": a superseded fact keeps its row, so "true now", "true at date T" (valid-time travel), and audit are one query. Embeddings are portable JSON ranked with Python cosine (no pgvector — the transcript's pattern, runs on SQLite + Postgres); hybrid semantic+lexical recall fused by **RRF** (Graphiti verbatim). Scoped per `(owner_id, agent_name)` so memory is durable ACROSS conversations (D54: cross-user visibility is the host's concern, not ours). |
| D55 | **Memory is agent-authored via tools, gated by `features.memory`** (P10): the agent decides what to remember through `memory_write/search/supersede/delete` (Letta-adapted docstrings + uniqueness guards), NOT an implicit "remember everything" scrape. An always-on usage preamble tells the model to search before answering and never assert an unretrieved fact. Off by default — a fresh clone opts an agent in. `memory_search` takes an `as_of` date so the model can time-travel; provenance (`source_turn_ids`) links every fact back to the transcript turn that produced it (append-on-dedup), which mem0 OSS cannot do. |
| D54 | **The AI service is the product, not a tenancy platform**: anything beyond AI business logic — tenant config, cross-user permission/visibility scoping, product settings — is a **platform** concern of whatever hosts the service (today the startup factory's platform layer; later the standalone service's own platform layer). The service scopes only by the `owner_id` it is handed and exposes per-agent toggles; it never grows tenant/product config. (Conceptual, high priority — it recurs: e.g. user-added extensions are keyed to `owner_id`, and who-sees-what is the host product's job, not ours.) |
| D53 | **Skills are containerized** (P9): a skill bundle (SKILL.md + files + scripts) lives in the sandbox. Progressive disclosure keeps the window lean — L1 metadata (name+description) is an always-on instructions preamble; the L2 body loads only when the model calls `open_skill`; L3 scripts run via `code_execution` with the bundle mounted at `/skills/<id>/` (their bytes never enter context). Network-off in v1 (inherits the P11 workspace egress policy later). Dev skills are code-registered by id; user skills are their pasted SKILL.md text. |
| D52 | **MCP via the official `mcp` SDK bridged into the tool table** (P9), NOT pydantic-ai's MCP capability: that pulls `fastmcp-slim` (needs `starlette>=1.0.1`), which hard-conflicts with FastAPI 0.115 (`starlette<0.46`). The `mcp` SDK needs only `starlette>=0.27` and coexists; `extensions/` is the sole `mcp`-importing seam. Bridging is also the cleaner fit — each server's tools become per-run tool-table fills (`mcp__<server>__<tool>`, bound via `Tool.from_schema`), inheriting billing + the tool path uniformly, results stored as untrusted data. Dev-added = catalog id (raw URL back-compat); user-added = **remote-only, token-paste** (no OAuth client — the product owns auth and hands us a token). Whole-tool P8 approval on MCP tools is best-effort. |
| D51 | **Per-agent BYO extension toggles** (P9): `allow_user_mcp` / `allow_user_skills` on the agent (default off, developer-controlled). When on, that agent's users add their OWN MCP servers / skills — **no allowlist**, the toggle is the gate; stored per `owner_id` (`ai_user_extensions`), loaded at run only when the toggle is on. Safety comes from the runtime posture (remote-only + SSRF for MCP, sandbox for skills, results as data, provenance labels), not from vetting each entry. Two-axis trust: a user-added extension is untrusted regardless of the user's tier. |
| D50 | **Extension catalog** (P9): the dev-owned registry of MCP servers + Skills, code-registered by id; agent specs reference the id, never inline config or secrets (credentials live as a header/secret-ref on the catalog entry). Mirrors the tool table's trust boundary (D33) — a spec is data and must never smuggle in a live server URL, a secret, or an executable bundle. The removed thing (vs the first sketch) is a *user allowlist*; the dev catalog stays. |
| D49 | **Prompt-injection defense is a dedicated research track, NOT built in P8** (deferred, owner-directed). The threat is worst where an agent takes consequential actions on content an attacker can shape: e.g. a future Sentry→LLM pipeline that turns a stack trace into a PR — a crafted error message carrying embedded instructions could steer the agent to change the app's CODE, strictly worse than a data leak or wasted spend. Necessary structural down-payment already made (P1/P5: tool results + retrieved content are stored as DATA items, never instructions), but that is not sufficient. The real defense is dataflow, not filtering: per-span **trust/provenance** marking + **capability confinement** (a consequential call whose args derive from an untrusted span is forced to approval or blocked), building on the P8 checkpoint. No trust marker, no confinement policy, and no "safety moderation" gate ship until that research lands — safety is the model's job; an optional UGC moderation fill is a niche per-agent toggle, not a phase. |
| D48 | **Denial is data, not an error**: an approve/deny decision resolves a paused call — a denial becomes the tool result the model reads (`ToolDenied` → a `tool_result` item), so the agent adapts (tries another approach, tells the user) instead of the run crashing. Approve may carry `override_args` to edit the call before it executes. |
| D47 | **HITL pause = a transcript checkpoint, never a blocked worker**: a gated call ends the run with `RunPaused`, persisting the pending `tool_call` item (marked `meta.approval="pending"`) WITHOUT its result and releasing the connection. Resume is an ordinary engine run over the checkpointed transcript (pending call rendered bare, deferred results supplied by the decisions), so budgets/branching/model-switching all apply and a resumed run may itself complete, fail, or pause again. A new turn instead of a decision abandons the pending call (synthetic "not executed" return); the same checkpoint is the **turn-granular durable-resume** story (no Temporal — intra-run resume is out of scope by owner decision). |
| D46 | **Only OUR tools are approval-gateable; native tools are never forced**: `AgentSpec.requires_approval` (whole-tool) + a code-registered `approval_predicate` (per-invocation) gate a run, but ONLY tools that resolve to our client-side implementations — a provider-native tool runs inside the provider and is structurally unreachable by the gate (a native name in the scope is inert, logged, never forced into the fill lane). Default empty = fully dormant, scoped per-agent + per-tool + per-invocation. |
| D17 | **Models built explicitly in the engine** (`Model` + `Provider` instances) from registry data + our secret config. Model strings (`'openai:...'`) allowed only in tests/scripts. `FallbackModel` wrapping when the agent spec declares fallbacks. |
| D18 | **OpenAI via Responses API** (`openai:` prefix — reasoning round-trips between turns). `openai-chat:` / `OpenAIChatModel(base_url=...)` only for OpenAI-compatible third parties (DeepSeek, Ollama, ...). |
| D19 | **Agents constructed programmatically from OUR agent-spec schema** (never persist Pydantic AI's YAML spec format). Agents are stateless singletons, cached per spec; always pass `name=`. **Consumer/product-level hand-written agents MAY use decorators** once the framework is complete — decorators are sugar over the same constructor args and don't violate the facade (the facade rule constrains platform engine code, not product agents built on top of it). |
| D20 | **Output modes registry-driven** (extends D12): `ToolOutput` default; `NativeOutput` when supported and streaming-of-structure not needed; `PromptedOutput` auto-fallback; `StructuredDict` for output schemas defined as data in agent specs. Never put `str` in the output union when structure is mandatory. |
| D21 | **Capabilities are the per-agent feature-toggle mechanism** (req. 8): our spec lists capability names + config; engine maps to built-ins (`Thinking`, `WebSearch`, `MCP`, ...) or ours (RAG-over-history, memory). **`defer_loading=True` is the default posture** — eager loading must be justified. |
| D22 | **One platform `RunDeps` dataclass** (db session, user, conversation id, budget meter) with an extension slot for product agents; bespoke `deps_type` only for hand-written product agents. |
| D23 | **Budget → `UsageLimits` bridge**: pre-flight budget check in our meter derives the run's `UsageLimits` (`request_limit`, `tool_calls_limit`, `output_tokens_limit`) from remaining budget. Settings layering: registry → model defaults; agent spec → agent level; per-request → run level. Retry budgets explicit in the spec; meter counts retry tokens. |

| D24 | **Transcript storage** (resolves U1/O1 direction): thin canonical item schema of our own + Pydantic AI's serialized message kept in a `raw` column per item. Readers (summarizer, RAG, UI, analytics) touch only our schema; `raw` gives lossless same-provider replay and is dropped when rendering for a different provider. |
| D25 | **Summarization placement** (resolves U2/O6 direction): hybrid — our renderer is the primary policy (builds the per-model view from the transcript *before* the run, using registry context windows + summary cache); a thin `before_model_request` overflow hook is the safety valve for single runs with long tool loops, **deferred until needed**. |

| D26 | **Extraction-ready packaging**: this service will outgrow QUANTSOC and become its own web service (with React design-system components, contexts, and an integration skill for AI agents). Package owns its `models.py` / schemas / exceptions; tables prefixed `ai_`; `owner_id: str` with no FK into host tables; host couplings (db_types, sessions, settings) documented and injected. See `PHASE1.md`. |
| D27 | **API keys**: direct keys for OpenAI, Anthropic, Google, xAI + one OpenRouter key for long-tail models. No gateway product (consistent with D5). Needed at P2, not P1. Keys are prepaid-credit based with per-provider dashboard caps, no auto-renew (owner-managed). |
| D45 | **An explicit speciality pick beats native capability**: `MediaModels` fields unset (None) = prefer the chat model's native modality when the registry flags it, fill otherwise (D41); an explicitly named `image_model` forces the `generate_image` fill with that model even on a natively image-capable chat model (pair a weak-image chat model with the best image model), and an explicit `tts_model`/`stt_model` on a voice config forces the pipeline over the native realtime adapter (only the pipeline can honor the pick). No extra toggle — the pick itself is the override (config minimalism). |
| D44 | **Media billing units are first-class**: registry `Cost` carries `per_million_characters` / `per_image` / `per_audio_minute` beside the token rates; `price_media(quantity, unit, info)` prices unit-billed calls with a deliberately expensive pessimistic fallback (`PessimisticMediaRates`). Token evidence wins when the provider reported it AND the registry prices those tokens; ledger kinds `voice` and `image` join `run`/`summarization`/`embedding`. |
| D43 | **Media aggregator lanes**: fal.ai (one `FAL_API_KEY`) is the media long tail — `fal:<endpoint-path>` ids route straight to the fal catalog (TTS voices incl. ElevenLabs/MiniMax, image models incl. FLUX/Ideogram/Recraft) at direct-price parity; OpenRouter's `/audio/transcriptions` (existing key) is the batch-STT long tail. Realtime/streaming speech always goes direct to the provider (no aggregator streams). Each lane is dormant without its key — picking a model on a missing lane fails validation loudly. |
| D42 | **Speciality models are pickable DATA with platform defaults**: `AgentSpec.media` (`MediaModels`: `tts_model`+`tts_voice`, `stt_model`, `image_model`) — canonical ids validated by the media port (adapter lane + key + registry output-modality check; unknown ids stay usable day one). Unset = platform defaults on keys the factory already holds (`openai:gpt-4o-mini-tts`, `openai:gpt-4o-mini-transcribe`, `google:gemini-3.1-flash-image`). Voices are provider-scoped strings passed through verbatim — the platform never maintains a voice list. The P6 pipeline constants became these defaults. |
| D41 | **Output-modality fills extend D34 from tools to modalities** (supersedes D40's no-fill clause; D40's persistence contract — MediaPart items + MediaGenerated events — is unchanged and shared by both paths): per-agent toggles `image_generation` and `speech_output` (both default off; owner decision — textual UIs use `POST /ai/speech` read-aloud instead of always-on speech). Native when the registry flags the serving model; otherwise OUR fill: image = the `generate_image` tool (media staged via the ToolContext collector, persisted atomically with the run, billed `kind="image"`), speech = a best-effort post-run TTS stage (billed `kind="voice"`). Callers cannot tell which path ran. |
| D40 | **Image generation is a D34 native capability** enabled per registry flag (`capabilities.image_generation`, override-set) through the SDK's `ImageGeneration` capability; generated images persist as `MediaPart` message items (the universal form IS the image — no `raw` needed) and stream as the `MediaGenerated` run event. *The original "no fill" clause was superseded by D41 (P7): non-capable models now get the `generate_image` fill instead of a loud failure.* |
| D39 | **Provider-SDK quarantine extends the facade rule to voice**: `voice/adapters/*.py` are the ONLY modules that may import provider realtime/audio SDKs or speak provider wire formats (websockets relay for OpenAI Realtime, google-genai for Gemini Live, the OpenAI audio HTTP API for the pipeline's STT/TTS), exactly as adapter.py/providers.py are for `pydantic_ai`. `test_facade_boundary` greps both boundaries; everything crossing the port is OUR `VoiceEvent`. |
| D38 | **Sandbox is a PORT with pluggable backends; dormant without config**: `SandboxBackend.start(workspace, limits) -> SandboxSession` (exec/put_file/get_file/close). First backend: Docker (CLI-driven; `--network none`, non-root, cap-drop, tmpfs workspace, output caps). `AI_SANDBOX_BACKEND` unset -> the `code_execution` fill raises its typed error (fail-closed, D32-style posture); Render has no Docker daemon, so prod ships dormant until a hosted backend (E2B/Modal — later org-services decision). The workspace materializes AGENTS.md, agentskills.io skills, and `.mcp.json` before execution (D14, requirement 11); the port shape keeps O12 (Claude-Agent-SDK/Cursor-SDK backend) open. |
| D37 | **Voice: backend WebSocket relay; the registry decides native vs pipeline**: clients speak ONE WebSocket to us (`/ai/conversations/{id}/voice`) — provider keys stay server-side, usage is metered into the ledger (`kind="voice"`, audio token rates), transcripts persist even if the client dies. `capabilities.realtime` (override-set) -> the provider's native adapter (OpenAI Realtime, Gemini Live); otherwise the STT→`stream_agent`→TTS pipeline serves the SAME `VoiceSession` contract — every model is voice-capable from day one. Budget pre-flight at connect; every usage event re-checks mid-session (a long call cannot outrun an enforced budget). |
| D36 | **Voice is a PORT beside the engine, not a run mode inside it**: realtime providers hold conversation state server-side during a session, so the port BOOKENDS the engine's world — it renders recent context in at connect and persists results out (transcribed turns as ordinary `message` items with `meta.modality="voice"`, session lifecycle as `event` items). Text and voice share ONE conversation: the next text run renders what was said, and RAG/summarization/budgets apply unchanged. In pipeline mode each turn IS an ordinary engine run, so all P2–P5 machinery applies. |
| D35 | **Embeddings run in the BACKGROUND, never on the hot path**: after a run persists its items, the engine schedules embedding backfill as a detached task with a FRESH session (never the request's); search degrades gracefully to text matching until vectors exist. Embedder = OpenAI `text-embedding-3-small` behind the P1 `EmbedderProtocol`, constructed in the adapter (sanctioned file); spend recorded `kind="embedding"`. No embeddings key = semantic search stays dormant, nothing fails. |
| D34 | **One tool name, two implementations, the registry decides**: for features providers offer natively (web search/fetch/code execution), the adapter enables the native capability when the run model's registry flag says so; otherwise the engine injects OUR client-side fill as an ordinary function tool under the SAME name and contract. Agents and transcripts are indifferent to which ran — `meta.implementation = "native" \| "ours"` on tool items records it for analytics, and native web-search calls carry the registry's per-call surcharge into the spend event. The split lives in ONE function (`tool_table.split_native_tools`) so engine and adapter can never disagree. |
| D33 | **Tool registration is CODE; tool selection is DATA**: implementations enter only via `register_tool(...)` at import/startup (platform tools in `platform_tools.py`, product tools from the product boundary); `AgentSpec.tools` carries NAMES only, so a spec — data — can never smuggle in an implementation. A spec naming an unregistered tool fails agent construction loudly (`ToolNotRegisteredError`). Implementations receive an injected `ToolContext` (db, owner, conversation, features, embedder) that model arguments can never reach or even name (D11). |
| D32 | **Budgets are INFORMATIVE BY DEFAULT**: cost tracking (ledger, `RunCompleted.cost_usd`, `/ai/usage`) is always on; pre-flight blocking is opt-in (`AI_BUDGET_ENFORCEMENT=true`). The consuming product owns its limits — per-user/agent/conversation `ai_budgets` rows with its own windows, or the settings-level owner defaults. Fail-closed semantics apply only once enforcement is explicitly enabled. |
| D31 | **Registry-sync alerting policy**: ordinary cross-feed price disagreements are NOT Sentry events — they are expected (feeds update at different cadences) and are surfaced where they're actionable: the sync report + PR diff. Sentry fires only for: a feed failing entirely on schedule (warning), >2x divergence on a model we actively use (warning), and runtime pricing misses (see PHASE4). Agent+web-search price confirmation on disagreement is DEFERRED (O13). |
| D30 | **RAG retrieval scope**: `search_conversation` defaults to the ACTIVE PATH only — abandoned/edited-away branches are treated as retracted content (an edit is usually a retraction; dead branches hold regenerated-away answers that contradict the live thread). `scope="all_branches"` exists as an explicit opt-in, to be gated behind a per-agent toggle in P5. `full_history` remains a system/owner view (audit, export, edit-history UI) — never a default model surface. Storage always keeps everything; scope governs surfacing only. |
| D29 | **Async layering**: the transport/engine layer is async-native end to end (async endpoints, `run_stream_events()` async iteration, cancellation on client disconnect stops token spend) — long-held connections never occupy threads. DB access stays sync-Session-via-`asyncio.to_thread` (millisecond holds, host-consistent DI); flip to async SQLAlchemy sessions at extraction (the standalone service is ~100% streaming workload) or earlier if profiling shows threadpool pressure. The store's session-first thin functions make that flip mechanical. |
| D28 | **Provider fallback to OpenRouter**: when a direct provider is unusable — key missing at construction time, or the call fails with auth/credit/quota errors (401/402/429-insufficient) — the engine falls back to the same model via OpenRouter. Implementation: engine-level construction fallback for missing keys + `FallbackModel(direct, openrouter)` for runtime errors. Fallback events are logged loudly (Sentry) so exhausted credits get diagnosed, not silently absorbed. NOTE: fallback lane requires an OpenRouter credit balance; a $0 balance means the fallback is dead on arrival. |

Full buildable specs: **`PHASE1.md`**–**`PHASE7.md`** (same directory).
Media-provider market research (TTS/STT/image, July 2026): **`MEDIA_PROVIDERS.md`**.

**Development rule (owner-mandated):** every piece of code in this scope is
thoroughly commented in the repo's commenting style (present-continuous ending
with a colon; section headers in large files). Sparse commenting is a review
blocker here.

## Future consideration — Cursor SDK / coding-agent harnesses (O12)

Containerized coding agents (req. 11) may integrate Cursor's agent SDK/CLI (or
Claude Agent SDK — see D15). Two integration shapes, both compatible with this
architecture without changes:
- If Cursor exposes a model/completions-style API → wrap it as a Pydantic AI
  custom `Model` subclass (documented extension point) → it becomes just another
  provider in the registry.
- If it is an agent harness (owns its own loop/tools like Claude Agent SDK) → it
  plugs in at the **sandbox/runtime port (P6)**, not the model layer; its
  transcript events get translated into our items like any other run.
No commitment yet; revisit at P6.

## Legacy code review (July 2026) — salvage notes

The pre-existing code in this directory was reviewed. Verdict: architecture
superseded (OpenAI-only, hand-rolled agent loop, hand-maintained model registry —
all replaced by Pydantic AI + registry feeds), but these **ideas carry forward**:

- **Three-threshold summarization policy** (`target` / `trigger` / `wait` as
  percentages of context window, computed per model; background summarization
  task between trigger and wait, blocking only past wait; incremental summaries
  building on previous ones) — best idea in the codebase; adopt as the P2
  renderer's policy vocabulary.
- **Tree-shaped conversations** (branching, edit-as-sibling-branch, dummy root so
  the first message is editable; recursive-CTE path loading) — carry into the P1
  item schema as `parent_item_id` branching.
- **Summary stored on the boundary node** ("summary of everything before me") —
  clean placement; becomes the summary-marker item pattern.
- **Typed content-part DTOs with explicit DB serialization** — validates the
  item-schema approach; the new schema generalizes it.
- **Per-model token caching on messages** (TokenCache descriptor) — right
  instinct; P1 stores token counts per item, cached per tokenizer family.
- **Not carried**: module-level SDK clients and hand-listed `Model` objects
  (registry replaces), tiktoken-everywhere counting (OpenAI-only; use provider
  usage + registry estimates), the custom tool-schema generator and agent loop
  (Pydantic AI replaces), `InputAudio.__new__`-returns-coroutine trick (clever,
  unmaintainable).

## P1 — Transcript store: detailed plan

Goal: the provider-independent source of truth everything else renders from.
No `pydantic_ai` imports anywhere in P1.

1. **Item schema** (`transcript/types.py`): `Item` = id, conversation_id,
   `parent_item_id` (branching, D24 tree), `kind`
   (`message` | `tool_call` | `tool_result` | `thinking` | `media` | `summary_marker` | `event`),
   `role`, `content_parts` (typed JSON list), `raw` (opaque provider/framework
   payload, D24), `model_used`, `usage` (input/output/reasoning/cache tokens),
   `created_at`, `meta` (JSON). Enums stored as `.value` strings; JSON via
   `infrastructure/db_types.JSONColumn` (repo DB rules).
2. **Tables** (`platform/models.py` + manual migration): `conversations`
   (id, user_id, title, current_item_id, created/updated) and `transcript_items`
   (columns above; indexes on conversation_id, parent_item_id, kind). Summary
   cache table: `transcript_summaries` (span_start_item, span_end_item,
   token_budget, model_family, summary_text, usage) — summaries are cache
   entries, never replacements (D4).
3. **Store API** (`transcript/store.py`): `append(item) -> Item` (append-only;
   sets conversation.current_item), `get_path(item_id) -> list[Item]`
   (recursive CTE, root→item), `branch_from(item_id, item)`,
   `full_history(conversation_id)`, `get_summary(span, budget)` /
   `put_summary(...)`. Service-layer style per repo rules (session first arg);
   async wrappers via `asyncio.to_thread` until async sessions are decided (O2).
4. **Embedding index** (`transcript/search.py`): embed text-bearing items on
   append (background task); store vectors (pgvector on PostgreSQL — portability
   exception must be documented per DB rules, or fallback simple FTS first);
   `search_conversation(conversation_id, query, k)` returning items. Ship FTS
   first if pgvector decision drags — the API is what matters.
5. **Tests**: pure-Python round-trip tests (serialize/deserialize every kind),
   branching/path tests, summary-cache keying tests. No model calls anywhere.

Done criteria: a conversation can be written, branched, reloaded, searched, and
fully reconstructed byte-for-byte, with zero AI SDK dependencies.

## Decisions — OPEN

- **O1 — Concrete item schema** for the canonical transcript (field-level design,
  Open-Responses alignment depth, provider-opaque item passthrough rules, branching:
  keep tree/branch editing from the old design or linear-with-forks?).
- **O2 — Storage design**: tables, indexing, embedding store (pgvector?), summary-cache
  keying; sync vs async SQLAlchemy sessions for tool code (current DI yields sync
  sessions → `asyncio.to_thread` or async engine?).
- **O3 — Registry schema v1** (merge rules across the three feeds, conflict policy,
  override file format) and reconciler-agent cadence/ownership.
- **O4 — Budget hierarchy semantics** (which levels ship first; pre-flight estimation
  strategy given no `count_tokens`; retry-token accounting).
- **O5 — Our event schema** (names/shape of facade stream events; AG-UI vs Vercel
  stream vs custom for the chat UI).
- **O6 — Summarization policy.** The target/trigger/wait thresholds answer only
  WHEN; P2's renderer must not ship without explicit answers to all of these
  (proposed defaults in parentheses; 3 and 7 are correctness rules, decided):
  1. Summarizer model (cheap fixed mini-tier model from spec, never the
     conversation model).
  2. Per-kind inclusion (tool results truncated/dropped first; thinking
     excluded; media as placeholder + transcript; messages carry narrative).
  3. **DECIDED — boundary atomicity**: never separate tool_call from
     tool_result, never split an assistant turn; snap cuts to turn seams.
  4. Summary content spec (structured prompt preserving decisions, facts,
     preferences, open questions, task state).
  5. Incremental chaining with a depth cap; every N rounds regenerate from
     items (RAG is the fidelity backstop).
  6. Cache-budget quantization to standard buckets (2k/4k/8k/16k) so similar
     models share cache entries.
  7. **DECIDED — failure policy**: on summarizer failure, plain truncation
     (drop oldest turns, keep tail verbatim) + loud Sentry alert; NEVER inject
     error strings into model context (legacy did; forbidden).
  8. Cost attribution (summarization billed to the conversation owner as an
     itemized system cost) + prompt-cache awareness: re-summarization rewrites
     the prompt head and invalidates provider prompt caching — prefer wide
     target/trigger hysteresis (summarize rarely and deeply).
  9. Concurrency: accept duplicate summarizer calls on worker races
     (put_summary is idempotent); no distributed locks.
- **O7 — Sandbox provider** for req. 11 (E2B / Modal / Fly / plain Docker; embed Claude
  Agent SDK for coding-agent surface or build thinner).
- **O8 — Realtime voice architecture** (which provider first, session port design,
  fallback pipeline).
- **O9 — Evals strategy** (pydantic-evals datasets per agent? cross-model eval matrix on
  model switch? CI integration).
- **O10 — Module layout / naming**: keep `generative_ai_service` or restructure to
  `platform/ai/` (transcript / registry / engine / tools / sandbox / realtime) per the
  research doc's build order. Skill directory moves with it.
- **O13 — Agent+web-search price verification** on sync disagreements or a slow schedule (deferred from P3; revisit after P4).
- **O11 — Skill freshness automation** (scheduled re-vendor of D16's snapshot).
- **O14 — Media object storage (STANDING REMINDER — owner asked to be re-prompted)**:
  generated images/audio persist as base64 inside `ai_transcript_items.parts` (and fal
  results as provider-hosted URLs that may eventually expire). Fine at current scale,
  wrong at product scale: move media bytes to object storage (R2/S3) with items holding
  URLs, and copy aggregator-hosted results into our bucket. Raise this at every merge
  gate / phase review until scheduled.
- **O15 — GPT-Live in the API (watch item)**: OpenAI's full-duplex consumer voice model
  (July 2026) has a sign-up form for future API access; when it ships, it likely becomes
  the realtime default over `gpt-realtime-2.1` — one registry override + audit of the
  relay event names.

## Incorporating Pydantic AI — starting shape

Build order (from `planning/AI_SDK_RESEARCH.md`, adapted to this scope):

1. **Transcript first** (req. 2/6/7): item types + append-only store + embedding index +
   `search_conversation` tool. No `pydantic_ai` import anywhere here.
2. **Engine facade**: our `Agent`/`Run`/`Stream` + our event schema. Inside it, one
   adapter module that: builds Pydantic AI agents from our agent spec (D13),
   renders transcript → Pydantic AI `message_history` per run (model-agnostic, replays
   across providers), runs `run_stream_events()`, translates events out, appends
   resulting items back to the transcript, meters usage × registry prices (D7).
3. **Registry** service + sync jobs (D6); engine consults it for context windows,
   settings surfacing, structured-output mode (D12), native-tool availability (D15).
4. **Tools**: MCP client integration + our client-side fills (RAG tool, memory) where a
   provider lacks the server-side native tool.
5. **Sandbox / realtime** last (O7/O8), behind their own ports.

Integration mechanics already validated in this conversation:

- FastAPI DI composes cleanly: router resolves `db_dependency`/`user_dependency`,
  constructs the run's deps object, passes `deps=` to the run — Pydantic AI never
  manages sessions (wrap sync DB calls in `asyncio.to_thread`).
- Agents are stateless singletons (or spec-constructed); all per-request state enters
  via `deps` at run time.
- Decorators are optional sugar — spec-driven construction passes `tools=[...]` /
  toolsets / capabilities programmatically.
- History from one provider replays to another; system-prompt positioning handled by
  the framework (with D9 making instructions the only prompt mechanism we use).
