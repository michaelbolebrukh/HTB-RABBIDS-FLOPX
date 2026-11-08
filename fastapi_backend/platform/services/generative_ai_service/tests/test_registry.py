"""
Registry tests (Phase 3): feed adapters against fixture payloads, per-field
merge priority, loader/override behavior, query filters, and the P2
back-compat contract. All offline — no live feed calls.
"""

from platform.services.generative_ai_service.registry import (
    ModelInfo,
    all_models,
    find_models,
    get_model_info,
    registry_size,
)
from platform.services.generative_ai_service.registry.feeds import litellm, models_dev, openrouter
from platform.services.generative_ai_service.registry.sync import _merge_field_wise, _sane


# ============================================================================
# FEED ADAPTERS (fixture payloads mirroring each feed's real shape)
# ============================================================================

def test_models_dev_adapter_maps_capabilities_and_mtok_costs():
    payload = {
        "anthropic": {"models": {"claude-test-1": {
            "limit": {"context": 200_000, "output": 64_000},
            "cost": {"input": 3.0, "output": 15.0, "cache_read": 0.3},
            "modalities": {"input": ["text", "image"], "output": ["text"]},
            "reasoning": True, "tool_call": True, "knowledge": "2025-05",
        }}},
        "unknown-provider": {"models": {"x": {}}},   # Unmapped providers are skipped:
    }
    out = models_dev.parse(payload)
    entry = out["anthropic:claude-test-1"]
    assert entry["context_window"] == 200_000
    assert entry["cost"]["input"] == 3.0            # Already per-Mtok in this feed
    assert entry["capabilities"]["reasoning"] is True
    assert entry["capabilities"]["vision"] is True  # Derived from image input modality
    assert entry["knowledge_cutoff"] == "2025-05"
    assert "unknown-provider:x" not in out


def test_litellm_adapter_converts_per_token_prices_to_mtok():
    payload = {
        "gpt-test": {"litellm_provider": "openai", "mode": "chat",
                     "max_input_tokens": 400_000, "max_output_tokens": 128_000,
                     "input_cost_per_token": 0.00000175, "output_cost_per_token": 0.000014,
                     "cache_read_input_token_cost": 0.000000175,
                     "supports_reasoning": True, "supports_prompt_caching": True},
        "gemini/gemini-test": {"litellm_provider": "gemini", "mode": "chat",
                               "input_cost_per_token": 0.0000005, "output_cost_per_token": 0.000003},
        "some/embedding": {"litellm_provider": "openai", "mode": "embedding"},  # Non-chat skipped:
    }
    out = litellm.parse(payload)
    gpt = out["openai:gpt-test"]
    # Per-token -> per-Mtok conversion (the unit trap this adapter owns):
    assert abs(gpt["cost"]["input"] - 1.75) < 1e-9
    assert abs(gpt["cost"]["cache_read"] - 0.175) < 1e-9
    assert gpt["capabilities"]["prompt_caching"] is True
    # Vendor-prefixed key maps to our canonical google id:
    assert "google:gemini-test" in out
    assert "openai:some/embedding" not in out and len(out) == 2


def test_openrouter_adapter_emits_both_direct_and_openrouter_entries():
    payload = {"data": [{
        "id": "anthropic/claude-test-1",
        "context_length": 200_000,
        "pricing": {"prompt": "0.000003", "completion": "0.000015"},
        "architecture": {"input_modalities": ["text", "image"], "output_modalities": ["text"],
                         "tokenizer": "Claude"},
        "supported_parameters": ["tools", "reasoning", "structured_outputs"],
        "top_provider": {"max_completion_tokens": 64_000},
    }]}
    out = openrouter.parse(payload)
    # The through-OpenRouter entry carries OpenRouter's own pricing (D28 fallback lane):
    via = out["openrouter:anthropic/claude-test-1"]
    assert via["provider"] == "openrouter" and abs(via["cost"]["input"] - 3.0) < 1e-9
    # The mapped direct entry exists as a base layer:
    direct = out["anthropic:claude-test-1"]
    assert direct["provider"] == "anthropic"
    assert direct["capabilities"]["reasoning"] is True


# ============================================================================
# MERGE + SANITY
# ============================================================================

def test_merge_priority_and_none_never_clobbers():
    lower = {"m:x": {"id": "m:x", "provider": "m", "context_window": 100,
                     "cost": {"input": 1.0, "output": 2.0}}}
    higher = {"m:x": {"id": "m:x", "provider": "m", "context_window": 200,
                      "cost": {"input": 5.0, "output": None}}}   # None must not erase 2.0
    merged = _merge_field_wise([lower, higher])["m:x"]
    assert merged["context_window"] == 200          # Later layer wins the field it carries
    assert merged["cost"]["input"] == 5.0
    assert merged["cost"]["output"] == 2.0          # None did not clobber real data


def test_sanity_bounds_drop_absurd_entries():
    assert _sane({"cost": {"input": 3.0, "output": 15.0}, "context_window": 200_000})
    assert not _sane({"cost": {"input": 99_999.0}, "context_window": 200_000})   # Absurd price
    assert not _sane({"cost": {}, "context_window": 10})                          # Absurd window


# ============================================================================
# LOADER + P2 BACK-COMPAT (the golden contract)
# ============================================================================

def test_loader_serves_core_models_with_costs():
    # The committed data file must cover the models the engine actually uses,
    # now WITH price data (what P4 depends on):
    for mid in ("openai:gpt-5.2", "anthropic:claude-sonnet-4-6", "openai:gpt-5-mini"):
        info = get_model_info(mid)
        assert isinstance(info, ModelInfo)
        assert info.context_window >= 100_000
        assert info.cost.is_priced(), f"{mid} must carry real prices"
    assert registry_size() > 100                     # The synced registry, not just the floor


def test_unknown_model_gets_conservative_unpriced_default():
    info = get_model_info("anthropic:claude-99-hypothetical")
    assert info.provider == "anthropic" and info.tokenizer_family == "anthropic"
    assert info.context_window == 128_000            # Conservative default window
    assert not info.cost.is_priced()                 # Unpriced -> P4 pessimistic policy


def test_convenience_flags_stay_in_lockstep():
    # capabilities.reasoning True must surface as supports_thinking True:
    info = ModelInfo(id="t:m", provider="t", capabilities={"reasoning": True})
    assert info.supports_thinking is True
    # And the flat flag propagates into capabilities:
    info2 = ModelInfo(id="t:m2", provider="t", supports_thinking=True)
    assert info2.capabilities.reasoning is True


# ============================================================================
# QUERY
# ============================================================================

def test_find_models_filters_and_orders_cheapest_first():
    hits = find_models(reasoning=True, min_context=200_000, priced_only=True)
    assert hits, "expect reasoning models with >=200k context in the synced registry"
    assert all(m.capabilities.reasoning and m.context_window >= 200_000 for m in hits)
    costs = [m.cost.input for m in hits]
    assert costs == sorted(costs)                    # Cheapest first

    cheap = find_models(max_input_cost=1.0, priced_only=True)
    assert all(m.cost.input <= 1.0 for m in cheap)


def test_find_models_provider_filter():
    only_anthropic = find_models(provider="anthropic")
    assert only_anthropic and all(m.provider == "anthropic" for m in only_anthropic)
