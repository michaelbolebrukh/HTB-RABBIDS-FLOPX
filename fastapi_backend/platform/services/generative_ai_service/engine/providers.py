"""
Provider construction — building Pydantic AI Model objects (Phase 2).

FACADE BOUNDARY (D3): this is one of exactly two modules allowed to import
`pydantic_ai` (the other is adapter.py). Everything above the engine sees only
our types; provider/SDK specifics are quarantined here.

Responsibilities:
- Map our canonical "provider:model" id onto the right Pydantic AI Model class,
  passing API keys explicitly from settings (D17), with OpenAI on the Responses
  API (D18).
- Apply the D28 OpenRouter fallback in both forms:
  * construction-time: a direct provider whose key is missing is built via
    OpenRouter instead (and the caller is told, so it can surface ModelSwitched);
  * runtime: the returned model is wrapped in FallbackModel(direct, openrouter)
    so auth/credit/quota failures roll over mid-call.
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic_ai.models import Model
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.models.fallback import FallbackModel
from pydantic_ai.models.google import GoogleModel
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIResponsesModel
from pydantic_ai.models.openrouter import OpenRouterModel
from pydantic_ai.providers.anthropic import AnthropicProvider
from pydantic_ai.providers.google import GoogleProvider
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.providers.openrouter import OpenRouterProvider

from infrastructure.settings import settings

from .model_info import get_model_info


# Mapping our provider prefix to the settings attribute holding its key:
_KEY_ATTR = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "google": "GOOGLE_API_KEY",
    "xai": "XAI_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
}

# Mapping OUR provider ids onto the model slug OpenRouter expects, so a fallback
# targets the same underlying model through OpenRouter's namespace:
_OPENROUTER_SLUG = {
    "openai": lambda name: f"openai/{name}",
    "anthropic": lambda name: f"anthropic/{name}",
    "google": lambda name: f"google/{name}",
    "xai": lambda name: f"x-ai/{name}",
}


@dataclass
class BuiltModel:
    """
    A constructed model plus the metadata the engine needs to report honestly:
    `served_by` is the provider that will actually handle the request after any
    construction-time fallback, and `switched_reason` is non-None when a
    fallback already happened at build time (surfaced as ModelSwitched, D28).
    """
    model: Model
    requested_id: str
    served_by: str
    switched_reason: str | None = None


def _api_key(provider: str) -> str | None:
    """
    Reading a provider's key from settings, normalizing a blank/whitespace
    value to None so an env var that is present-but-empty counts as missing
    (the D28 trigger) rather than being passed as an invalid empty key:
    """
    attr = _KEY_ATTR.get(provider)
    if not attr:
        return None
    value = getattr(settings, attr, None)
    if value is None or not value.strip():
        return None
    return value


def _split(model_id: str) -> tuple[str, str]:
    """
    Splitting a canonical id into (provider, model_name), rejecting a bare
    provider name with no model (e.g. "openai") rather than silently building a
    model with an empty name:
    """
    provider, sep, name = model_id.partition(":")
    if not sep or not provider or not name:
        raise ValueError(f"Invalid model id {model_id!r}; expected 'provider:model'")
    return provider, name


def _build_direct(provider: str, name: str) -> Model:
    """Constructing the native Model for a provider using its explicit key (D17/D18):"""
    key = _api_key(provider)
    if provider == "openai":
        # OpenAI on the Responses API by default so reasoning items round-trip (D18):
        return OpenAIResponsesModel(name, provider=OpenAIProvider(api_key=key))
    if provider == "anthropic":
        return AnthropicModel(name, provider=AnthropicProvider(api_key=key))
    if provider == "google":
        return GoogleModel(name, provider=GoogleProvider(api_key=key))
    if provider == "xai":
        # Reaching xAI directly through its OpenAI-compatible endpoint rather
        # than the dedicated xai-sdk: xai-sdk pins packaging>=25, which is
        # mutually incompatible with the rate limiter's limits<25 constraint,
        # and the OpenAI-compatible wire covers everything the engine needs:
        return OpenAIChatModel(
            name, provider=OpenAIProvider(base_url="https://api.x.ai/v1", api_key=key)
        )
    if provider == "openrouter":
        # An explicitly-requested OpenRouter model (long-tail models, D5/D27):
        return OpenRouterModel(name, provider=OpenRouterProvider(api_key=key))
    raise ValueError(f"Unknown provider: {provider!r} (model id must be 'provider:model')")


def _build_openrouter_equivalent(provider: str, name: str) -> Model | None:
    """
    Building the OpenRouter stand-in for a direct provider model (the D28
    fallback target). Returns None when we cannot map the model into
    OpenRouter's namespace or have no OpenRouter key:
    """
    if _api_key("openrouter") is None:
        return None
    slug_fn = _OPENROUTER_SLUG.get(provider)
    if slug_fn is None:
        return None
    return OpenRouterModel(slug_fn(name), provider=OpenRouterProvider(api_key=_api_key("openrouter")))


def build_model(model_id: str, fallback_ids: list[str] | None = None) -> BuiltModel:
    """
    Building the runnable model for a canonical id, applying D28 fallback.

    Construction-time fallback: a direct provider with no key is replaced by its
    OpenRouter equivalent, recorded in `switched_reason`. Runtime fallback: the
    primary is wrapped with the OpenRouter equivalent (and any spec-declared
    fallback ids) so mid-call auth/credit/quota errors roll over.
    """
    provider, name = _split(model_id)
    get_model_info(model_id)  # Validating the id resolves (raises nothing; seeds defaults):

    switched_reason: str | None = None
    served_by = provider

    # Construction-time fallback: no key for a direct provider -> OpenRouter:
    if provider != "openrouter" and _api_key(provider) is None:
        equivalent = _build_openrouter_equivalent(provider, name)
        if equivalent is None:
            raise ValueError(
                f"No API key for provider {provider!r} and no OpenRouter fallback available "
                f"(set {_KEY_ATTR.get(provider)} or OPENROUTER_API_KEY)"
            )
        primary: Model = equivalent
        served_by = "openrouter"
        switched_reason = f"{provider} key missing at construction; routed via OpenRouter"
    else:
        primary = _build_direct(provider, name)

    # Assembling the runtime fallback chain: primary, then OpenRouter equivalent
    # (unless the primary already IS openrouter), then any spec-declared ids:
    chain: list[Model] = [primary]
    if served_by != "openrouter":
        equivalent = _build_openrouter_equivalent(provider, name)
        if equivalent is not None:
            chain.append(equivalent)
    for fid in (fallback_ids or []):
        fprovider, fname = _split(fid)
        # Only adding a spec-declared fallback we can actually build — a
        # provider (including OpenRouter) with no key would raise a raw SDK
        # UserError at construction, so skip it silently instead:
        if _api_key(fprovider) is not None:
            chain.append(_build_direct(fprovider, fname))

    # Only wrapping when there is somewhere to fall back to (a bare model when not):
    model: Model = FallbackModel(*chain) if len(chain) > 1 else primary

    return BuiltModel(model=model, requested_id=model_id, served_by=served_by, switched_reason=switched_reason)
