"""
Registry loader — the in-memory model index (Phase 3).

Reads the committed `data/models.json` (written by sync.py, never by runtime)
plus `overrides.json` (hand-pins that always win, per-field) once at import,
validates everything through the schema, and serves lookups from memory: a run
needs ZERO database or network round-trips to price or route a model.

Reload semantics are process-restart by design: new data ships like code (a
reviewed PR + deploy), so prod pricing can never mutate silently underneath a
running process (D6/D31).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from .schema import ModelInfo


logger = logging.getLogger(__name__)

_DATA_PATH = Path(__file__).parent / "data" / "models.json"
_OVERRIDES_PATH = Path(__file__).parent / "overrides.json"

# The committed seed models are also baked here as a last-resort floor, so an
# empty/corrupt data file can never leave the engine without its core models:
_FLOOR: dict[str, dict] = {
    "openai:gpt-5.2": {"id": "openai:gpt-5.2", "provider": "openai", "context_window": 400_000,
                       "max_output_tokens": 128_000, "tokenizer_family": "openai", "supports_thinking": True},
    "anthropic:claude-sonnet-4-6": {"id": "anthropic:claude-sonnet-4-6", "provider": "anthropic",
                                    "context_window": 200_000, "max_output_tokens": 64_000,
                                    "tokenizer_family": "anthropic", "supports_thinking": True},
    "anthropic:claude-opus-4-6": {"id": "anthropic:claude-opus-4-6", "provider": "anthropic",
                                  "context_window": 200_000, "max_output_tokens": 64_000,
                                  "tokenizer_family": "anthropic", "supports_thinking": True},
    "anthropic:claude-haiku-4-5": {"id": "anthropic:claude-haiku-4-5", "provider": "anthropic",
                                   "context_window": 200_000, "max_output_tokens": 32_000,
                                   "tokenizer_family": "anthropic"},
    "google:gemini-3-pro-preview": {"id": "google:gemini-3-pro-preview", "provider": "google",
                                    "context_window": 1_000_000, "max_output_tokens": 64_000,
                                    "tokenizer_family": "google", "supports_thinking": True},
    "google:gemini-3-flash-preview": {"id": "google:gemini-3-flash-preview", "provider": "google",
                                      "context_window": 1_000_000, "max_output_tokens": 64_000,
                                      "tokenizer_family": "google"},
    "xai:grok-4.3": {"id": "xai:grok-4.3", "provider": "xai", "context_window": 256_000,
                     "max_output_tokens": 64_000, "tokenizer_family": "xai", "supports_thinking": True},
}


def _read_json(path: Path) -> dict:
    """Reading a JSON file defensively — a missing/corrupt file yields {} plus a warning:"""
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return {}
    except Exception:  # noqa: BLE001 — a broken data file must not kill app boot:
        logger.warning("registry: failed to parse %s; ignoring", path)
        return {}


def _merge_entry(base: dict, extra: dict) -> dict:
    """Merging override fields onto a base entry (per-field; nested dicts merge one level):"""
    merged = dict(base)
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = {**merged[key], **value}
        else:
            merged[key] = value
    return merged


def _build_index() -> dict[str, ModelInfo]:
    """
    Building the in-memory index at import: floor <- data file <- overrides
    (later layers win per-field). Entries that fail schema validation are
    skipped with a warning rather than failing boot:
    """
    raw: dict[str, dict] = dict(_FLOOR)

    data = _read_json(_DATA_PATH)
    for model_id, entry in data.get("models", {}).items():
        raw[model_id] = _merge_entry(raw.get(model_id, {}), entry)

    overrides = _read_json(_OVERRIDES_PATH)
    for model_id, entry in overrides.items():
        if model_id.startswith("_"):
            continue  # Skipping doc/meta keys (JSON has no comments):
        entry = {**entry, "source": "overrides"}
        raw[model_id] = _merge_entry(raw.get(model_id, {"id": model_id, "provider": model_id.split(":")[0]}), entry)

    index: dict[str, ModelInfo] = {}
    for model_id, entry in raw.items():
        try:
            entry.setdefault("id", model_id)
            index[model_id] = ModelInfo.model_validate(entry)
        except Exception:  # noqa: BLE001 — one bad entry must not sink the rest:
            logger.warning("registry: invalid entry for %s; skipped", model_id)
    return index


# Building once at import (process-restart reload semantics):
_INDEX: dict[str, ModelInfo] = _build_index()


def get_model_info(model_id: str) -> ModelInfo:
    """
    Returning info for a canonical "provider:model" id. Unknown ids resolve to
    a conservative default keyed on the provider prefix — a brand-new model
    still runs day-one (Pydantic AI resolves the string) with safe window
    assumptions and an UNPRICED cost (P4 applies the pessimistic policy):
    """
    hit = _INDEX.get(model_id)
    if hit is not None:
        return hit

    provider = model_id.split(":", 1)[0] if ":" in model_id else "openai"
    # Defaulting conservatively — a small assumed window never over-fills a real
    # model, it only summarizes a bit sooner than strictly necessary:
    return ModelInfo(id=model_id, provider=provider, tokenizer_family=provider, source="default")


def all_models() -> list[ModelInfo]:
    """Returning every registry entry (stable order by id) for query/reporting:"""
    return [_INDEX[k] for k in sorted(_INDEX)]


def registry_size() -> int:
    """Returning how many models the loaded registry holds (health/report use):"""
    return len(_INDEX)
