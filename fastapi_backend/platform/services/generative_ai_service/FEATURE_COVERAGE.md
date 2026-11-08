# Provider-API Agent Feature Coverage

A living checklist of agent-relevant features exposed by the major providers'
APIs (OpenAI, Anthropic, Google, xAI), tracked against our phases. Update the
status as phases land. Anything still ❌/◐ after P6 is a candidate for a
dedicated follow-up phase.

**Legend:** ✅ covered · ◐ partial (plumbing exists / reachable via passthrough,
not first-class) · ⏳ planned (phase noted) · ❌ not covered

**Key nuance — the `model_settings` passthrough.** `AgentSpec.model_settings`
is a raw dict merged into the provider request, so many low-level knobs are
already *reachable* without a typed field. Those are marked ◐ (works, but not
surfaced/validated in our schema). The genuine gaps are the capital-F Features
below marked ❌.

## Generation core
| Feature | Status | Phase | Note |
|---|---|---|---|
| Text / chat generation | ✅ | P2 | `stream_agent`/`run_agent` |
| Streaming (token + events) | ✅ | P2 | `run_stream_events` → our event schema |
| Instructions (system/developer) | ✅ | P2 | `AgentSpec.instructions` (D9: never system_prompt) |
| Sampling knobs (temperature/top_p/penalties) | ◐ | P2 | via `model_settings` passthrough |
| Max output tokens | ✅ | P2 | `AgentLimits.max_output_tokens` |
| Stop sequences | ◐ | P2 | passthrough; no typed field |
| Seed / deterministic sampling | ◐ | P2 | passthrough where provider supports |
| Logprobs | ❌ | — | not surfaced; low priority |
| N candidates / multiple completions | ❌ | — | not surfaced |

## Reasoning / thinking
| Feature | Status | Phase | Note |
|---|---|---|---|
| Extended thinking / reasoning | ✅ | P2 | `features.thinking`; `ThinkingDelta` events |
| Reasoning effort control | ✅ | P2 | `features.thinking_effort` |
| Reasoning-token round-trip | ✅ | P2 | preserved via `raw` (D24) |

## Structured output
| Feature | Status | Phase | Note |
|---|---|---|---|
| JSON-schema structured output | ✅ | P5 | registry-driven mode selection (native vs tool vs prompted) — D20 closed |
| Tool-based output | ✅ | P2 | default output mode |

## Tools & function calling
| Feature | Status | Phase | Note |
|---|---|---|---|
| Function/tool calling loop | ✅ | P2 | agentic loop, `max_requests`/`max_tool_calls` |
| Tool definitions from our spec | ✅ | P5 | tool table: `register_tool` in code, names in spec (D33); ctx injection (D11) |
| Parallel tool calls | ◐ | P3 | capability flag in registry; behavior largely automatic |
| Tool choice / forced tool | ◐ | P2 | passthrough; no typed field yet |
| Human-in-the-loop / tool approval | ✅ | P8 | pause/resume tool loop; `requires_approval` (whole-tool) + `approval_predicate` (per-invocation); approve/edit-args/deny; ONLY our tools (D46); `RunPaused` + `resume_agent` (D47/D48) |
| Deferred / external tools | ◐ | P8 | same `DeferredToolRequests` seam as approval; external-result path not yet exposed as its own tool flag |

## Provider-native (server-side) tools
| Feature | Status | Phase | Note |
|---|---|---|---|
| Web search | ✅ | P5 | native per registry flag, our DDG-HTML fill otherwise — same name (D34); per-call cost attributed |
| Web fetch (URL) | ✅ | P5 | native per registry flag, our httpx fill (SSRF guard, size cap) otherwise (D34) |
| Code execution / interpreter | ✅ | P6 | native per registry flag; sandbox fill otherwise (D38 — dormant without `AI_SANDBOX_BACKEND`) |
| File search / managed RAG | ❌ | P5 | provider-side vector search |
| Image generation (as tool) | ✅ | P6/P7 | native per registry flag, `generate_image` fill otherwise (D41); pickable image model (D42); `MediaPart` items + `MediaGenerated` event |
| Computer use / browser control | ❌ | post-P6 | sandbox port shape keeps a Claude-Agent-SDK/Cursor-SDK backend possible (O12) |
| MCP servers (as tools) | ✅ | P9 | official `mcp` SDK bridged into the tool table (`mcp__<server>__<tool>` fills, D52); dev via catalog id, user-added remote-only + token-paste; NOT fastmcp (starlette conflict) |
| Agent Skills (SKILL.md) | ✅ | P9 | containerized progressive disclosure (D53): L1 metadata preamble, `open_skill` body, scripts in the sandbox at `/skills/<id>/`; dev + user-added |
| User-added extensions | ✅ | P9 | per-agent `allow_user_mcp`/`allow_user_skills` toggles, no allowlist, per-`owner_id` (D51); `GET/POST/DELETE /ai/agents/{id}/extensions` |
| Durable agent memory | ✅ | P10 | bi-temporal fact store (D56), write-time supersede-vs-accrue reconciliation (D57), `memory_write/search/supersede/delete` gated by `features.memory` (D55); as-of time-travel + provenance; **beats mem0 23/23 vs 16/23** (BENCHMARK.md) |
| X/Twitter search | ❌ | — | xAI-only; low priority |

## Multimodal I/O
| Feature | Status | Phase | Note |
|---|---|---|---|
| Image input | ◐ | P2 | `UserInput.media` + `MediaPart` exist; end-to-end wiring/test pending |
| Document / PDF input | ◐ | P2 | `MediaPart(media="file")`; wiring pending |
| Audio input (STT pipeline) | ✅ | P6/P7 | pipeline STT leg on pickable models (OpenAI direct, OpenRouter batch lane, Gemini ride-along), billed `kind="voice"` |
| Video input | ❌ | post-P6 | Google/OpenRouter only; an explicit P6 non-goal |
| Image generation output | ✅ | P6/P7 | every model via native capability or fill (D41); persisted as `MediaPart`; streamed as `MediaGenerated` |
| Audio output / TTS | ✅ | P6/P7 | read-aloud `POST /ai/speech`, per-agent `speech_output` stage, pipeline TTS leg — all on pickable TTS models/voices (D42); + native realtime audio |

## Realtime / voice
| Feature | Status | Phase | Note |
|---|---|---|---|
| Realtime voice-to-voice | ✅ | P6 | voice port: native adapters (OpenAI Realtime, Gemini Live) + pipeline fallback — every model voice-capable (D36/D37) |
| Bidirectional live streaming | ✅ | P6 | WS `/ai/conversations/{id}/voice`; our VoiceEvent schema |

## Caching & cost
| Feature | Status | Phase | Note |
|---|---|---|---|
| Prompt-cache-aware cost accounting | ✅ | P3/P4 | cache read/write rates from registry, priced per run |
| Explicit cache-control breakpoints | ❌ | — | providers auto-cache; explicit breakpoints not set — **minor gap** |
| Token cost calculation | ✅ | P4 | `price_usage` × registry rates, all dimensions; `RunCompleted.cost_usd` |
| Budget / limit enforcement | ✅ | P4 | pre-flight 429, fail-closed, budget→output-cap derivation |

## State, batching, orchestration
| Feature | Status | Phase | Note |
|---|---|---|---|
| Conversation state | ✅ | P1 | we own it (deliberately not provider-side) |
| Provider-side state (Responses `store`) | ✅ (by choice) | — | intentionally not used — we own the transcript (D4) |
| Batch API (async bulk jobs) | ❌ | — | not covered — **gap** if bulk workloads arise |
| Durable execution (Temporal/DBOS) | ❌ | — | Pydantic AI supports; not wired — **gap** |
| Multi-agent delegation | ❌ | — | Pydantic AI supports; not exposed yet |

## Safety
| Feature | Status | Phase | Note |
|---|---|---|---|
| Provider safety settings (e.g. Gemini) | ◐ | P2 | via `model_settings` passthrough |
| Moderation API | ❌ | — | not covered — **gap** if needed for UGC |

## Embeddings
| Feature | Status | Phase | Note |
|---|---|---|---|
| Embeddings (for our RAG) | ✅ | P5 | `text-embedding-3-small` behind `EmbedderProtocol`; background backfill (D35), spend metered |
| Embeddings as a first-class product feature | ❌ | — | not a goal yet |

---

## Gaps NOT owned by any current phase (revisit after P8)

These have no home in P1–P8 as scoped. Decide per-item at the end: cover in a
follow-up phase, or consciously drop.

1. **Prompt-injection defense** — **its own research track (D49)**, deferred by
   owner decision; nothing ships until the research lands. Threat model:
   indirect injection where an agent acts on attacker-shaped content — worst
   case a Sentry→LLM pipeline that emits a PR, where a crafted stack trace could
   steer the agent to change the app's code (worse than a data leak). The P1/P5
   data-channel separation is the necessary down-payment; the real defense is
   per-span **trust/provenance** + **capability confinement** riding the P8
   approval checkpoint, NOT a moderation filter.
2. **Safety moderation** — rely on the model itself; an optional UGC-facing
   moderation fill is a niche per-agent toggle if ever needed, not a phase.
3. **Durable execution (intra-run)** — turn-granular resume is covered by the P8
   checkpoint (owner-accepted granularity); mid-tool-loop exactly-once /
   long-running suspendable tools (Temporal/DBOS) remain out of scope.
4. **Batch API** — cheap async bulk inference.
5. **Explicit prompt-cache breakpoints** — cost optimization beyond auto-caching.
6. **Multi-agent delegation** — agent-calls-agent orchestration.
7. **Logprobs / N-candidates** — niche; likely drop unless a feature needs them.

Everything else is scheduled inside P3–P8 or reachable today via
`model_settings` passthrough.
