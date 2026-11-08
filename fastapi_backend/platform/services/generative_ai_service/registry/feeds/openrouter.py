"""
OpenRouter feed adapter (Phase 3) — https://openrouter.ai/api/v1/models

A LIVE API from a company whose billing depends on these numbers (freshest
source for price/context). Each model contributes TWO kinds of entries:

1. `openrouter:<vendor/model>` — the model as reachable THROUGH OpenRouter,
   with OpenRouter's own pricing. This matters for D28: fallback traffic is
   billed at OpenRouter's rates, not the direct provider's, so the fallback
   lane gets its own correct price.
2. For the big-four vendors, a mapped DIRECT entry (`openai:...`, `xai:...`)
   used as the lowest-priority base layer for direct ids (models.dev/LiteLLM
   override it where they know better).

UNIT NOTE: OpenRouter pricing values are USD per SINGLE token as strings;
converted to USD per Mtok here, once.
"""

from __future__ import annotations

from typing import Any


# Mapping OpenRouter vendor slugs onto our canonical direct providers:
_VENDOR_MAP = {
    "openai": "openai",
    "anthropic": "anthropic",
    "google": "google",
    "x-ai": "xai",
}

_PER_TOKEN_TO_PER_MTOK = 1_000_000


def _mtok(value: Any) -> float | None:
    """Converting a per-token string/number price to per-Mtok (None/'0' handled):"""
    if value in (None, "", "0", 0):
        return 0.0 if value in ("0", 0) else None
    try:
        return float(value) * _PER_TOKEN_TO_PER_MTOK
    except (TypeError, ValueError):
        return None


def _entry_from(m: dict[str, Any], canonical: str, provider: str) -> dict:
    """Building one partial registry entry from an OpenRouter model record:"""
    entry: dict[str, Any] = {"id": canonical, "provider": provider, "source": "openrouter"}

    if m.get("context_length"):
        entry["context_window"] = int(m["context_length"])
    top = m.get("top_provider") or {}
    if top.get("max_completion_tokens"):
        entry["max_output_tokens"] = int(top["max_completion_tokens"])

    pricing = m.get("pricing") or {}
    entry["cost"] = {
        "input": _mtok(pricing.get("prompt")),
        "output": _mtok(pricing.get("completion")),
        "cache_read": _mtok(pricing.get("input_cache_read")),
        "cache_write": _mtok(pricing.get("input_cache_write")),
        "reasoning": _mtok(pricing.get("internal_reasoning")),
        "per_request": _mtok(pricing.get("request")),
    }

    arch = m.get("architecture") or {}
    input_mods = list(arch.get("input_modalities") or ["text"])
    entry["modalities"] = {
        "input": input_mods,
        "output": list(arch.get("output_modalities") or ["text"]),
    }

    supported = set(m.get("supported_parameters") or [])
    entry["capabilities"] = {
        "tool_call": "tools" in supported,
        "structured_output": "structured_outputs" in supported or "response_format" in supported,
        "reasoning": "reasoning" in supported or "include_reasoning" in supported,
        "vision": "image" in input_mods,
        "parallel_tool_calls": "parallel_tool_calls" in supported,
    }

    # Tokenizer family: the vendor's own family for mapped entries; OpenRouter
    # reports a tokenizer group we lowercase as a reasonable family key:
    tokenizer = (arch.get("tokenizer") or provider or "generic")
    entry["tokenizer_family"] = provider if provider in _VENDOR_MAP.values() else str(tokenizer).lower()
    return entry


def parse(payload: dict[str, Any]) -> dict[str, dict]:
    """Mapping an OpenRouter /models payload into partial registry entries:"""
    out: dict[str, dict] = {}

    for m in payload.get("data") or []:
        if not isinstance(m, dict) or not m.get("id"):
            continue
        slug = str(m["id"])  # e.g. "anthropic/claude-sonnet-4-6"

        # 1. The through-OpenRouter entry (fallback-lane pricing, D28):
        out[f"openrouter:{slug}"] = _entry_from(m, f"openrouter:{slug}", "openrouter")

        # 2. A mapped direct entry for big-four vendors (base layer only):
        vendor = slug.split("/", 1)[0] if "/" in slug else ""
        provider = _VENDOR_MAP.get(vendor)
        if provider:
            name = slug.split("/", 1)[1]
            canonical = f"{provider}:{name}"
            direct = _entry_from(m, canonical, provider)
            # Direct pricing differs from OpenRouter's; keeping context/caps but
            # letting higher-priority feeds own the direct cost when they can.
            out[canonical] = direct

    return out
