"""
models.dev feed adapter (Phase 3) — https://models.dev/api.json

models.dev is an MIT-licensed, community-maintained model database (used in
production by opencode). Its api.json is keyed by provider, each holding a
`models` map. This feed is our best source for CAPABILITY booleans and
lifecycle data; costs come in USD per Mtok already (our unit).

Adapter contract (all feeds): parse(payload) -> dict[canonical_id, entry_dict]
where entry_dict is a partial ModelInfo-shaped dict. Adapters are defensive —
a malformed model entry is skipped, never fatal.
"""

from __future__ import annotations

from typing import Any


# Mapping models.dev provider keys onto our canonical provider prefixes. Only
# providers we reach DIRECTLY are mapped; everything else is reachable through
# the OpenRouter feed instead:
_PROVIDER_MAP = {
    "openai": "openai",
    "anthropic": "anthropic",
    "google": "google",
    "xai": "xai",
}


def parse(payload: dict[str, Any]) -> dict[str, dict]:
    """Mapping a models.dev api.json payload into partial registry entries:"""
    out: dict[str, dict] = {}

    for feed_provider, provider_blob in payload.items():
        provider = _PROVIDER_MAP.get(feed_provider)
        if provider is None or not isinstance(provider_blob, dict):
            continue

        for model_key, m in (provider_blob.get("models") or {}).items():
            if not isinstance(m, dict):
                continue
            canonical = f"{provider}:{model_key}"

            entry: dict[str, Any] = {"id": canonical, "provider": provider, "source": "models_dev"}

            # Context/output limits:
            limit = m.get("limit") or {}
            if limit.get("context"):
                entry["context_window"] = int(limit["context"])
            if limit.get("output"):
                entry["max_output_tokens"] = int(limit["output"])

            # Costs are already USD per Mtok in this feed:
            cost = m.get("cost") or {}
            entry["cost"] = {
                "input": cost.get("input"),
                "output": cost.get("output"),
                "cache_read": cost.get("cache_read"),
                "cache_write": cost.get("cache_write"),
            }

            # Capability booleans (this feed's strength):
            modalities = m.get("modalities") or {}
            entry["capabilities"] = {
                "tool_call": bool(m.get("tool_call", True)),
                "reasoning": bool(m.get("reasoning", False)),
                "structured_output": bool(m.get("structured_output", True)),
                "vision": "image" in (modalities.get("input") or []),
            }
            if modalities:
                entry["modalities"] = {
                    "input": list(modalities.get("input") or ["text"]),
                    "output": list(modalities.get("output") or ["text"]),
                }

            # Lifecycle/cutoff metadata:
            if m.get("status"):
                entry["lifecycle"] = str(m["status"])
            if m.get("knowledge"):
                entry["knowledge_cutoff"] = str(m["knowledge"])

            entry["tokenizer_family"] = provider
            out[canonical] = entry

    return out
