"""
Pricing — pure cost math (Phase 4). Usage x registry rates -> micro-USD.

No database, no SDK, no I/O: a deterministic function from (tokens, rates) to
integer micro-USD, unit-tested with golden values. Registry rates are USD per
MILLION tokens, which conveniently equals MICRO-USD PER TOKEN — so each
dimension's cost is simply `tokens x rate`, rounded.

Billing-dimension assumptions (documented, revisited when reconciling against
real provider invoices):
- `input_tokens` are priced at the input rate; `cache_read`/`cache_write`
  tokens at their own rates when the registry has them.
- `reasoning_tokens` are priced separately ONLY when the registry carries a
  reasoning rate; otherwise providers bill reasoning inside output tokens and
  adding it again would double-count.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..registry import ModelInfo
from ..transcript.types import Usage


@dataclass
class PriceResult:
    """The outcome of pricing one Usage: total + per-dimension breakdown."""
    cost_microusd: int | None            # None = unpriced and no pessimistic rates given
    priced: bool                         # False when pessimistic/default rates were used
    breakdown: dict[str, int] = field(default_factory=dict)


@dataclass
class PessimisticRates:
    """
    Fallback USD-per-Mtok rates for models the registry has no price for
    (the unpriced-model policy, PHASE4). Deliberately expensive so unknown
    models deplete budgets FASTER than reality, never slower:
    """
    input: float = 15.0
    output: float = 75.0


def price_usage(
    usage: Usage,
    info: ModelInfo,
    pessimistic: PessimisticRates | None = None,
) -> PriceResult:
    """
    Pricing a run's usage against a model's registry rates. Unpriced model +
    no pessimistic rates -> (None, priced=False); with pessimistic rates the
    cost is computed from those and flagged `priced=False`:
    """
    cost = info.cost

    # Choosing the rate set — real registry rates, or the pessimistic fallback:
    if cost.is_priced():
        rate_input, rate_output = cost.input, cost.output
        rate_cache_read, rate_cache_write = cost.cache_read, cost.cache_write
        rate_reasoning = cost.reasoning
        priced = True
    elif pessimistic is not None:
        rate_input, rate_output = pessimistic.input, pessimistic.output
        rate_cache_read = rate_cache_write = rate_reasoning = None
        priced = False
    else:
        return PriceResult(cost_microusd=None, priced=False)

    # USD-per-Mtok == micro-USD-per-token, so each dimension is tokens x rate:
    breakdown: dict[str, int] = {}
    breakdown["input"] = round(usage.input_tokens * rate_input)
    breakdown["output"] = round(usage.output_tokens * rate_output)

    if usage.cache_read_tokens and rate_cache_read is not None:
        breakdown["cache_read"] = round(usage.cache_read_tokens * rate_cache_read)
    if usage.cache_write_tokens and rate_cache_write is not None:
        breakdown["cache_write"] = round(usage.cache_write_tokens * rate_cache_write)
    # Reasoning is priced separately ONLY when a dedicated rate exists (else it
    # is already inside output tokens and would double-count — see module doc):
    if usage.reasoning_tokens and rate_reasoning is not None:
        breakdown["reasoning"] = round(usage.reasoning_tokens * rate_reasoning)

    # Audio dimensions (P6 voice): realtime/STT/TTS models bill audio tokens at
    # their own rates. Providers report audio tokens SEPARATELY from text
    # tokens, so this never double-counts; with no audio rate in the registry,
    # audio tokens fall back to the ordinary input/output rates (pessimistic
    # only when those are already the pessimistic set — never silently free):
    if usage.input_audio_tokens:
        rate = cost.input_audio if (priced and cost.input_audio is not None) else rate_input
        breakdown["input_audio"] = round(usage.input_audio_tokens * rate)
    if usage.output_audio_tokens:
        rate = cost.output_audio if (priced and cost.output_audio is not None) else rate_output
        breakdown["output_audio"] = round(usage.output_audio_tokens * rate)

    return PriceResult(cost_microusd=sum(breakdown.values()), priced=priced, breakdown=breakdown)


# ============================================================================
# MEDIA PRICING (P7, D44) — unit-billed calls (characters / images / minutes)
# ============================================================================

# Media units and where their rate lives on the registry Cost model. The unit
# string doubles as the breakdown key on the resulting PriceResult:
_MEDIA_UNIT_FIELDS = {
    "characters": "per_million_characters",   # Quantity = characters of input text
    "images": "per_image",                    # Quantity = images generated
    "audio_minutes": "per_audio_minute",      # Quantity = minutes of audio
}


@dataclass
class PessimisticMediaRates:
    """
    Fallback per-unit rates for media models the registry has no price for —
    the same deliberately-expensive posture as PessimisticRates (unknown
    models deplete budgets FASTER than reality, never slower). Anchored to
    the top of each verified market range (MEDIA_PROVIDERS.md):
    """
    per_million_characters: float = 200.0     # 2x ElevenLabs v3, the priciest mainstream TTS
    per_image: float = 0.30                   # Above NB Pro 4K / gpt-image-2 high
    per_audio_minute: float = 0.05            # ~3x OpenAI's realtime STT


def price_media(
    quantity: float,
    unit: str,
    info: ModelInfo,
    pessimistic: PessimisticMediaRates | None = None,
) -> PriceResult:
    """
    Pricing one unit-billed media call (TTS characters, generated images,
    transcribed audio minutes) against a model's registry rates. Mirrors
    `price_usage`: no registry rate + no pessimistic rates -> (None, False);
    with pessimistic rates the cost is computed from those, flagged unpriced.
    Token-billed media models (OpenAI TTS/STT report token usage) keep using
    `price_usage` — this function is only for calls whose provider bills by
    unit and reports no tokens:
    """
    field_name = _MEDIA_UNIT_FIELDS.get(unit)
    if field_name is None:
        raise ValueError(f"unknown media unit {unit!r}; expected one of {sorted(_MEDIA_UNIT_FIELDS)}")

    rate = getattr(info.cost, field_name)
    priced = rate is not None
    if not priced:
        if pessimistic is None:
            return PriceResult(cost_microusd=None, priced=False)
        rate = getattr(pessimistic, field_name)

    # per_million_characters is USD per MILLION units (token-table convention);
    # the other units are USD per single unit — normalize to USD per unit:
    per_unit_usd = rate / 1_000_000 if unit == "characters" else rate
    cost = round(quantity * per_unit_usd * 1_000_000)
    return PriceResult(cost_microusd=cost, priced=priced, breakdown={unit: cost})
