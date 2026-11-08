"""
Provider construction tests: key resolution, D28 fallback at construction and
runtime, blank-key handling. No network — models are built, never called.
"""

import pytest

from platform.services.generative_ai_service.engine import providers
from platform.services.generative_ai_service.engine.model_info import get_model_info, quantize_budget


@pytest.fixture(autouse=True)
def _keys(monkeypatch):
    """Setting a controlled key environment on the settings object per test:"""
    from infrastructure.settings import settings
    monkeypatch.setattr(settings, "OPENAI_API_KEY", "sk-openai", raising=False)
    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", None, raising=False)
    monkeypatch.setattr(settings, "GOOGLE_API_KEY", "  ", raising=False)   # Blank = missing
    monkeypatch.setattr(settings, "XAI_API_KEY", None, raising=False)
    monkeypatch.setattr(settings, "OPENROUTER_API_KEY", "sk-or", raising=False)


def test_direct_provider_with_key_is_wrapped_with_fallback():
    from pydantic_ai.models.fallback import FallbackModel
    built = providers.build_model("openai:gpt-5.2")
    assert built.served_by == "openai"
    assert built.switched_reason is None
    # OpenRouter fallback is available, so the model is a FallbackModel:
    assert isinstance(built.model, FallbackModel)


def test_missing_key_falls_back_to_openrouter_at_construction():
    built = providers.build_model("anthropic:claude-sonnet-4-6")
    assert built.served_by == "openrouter"
    assert "missing" in built.switched_reason


def test_blank_key_is_treated_as_missing():
    # Google key is whitespace -> construction fallback to OpenRouter:
    built = providers.build_model("google:gemini-3-pro-preview")
    assert built.served_by == "openrouter"


def test_missing_key_and_no_openrouter_raises(monkeypatch):
    from infrastructure.settings import settings
    monkeypatch.setattr(settings, "OPENROUTER_API_KEY", None, raising=False)
    with pytest.raises(ValueError, match="No API key"):
        providers.build_model("anthropic:claude-sonnet-4-6")


def test_explicit_openrouter_model_needs_no_fallback():
    built = providers.build_model("openrouter:meta-llama/llama-3.3-70b-instruct")
    assert built.served_by == "openrouter"


def test_model_info_seed_and_defaults():
    # The registry (P3) carries live-synced windows; asserting the sane range
    # rather than an exact number so feed updates don't break the golden test:
    info = get_model_info("google:gemini-3-pro-preview")
    assert info.context_window >= 1_000_000 and info.provider == "google"
    assert info.tokenizer_family == "google"
    # Unknown model resolves to conservative defaults keyed on the provider prefix:
    unknown = get_model_info("anthropic:claude-9-future")
    assert unknown.provider == "anthropic" and unknown.context_window > 0


def test_budget_quantization_buckets():
    assert quantize_budget(1500) == 2000
    assert quantize_budget(3000) == 4000
    assert quantize_budget(999_999) == 32_000
