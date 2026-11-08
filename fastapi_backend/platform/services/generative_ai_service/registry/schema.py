"""
Registry schema — the superset ModelInfo (Phase 3).

Extends P2's ModelInfo with cost, capability, modality, and lifecycle data
WITHOUT changing any existing field or its default, so every P2 caller
(renderer window math, providers, adapter output-mode choice) is untouched.
The old boolean convenience fields (`supports_thinking`,
`supports_structured_output`) are kept in lockstep with the structured
`capabilities` object by a validator, so old and new call sites always agree.

No `pydantic_ai` and no ORM imports — the registry is pure data our layer owns.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, model_validator


class Cost(BaseModel):
    """
    USD per MILLION tokens for each billing dimension (matching the units used
    by models.dev and, after conversion, LiteLLM/OpenRouter). None = the feed
    did not report that dimension; the P4 pricer treats a fully-None cost as
    "unpriced" and applies the pessimistic policy.
    """
    input: float | None = None
    output: float | None = None
    cache_read: float | None = None
    cache_write: float | None = None
    reasoning: float | None = None        # Only some providers bill reasoning separately
    input_audio: float | None = None
    output_audio: float | None = None
    # Per-unit frontier-feature costs (not per-token; P4 prices these later):
    web_search_per_call: float | None = None
    per_request: float | None = None
    # Media billing units (P7, D44) — media providers bill in characters,
    # images, or audio minutes rather than tokens. Field names carry the unit
    # explicitly so a rate can never be misread against the wrong quantity:
    per_million_characters: float | None = None   # USD per 1M input characters (TTS)
    per_image: float | None = None                # USD per generated image (~1MP standard)
    per_audio_minute: float | None = None         # USD per audio minute (STT/realtime)

    def is_priced(self) -> bool:
        """Returning whether the core dimensions carry any real price data."""
        return self.input is not None and self.output is not None


class Modalities(BaseModel):
    """What the model accepts and produces (text/image/audio/video/pdf)."""
    input: list[str] = Field(default_factory=lambda: ["text"])
    output: list[str] = Field(default_factory=lambda: ["text"])


class Capabilities(BaseModel):
    """Structured capability flags (the registry's per-model feature switches)."""
    tool_call: bool = True
    structured_output: bool = True        # Native JSON-schema output mode (D20)
    reasoning: bool = False               # Extended thinking support
    vision: bool = False
    web_search: bool = False              # Provider-native web-search tool
    prompt_caching: bool = False
    parallel_tool_calls: bool = False
    # D34 native-vs-fill flags with no feed coverage yet — they default False
    # (conservative: our client-side fill runs) and are set per model via
    # registry overrides until a feed starts reporting them:
    web_fetch: bool = False               # Provider-native URL-fetch tool
    code_execution: bool = False          # Provider-native code interpreter
    # P6 flags, override-set like the two above:
    realtime: bool = False                # Native speech-to-speech session (D37 selection)
    image_generation: bool = False        # Native image-generation tool (D40, no fill)


class ModelInfo(BaseModel):
    """
    Everything the engine and the billing layer need to know about a model
    without a live API call. The first block is the frozen P2 contract; the
    second block is P3's additive data.
    """
    # --- P2 contract (field names, types, and defaults are frozen) ---
    id: str                                # Canonical "provider:model" (D17)
    provider: str                          # openai | anthropic | google | xai | openrouter
    context_window: int = 128_000
    max_output_tokens: int = 16_384
    tokenizer_family: str = "generic"      # Summary-cache key component (D24/O6.6)
    supports_structured_output: bool = True
    supports_thinking: bool = False

    # --- P3 additions (all optional/defaulted — invisible to P2 callers) ---
    cost: Cost = Field(default_factory=Cost)
    capabilities: Capabilities = Field(default_factory=Capabilities)
    modalities: Modalities = Field(default_factory=Modalities)
    lifecycle: str = "stable"              # alpha | beta | stable | deprecated
    knowledge_cutoff: str | None = None
    source: str = "seed"                   # Which layer last won: overrides|models_dev|litellm|openrouter|seed
    synced_at: str | None = None           # ISO date of the sync that produced this entry

    @model_validator(mode="after")
    def _sync_convenience_flags(self) -> "ModelInfo":
        """
        Keeping the flat P2 booleans and the structured capabilities in
        lockstep, whichever side a feed populated (the flat fields win when
        they differ from capability defaults, since overrides set those):
        """
        # Propagating flat -> structured when the flat side is the non-default one:
        if self.supports_thinking and not self.capabilities.reasoning:
            self.capabilities.reasoning = True
        if not self.supports_structured_output and self.capabilities.structured_output:
            self.capabilities.structured_output = False
        # Propagating structured -> flat (feeds populate capabilities):
        if self.capabilities.reasoning:
            self.supports_thinking = True
        if not self.capabilities.structured_output:
            self.supports_structured_output = False
        return self
