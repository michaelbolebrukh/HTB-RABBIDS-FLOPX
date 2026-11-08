"""
LiteLLM feed adapter (Phase 3) —
https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json

The de-facto industry price table (~2,900 entries, community-maintained by PR).
This feed's strength is COST coverage and richness (cache read/write, reasoning
tokens). IMPORTANT UNIT NOTE: LiteLLM prices are USD per SINGLE token
(`input_cost_per_token`); we convert to USD per Mtok (x 1_000_000) here, once.

Keys look like "gpt-5.2" (provider implied), "anthropic/claude-...",
"gemini/gemini-...", "xai/grok-..."; the `litellm_provider` field is the
authority on which provider a key belongs to.
"""

from __future__ import annotations

from typing import Any


# Mapping litellm_provider values onto our canonical prefixes (direct providers only):
_PROVIDER_MAP = {
    "openai": "openai",
    "anthropic": "anthropic",
    "gemini": "google",
    "vertex_ai-language-models": "google",
    "xai": "xai",
}

_PER_TOKEN_TO_PER_MTOK = 1_000_000


def _mtok(value: Any) -> float | None:
    """Converting a per-token price to per-Mtok (None passes through):"""
    return float(value) * _PER_TOKEN_TO_PER_MTOK if value is not None else None


def _canonical_name(key: str) -> str:
    """Stripping any 'vendor/' prefix from a LiteLLM key to get the bare model name:"""
    return key.split("/", 1)[1] if "/" in key else key


def parse(payload: dict[str, Any]) -> dict[str, dict]:
    """Mapping the LiteLLM pricing JSON into partial registry entries:"""
    out: dict[str, dict] = {}

    for key, m in payload.items():
        if not isinstance(m, dict) or key == "sample_spec":
            continue
        # Only chat-mode models matter to the engine (embeddings etc. come later):
        if m.get("mode") not in (None, "chat", "responses"):
            continue

        provider = _PROVIDER_MAP.get(str(m.get("litellm_provider") or ""))
        if provider is None:
            continue

        canonical = f"{provider}:{_canonical_name(key)}"
        entry: dict[str, Any] = {"id": canonical, "provider": provider, "source": "litellm"}

        # Window/output limits:
        if m.get("max_input_tokens"):
            entry["context_window"] = int(m["max_input_tokens"])
        if m.get("max_output_tokens"):
            entry["max_output_tokens"] = int(m["max_output_tokens"])

        # Costs — converted per-token -> per-Mtok exactly once, here:
        entry["cost"] = {
            "input": _mtok(m.get("input_cost_per_token")),
            "output": _mtok(m.get("output_cost_per_token")),
            "cache_read": _mtok(m.get("cache_read_input_token_cost")),
            "cache_write": _mtok(m.get("cache_creation_input_token_cost")),
            "reasoning": _mtok(m.get("output_cost_per_reasoning_token")),
        }

        # Capability flags (secondary to models.dev but wide coverage):
        entry["capabilities"] = {
            "tool_call": bool(m.get("supports_function_calling", True)),
            "structured_output": bool(m.get("supports_response_schema", True)),
            "reasoning": bool(m.get("supports_reasoning", False)),
            "vision": bool(m.get("supports_vision", False)),
            "web_search": bool(m.get("supports_web_search", False)),
            "prompt_caching": bool(m.get("supports_prompt_caching", False)),
            "parallel_tool_calls": bool(m.get("supports_parallel_function_calling", False)),
        }

        entry["tokenizer_family"] = provider
        out[canonical] = entry

    return out
