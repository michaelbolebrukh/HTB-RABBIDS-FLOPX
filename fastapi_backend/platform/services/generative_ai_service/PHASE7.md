# Phase 7 — Output-Modality Supplementation: Full Development Spec

Status: BUILT (July 2026). Companion to `README.md` (D1–D45) and
`MEDIA_PROVIDERS.md` (the verified market research behind every provider
choice here). Owner decisions folded in during the build: speech_output
defaults OFF (read-aloud endpoint serves textual UIs); default image model =
gemini-3.1-flash-image; base64-in-DB media storage accepted with the O14
standing reminder; the full speech system (read-aloud + voice-to-voice native
and pipeline + pickable models/voices) shipped end to end; FAL_API_KEY
provisioned on Render 2026-07-12. GPT-Live has no API yet (O15 watch item) —
the native realtime default remains gpt-realtime-2.1.

The driving observation (owner-supplied, confirmed by research): even frontier
chat models output **text only** — OpenAI's GPT-5.6 line (Sol/Terra/Luna)
supports no image and no audio output, Claude none, Gemini's text line none.
Native media output exists only on speciality lines (Gemini's image models,
gpt-audio/gpt-realtime). So "the agent can speak and draw" must be OUR
guarantee, not the model's: native where the registry says so, supplemented by
speciality models everywhere else — and the speciality model, like the chat
model, must be **pickable from virtually the whole market**, not pinned.

## Goals / non-goals

**Goals**:
- **Every agent can output images and speech regardless of its chat model**,
  behind per-agent toggles. Native modality output rides the serving model
  when the registry flags it; otherwise OUR fill supplies it through a
  speciality model — same items, same events, callers indifferent (the D34
  philosophy extended from tools to output modalities).
- **Speciality models are pickable data**: TTS model + voice, STT model, and
  image model become spec fields validated against the registry, with platform
  defaults — mirroring how the chat model is already chosen. The P6 pipeline
  constants (`gpt-4o-mini-tts` etc.) become defaults, not pins.
- **The media long tail behind aggregator lanes**: fal.ai (one key ≈ the
  OpenRouter-of-media: ElevenLabs, MiniMax, Qwen3-TTS, Kokoro, FLUX, Ideogram,
  Recraft, Grok Imagine — at direct-price parity) and OpenRouter's audio
  endpoints (batch STT on the key we already hold).
- **Unit-correct billing**: media bills per character / image / audio-minute /
  media-token depending on provider; the registry and pricer carry the unit
  instead of assuming tokens.

**Non-goals (explicitly out of P7)**:
- Video generation — delegated to skills (owner decision, July 2026).
- Voice cloning flows (consent capture, storage) — provider cloning is reachable
  by naming a cloned voice id, but WE build no cloning UX yet.
- Input-modality supplementation beyond what P6 shipped (STT already covers
  audio-in; image/PDF-in wiring is a P2 leftover, tracked separately).
- Self-hosted/open-weight serving (Kokoro, Voxtral) — reachable later through
  an OpenAI-compatible `base_url` adapter without new architecture.

## Design decisions (proposed)

**D41 — Output-modality fills extend D34 from tools to modalities**
(supersedes D40's "no fill" clause; D40's persistence contract — MediaPart
items + `MediaGenerated` events — is unchanged and shared by both paths).
Per-agent toggles on `AgentFeatures`: `image_generation` (exists) and
`speech_output` (new). Selection per run, in one place:
- Model natively emits the modality (registry `capabilities.image_generation`
  / `modalities.output`) → native capability, exactly P6's D40 path.
- Otherwise → OUR fill: **image** = a registered `generate_image` platform
  tool (D33 table; the model calls it like any tool, the tool calls the
  configured image model, the result persists as the same MediaPart item and
  streams as the same MediaGenerated event); **speech** = a TTS stage over the
  run's output text (the P6 pipeline's TTS leg, promoted to a general
  post-run stage usable outside voice sessions).
Even OpenAI implements "native" image output as delegation to gpt-image behind
a tool — our fill is architecturally identical, one layer up and
provider-independent.

**D42 — Speciality-model choice is DATA with platform defaults.** A new
`MediaModels` block on `AgentSpec` (and reused by `VoiceConfig`):
`tts_model` + `tts_voice`, `stt_model`, `image_model` — canonical
`provider:model` ids, validated against the registry's modalities at agent
construction (loudly: an id whose registry entry cannot output the modality is
a `CapabilityUnavailableError`, not a runtime surprise). Unset → platform
defaults chosen from keys we already hold: `openai:gpt-4o-mini-tts`,
`openai:gpt-4o-mini-transcribe`, `google:gemini-3.1-flash-image`. Voices are
provider-scoped strings passed through (OpenAI's 13, Google's 30, ElevenLabs'
10k+ library ids, cloned-voice ids) — we never enumerate them ourselves; a
`voices` listing passthrough per provider is a later nicety.

**D43 — Media aggregator lane: fal.ai; OpenRouter audio for batch STT.** The
D28 pattern applied to media: direct provider first, aggregator for the long
tail. One optional `FAL_API_KEY` exposes the fal catalog (registry entries
`fal:<endpoint>` with per-unit rates); OpenRouter's `/audio/transcriptions`
rides the existing `OPENROUTER_API_KEY` for long-tail batch STT. Without the
key, fal-routed models simply fail validation loudly (config-minimalism:
absent ≠ half-working). Realtime/streaming speech always goes direct to the
provider (no aggregator streams today). New org-services row when fal is
enabled — one key, one row.

**D45 — An explicit speciality pick beats native capability** (post-build owner
decision, July 2026). D41's selection order gains one clause: when the spec
explicitly names an `image_model`, the `generate_image` fill runs with that
model even if the chat model is natively image-capable (a chat model with weak
image output pairs with the best image model); likewise an explicit
`tts_model`/`stt_model` on a `VoiceConfig` forces the pipeline over the native
realtime adapter, since only the pipeline can honor the pick. Unset (None)
keeps the D41 default: native when flagged, fill otherwise. The pick itself is
the override — no extra toggle (config minimalism).

**D44 — Billing units are first-class.** Registry `Cost` gains
`per_character`, `per_image`, `per_audio_minute` (USD per unit) beside the
existing token rates and `web_search_per_call`. `price_usage` stays the token
path; a sibling `price_media(units, kind, info)` prices unit-billed calls.
Spend rows reuse the P4 ledger with kinds `voice` (exists) and `image` (new);
`RunCompleted.cost_usd` keeps including everything the run spent, fills
included. The pessimistic-unpriced policy applies to media models too.

## Module layout (additions / changes)

```
generative_ai_service/
  media/
    __init__.py       # public surface: synthesize_speech, transcribe, generate_image, MediaModels
    types.py          # MediaModels spec block, SpeechResult/ImageResult, errors (OURS — no SDK)
    port.py           # the three port functions + D42 validation + D41/D43 routing
    adapters/         # sanctioned provider seams (D39 quarantine grows here)
      openai_media.py   # /audio/speech, /audio/transcriptions, /images (httpx)
      google_media.py   # Gemini TTS + Gemini/Imagen image + Cloud STT
      elevenlabs.py     # optional key — premium voices + cloning ids
      fal.py            # aggregator lane (queue/poll + parity pricing)
      openrouter_audio.py # batch STT long tail on the existing key
  image_tool.py       # registers the `generate_image` fill (D33, like platform_tools)
  voice/adapters/pipeline.py  # refactored: STT/TTS legs call media.port (constants removed)
  engine/…            # speech_output feature; select_native_capabilities uses D41 selection
  registry/…          # Cost per-unit fields; overrides gain speciality-model entries
  billing/pricing.py  # price_media (D44)
```

## Interfaces (normative)

```python
# media/types.py
class MediaModels(BaseModel):
    tts_model: str | None = None       # None => platform default (D42)
    tts_voice: str = "default"         # Provider-scoped voice id, passed through
    stt_model: str | None = None
    image_model: str | None = None

# media/port.py — the three ports (async, adapters behind them):
async def synthesize_speech(text, models: MediaModels, *, stream=False) -> SpeechResult  # audio bytes + Usage/units
async def transcribe(audio: bytes, sample_rate: int, models: MediaModels) -> TranscriptResult
async def generate_image(prompt, models: MediaModels, *, size="1024x1024") -> ImageResult

# image_tool.py — the D41 image fill (registered like every platform tool):
async def generate_image_tool(ctx: ToolContext, prompt: str, size: str = "1024x1024") -> dict:
    """Generate an image from a text prompt. Returns a reference to the stored
    image item; raises ToolExecutionError when no image lane is configured."""

# AgentSpec (additive):
class AgentSpec(BaseModel):
    ...
    media: MediaModels = Field(default_factory=MediaModels)
class AgentFeatures(BaseModel):
    ...
    speech_output: bool = False        # D41: speak the run's output text
```

## Flows

**Image (fill path)**: agent has `image_generation=True`, serving model lacks
the native flag → `generate_image` tool is injected (D34 split, existing
mechanics) → model calls it → media.port routes to the configured image model
(direct or fal) → image persists as the SAME MediaPart item + MediaGenerated
event as the native path → spend row `kind="image"` at the image model's
per-unit rate.

**Speech (fill path)**: agent has `speech_output=True`, model has no audio
output → after `run_completed`, the engine emits the output text through
`synthesize_speech` → `AudioDelta`-style media event + MediaPart(audio) item
(voice sessions already do exactly this inside the pipeline; the stage is the
same code, run outside a session). Native audio-output models
(gpt-audio/realtime, Gemini native-audio) skip the stage.

**Voice sessions**: `VoiceConfig` carries `MediaModels`; the pipeline's STT/TTS
legs route through media.port, so ElevenLabs voices / Deepgram STT / any
registry speech model become session choices without touching the port.

## Tests (offline; adapters faked)

- D42 validation goldens: unset → defaults; non-speech model as tts_model →
  loud CapabilityUnavailableError; fal model without FAL_API_KEY → loud.
- D41 selection goldens per modality (native flag → native, absent → fill,
  toggle off → neither) — mirrors the D34 split tests.
- `generate_image` fill end-to-end with a fake adapter: tool call → MediaPart
  item + MediaGenerated event + `kind="image"` spend row.
- speech_output stage: TestModel run → audio media event + item + spend.
- price_media goldens per unit (chars, images, audio-minutes) + pessimistic.
- Facade greps extended: media adapters join the sanctioned-seam allowlist.

## Build order

1. Registry: Cost per-unit fields + overrides for the pickable set (OpenAI
   tts/stt/image, Gemini TTS, Imagen, gpt-image-1.5/2, ElevenLabs, Cartesia,
   Deepgram, fal endpoints); `price_media` (D44).
2. `media/` types + port + OpenAI/Google adapters; voice pipeline refactored
   onto the port (P6 constants become D42 defaults).
3. `generate_image` fill + D41 selection in the engine; speech_output stage.
4. ElevenLabs + fal + OpenRouter-audio adapters (each dormant without its key).
5. Docs, FEATURE_COVERAGE ticks, facade greps, full suite.

## Owner steps

None to start (OpenAI + Google keys cover defaults and the first pickable
tier). Optional, one org-services row each, when a product wants the lane:
`ELEVENLABS_API_KEY` (premium voices/cloning), `FAL_API_KEY` (media long
tail), `CARTESIA_API_KEY` (lowest-latency agents), `DEEPGRAM_API_KEY`
(streaming STT price-performance).
