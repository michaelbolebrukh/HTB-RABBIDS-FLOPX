# Media Provider Landscape — TTS / STT / Image Generation (researched July 12, 2026)

The market-research record behind PHASE7 (output-modality supplementation).
Three parallel research passes, claims verified on official pricing/docs pages
(URLs inline). Prices are pay-as-you-go list, normalized per modality. This
file is the durable snapshot — re-verify before major pricing decisions
(provider prices moved noticeably during 2025–26).

---

## 1. Text-to-speech (TTS)

Normalized to **$/1M characters** (~150 wpm ≈ 750 chars/min of speech).

| Provider | Flagship models | Voices / langs | Cloning | Streaming (TTFA) | ~$/1M chars | Notes |
|---|---|---|---|---|---|---|
| **OpenAI** | `gpt-4o-mini-tts` (rec.), `tts-1`, `tts-1-hd` | 13 voices, ~57 langs | Sales-gated "custom voices" | HTTP chunked; Realtime WS | tts-1 $15 · hd $30 · 4o-mini-tts ~$20 | Steerable via `instructions`; key already provisioned |
| **ElevenLabs** | `eleven_v3`, `eleven_flash_v2_5` | **10,000+ voice library**, 70+ langs | Y (IVC $6+/PVC $22+) | WS, **~75 ms** (Flash) | Flash $50 · v3/mv2 $100 | Quality/branding leader; library voices can vanish on owner notice |
| **Google Gemini TTS** | `gemini-2.5-flash-tts` (GA), `gemini-3.1-flash-tts-preview` | 30 voices, ~75 langs, multi-speaker | Prompt-steerable only | streaming generateContent | ~$20 (flash) · ~$40 (pro/3.1) | LLM-based TTS; batch halves cost |
| **Google Cloud TTS** | Chirp 3 HD, Neural2, WaveNet | **380+ voices, 75+ langs** | Y (Instant Custom Voice $60/1M) | bidirectional streaming | $4 (std/WaveNet) → $30 (Chirp3 HD) → $160 (Studio) | Widest Google catalog; generous free tier |
| **Azure Speech** | Neural, Neural HD, CNV, Personal Voice | **~553 voices, ~150 locales** (widest anywhere) | Y (access-gated) | Speech SDK WS | $15 (neural) · $22 (HD); commit tiers → $5.70 | Enterprise lane; cloning gated |
| **Amazon Polly** | standard/neural/long-form/generative | ~100 voices, 40+ langs | Brand Voice (paid engagement) | HTTP streaming | $4 → $16 (neural) → $30 (generative) | No WS; no self-serve cloning |
| **Cartesia** | `sonic-3.5` | 42 langs | Y (from $5/mo) | **sub-90 ms**, WS + word/phoneme timestamps | $37–50 | Best latency/price for voice agents |
| **Deepgram** | `aura-2` | 41 EN voices, 7 langs | N | sub-200 ms WS | $30 | English-first |
| **Hume** | Octave 2 (preview), EVI 4-mini | Voice design by prompt, 11 langs | Y (15 s, all plans) | ~100 ms | $50–150 by tier | Emotion-first positioning |
| **MiniMax** | `speech-2.8-hd`/`-turbo` | **~330 voices**, ~40 langs | **Y — $1.50/voice** | WS + async ≤1M chars | turbo $60 · HD $100 | Huge voice count, cheap clones; CN-parent consideration |
| **Open weights** | Kokoro-82M (Apache), Chatterbox (MIT), Qwen3-TTS (Apache) | — | Qwen3: 3 s clone | Orpheus ~200 ms | hosted **~$0.70** (DeepInfra) | License traps: XTTS-v2, F5-TTS, OpenAudio = non-commercial |
| ~~PlayHT~~ | — | — | — | — | — | **DEFUNCT** (Meta acqui-hire 2025; platform deleted Dec 2025) |

**TTS long-tail lane: fal.ai** — ~33 TTS endpoints behind one key at
direct-price parity (full MiniMax line incl. $1.50 clones, first-party
ElevenLabs, Gemini TTS, xAI Grok TTS, Qwen3-TTS, Chatterbox, Kokoro, Dia,
Orpheus…). Together AI = best OpenAI-compatible ergonomics but narrow;
OpenRouter now has `/api/v1/audio/speech` (9 models) — watch it converge with
our existing chat routing.

**Chat models with NATIVE audio output (mid-2026):** OpenAI `gpt-audio-1.5`/
`gpt-audio-mini` (Chat Completions, audio $32/$64 per Mtok, mini $10/$20) and
the `gpt-realtime-2.1` family; Google `gemini-2.5-flash-native-audio`,
`gemini-3.1-flash-live-preview`; xAI `grok-voice-latest` ($3/hr realtime) +
Grok TTS; Alibaba `qwen3.5-omni-*`; Amazon Nova 2 Sonic; open-weight Moshi.
Mainline text flagships (GPT-5.6 Sol/Terra/Luna, Claude, Gemini text line)
emit **text only** — hence the fill layer.

---

## 2. Speech-to-text (STT)

Normalized to **$/audio-hour** (batch / streaming).

| Provider | Flagship models | Langs | Streaming | Diarization | Batch → Streaming $/hr |
|---|---|---|---|---|---|
| **Deepgram** | `nova-3`, `flux` (turn-taking ASR for agents) | 45+ | Y (mature WSS) | +$0.12/hr add-on | **$0.26 → $0.29** (flux $0.39) |
| **AssemblyAI** | `universal-3.5-pro`, Sync API (≤2 min, ~134 ms) | 18 native code-switch / 99 batch | Y | add-on | $0.21 → $0.15–0.45; Sync $0.45 |
| **OpenAI** | `gpt-4o-transcribe(-diarize)`, `gpt-4o-mini-transcribe`, `whisper-1`, **`gpt-realtime-whisper`** (streaming, May 2026) | ~99 | Y (Realtime WS, tunable delay) | `-diarize` variant (segment-level) | $0.36 (mini **$0.18**) → **$1.02** |
| **Google Cloud STT** | `chirp_3` | ~100+ (diarization 14) | Y (gRPC) | batch-only | $0.96 (dyn. batch **$0.18**) → $0.96 |
| **Gemini audio-in** | any Gemini model (32 tok/s audio, 9.5 h/prompt) | ~100 | Live API | via prompting | **~$0.045–0.17** (Flash-Lite batch **$0.017** — cheapest anywhere) |
| **Azure Speech** | std + MAI-Transcribe-1.5 (FLEURS SOTA claim, 43 langs) | ~140 locales | Y | add-on $0.30/hr | $0.18 (fast $0.36) → $1.00 |
| **Amazon Transcribe** | (unnamed foundation model) | 100+ batch / 54 streaming | Y | **included** | $0.36 → $0.60 (now flat, tiers gone) |
| **Speechmatics** | Ursa 2 (Enhanced/Standard), Melia 1 (code-switch, batch) | **55+** both modes | Y (<1 s) | **included free** | $0.24 (Melia) – $0.75 → $0.45–0.80; 50 free hrs/mo |
| **Groq** | `whisper-large-v3(-turbo)` | ~99 | N (228× RT batch) | N | **$0.04–0.11** |
| **Mistral Voxtral** | Transcribe 2 batch + `Voxtral-Mini-4B-Realtime` (open, Apache) | 13 | Y (240 ms–2.4 s, self-hostable) | **included** (batch) | $0.06–0.18 → $0.36 (or $0 self-host) |
| **ElevenLabs** | `scribe-v2` (~2.2% WER, top of AA index) | 90+ | Y (~150 ms) | included | $0.22 → $0.39 |
| **xAI** | Grok STT (+ Voice Agent API $0.05/min) | — | Y (realtime) | — | $0.10 → $0.20 (secondary-source rates) |
| **Open/local** | faster-whisper/WhisperX; Qwen3-ASR, Kyutai (natively streaming) | 52–99 | Kyutai/Qwen3: Y | WhisperX pipeline | ~$0.002–0.01 GPU compute |

**STT long-tail lane: OpenRouter** shipped `/api/v1/audio/transcriptions`
(May 2026) — OpenAI-compatible, provider pass-through pricing, same key as our
chat fallback lane (~8 STT models: whisper-v3 $0.09/hr, GPT-4o-transcribe,
Chirp 3, Voxtral, MAI-Transcribe, Parakeet, Qwen3-ASR). **Batch only** (no
WS): realtime streaming must stay direct-to-provider. Cost floor: DeepInfra
(whisper-v3-**turbo $0.012/hr**, Voxtral, streaming Nemotron) and Groq
($0.04/hr turbo, 228× RT) — both OpenAI-SDK-compatible via `base_url` swap.

**Chat models with NATIVE audio input:** all current Gemini models (audio-in
at chat prices ≈ cheaper than dedicated ASR), OpenAI `gpt-audio`/`gpt-realtime`
family, Mistral Voxtral. **Claude still takes no audio** (mid-2026) — Claude
conversations always need the STT hop.

---

## 3. Image generation

Normalized to **$/image at ~1024×1024 standard quality**.

| Provider | Flagship models | Strengths | Editing | ~$/image | Gating |
|---|---|---|---|---|---|
| **OpenAI** | `gpt-image-2` (flagship), `gpt-image-1.5` (#1 Arena T2I), `gpt-image-1-mini` | Instruction-following, multi-turn editing, text rendering | Y (edits, masks, multi-ref) | $0.053 / **$0.034** / $0.006–0.009 | **Org verification** + Tier-1 = 5 images/min |
| **Google** | `gemini-3-pro-image` (NB Pro), `gemini-3.1-flash-image` (NB2), `imagen-4` (fast/std/ultra) | **Chat-native generation + editing**, character consistency (≤14 ref images), text rendering (NB Pro) | Y (conversational) | NB2 ~$0.05 · NB Pro $0.134 · **Imagen-4-fast $0.02** | None |
| **Black Forest Labs** | `flux-2-pro/-flex/-max/-klein` (klein open-weight Apache) | Photorealism/prompt-adherence frontier of independents; LoRA | Y (all endpoints + Fill/Outpaint) | **pro $0.03** · max $0.07 · klein $0.014 | Async API (submit/poll) |
| **xAI** | `grok-imagine-image` (+quality, /edit) | Fast, very cheap | Y (multi-image edit) | **$0.02** (quality ~$0.07) | None |
| **Ideogram** | `ideogram-4.0` (turbo/default/quality) | **Text-rendering leader** (90–95% claim), logos, transparent BG, layers | Y | $0.03 / $0.06 / $0.10 | 10 in-flight default |
| **Recraft** | `recraft-v4.1(-pro)` | **Native SVG vectors**, exact brand colors, long text | Y (+vectorize) | $0.035 raster · **$0.08 SVG** | Prepaid units |
| **Stability** | SD3.5 family (no SD4 — stagnant) | Open weights, self-host | Y (tools $0.05) | $0.03–0.08 | Skip direct; via aggregator |
| ~~Midjourney~~ | v8 (app only) | Aesthetics leader | in-app | — | **Still NO official API** — exclude (3rd-party bridges violate ToS) |
| **Leonardo** | Lucid Origin/Realism | Game/marketing assets | Y | credit-based (opaque) | Via aggregator if ever needed |

**Image long-tail lane: fal.ai** — ~200+ T2I endpoints, one key, **zero markup
on partner models** (NB $0.0398 vs Google $0.039; FLUX at BFL list): covers
FLUX.2 + LoRA training, Ideogram v4, Recraft v4, Grok Imagine, Seedream v5,
Qwen-Image, SDXL, plus fallback copies of gpt-image-2 and the Gemini image
models (useful when OpenAI's org-verification/rate-tiers bite). Replicate =
deeper community tail (50k Cog models, per-GPU-second) but cold starts; Together
= one OpenAI-compatible key but narrower.

**Chat models with NATIVE image output (mid-2026):** only Google's Gemini
image line (`gemini-2.5-flash-image`, `gemini-3.1-flash-image`,
`gemini-3-pro-image`) emits image tokens from the chat surface. OpenAI's
mainline models (GPT-5.6 family) do NOT — the Responses `image_generation`
tool **delegates** to the GPT Image family (tool picks the backing model;
pricing = mainline text tokens + GPT-Image token rates). So even "native"
OpenAI image output is architecturally a fill — which validates ours.

---

## 4. Cross-cutting conclusions (feed PHASE7)

1. **Defaults (no new vendors, keys already provisioned):** TTS
   `openai:gpt-4o-mini-tts`; STT batch `openai:gpt-4o-mini-transcribe`
   ($0.18/hr); image `google:gemini-3.1-flash-image` ($0.05, no verification
   friction; cost floor `google:imagen-4-fast` $0.02).
2. **Pickable set (one new key each, added when a product wants the lane):**
   ElevenLabs (premium voices + cloning), Cartesia sonic-3.5 (agent latency),
   Deepgram nova-3/flux (STT price-performance + streaming), AssemblyAI
   (accuracy + Sync API), gpt-image-1.5/2 (quality ceiling), FLUX.2-pro,
   Ideogram 4 (typography), Recraft v4 (SVG/brand).
3. **Aggregator lane: fal.ai is the OpenRouter-of-media** — one key covers the
   TTS *and* image long tails at direct-price parity (their margin is hosted
   GPU models, not markup). OpenRouter's new audio endpoints cover batch-STT
   long tail on the key we already hold. Neither streams TTS/STT realtime —
   realtime stays direct-to-provider.
4. **Three API shapes cover everything** (adapter design): OpenAI-style sync
   REST (OpenAI, xAI, Recraft, Together, Groq, DeepInfra — often literally the
   `openai` SDK with `base_url`), Gemini `generateContent` (token-billed
   media), and queue/poll async (BFL, fal, Replicate). One async-capable
   internal port with a sync fast-path fits all three.
5. **Billing units are heterogeneous by design**: per-token (OpenAI/Gemini
   media tokens), per-character (most TTS), per-audio-minute (STT, realtime),
   per-image, per-megapixel (BFL). The ledger's `kind` + per-unit rate fields
   must carry the unit, not assume tokens.
6. **Exclusions:** Midjourney (no API), PlayHT (defunct), Stability-direct
   (stagnant), non-commercial open weights (XTTS-v2, F5-TTS, OpenAudio).
7. **Verification flags:** OpenAI gpt-image needs org verification + has low
   starting rate tiers; Azure CNV / OpenAI custom voices are access-gated;
   several Gemini/Hume/MiniMax flagships are still "preview"-labeled; a few
   numbers (Nova Sonic rates, Polly voice counts, Speechmatics per-model
   rates) could not be confirmed on official pages this pass.
