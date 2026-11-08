"""
Registry sync — the OFFLINE job that regenerates data/models.json (Phase 3).

Runtime never writes the registry; this script does, and only on an operator's
or scheduler's invocation. Flow: fetch the three feeds -> parse via adapters ->
merge with documented per-field priority -> write a DETERMINISTIC json file
(sorted keys, stable formatting) so a git diff of a sync is human-reviewable.
The reconciler wraps this: run, and if the file changed, open a PR (D6) —
never commit to main unattended.

Merge priority (later wins per-field): openrouter < litellm < models_dev <
overrides (overrides applied at LOAD time by loader.py, not baked into the
file). A feed that fails is skipped with a warning — partial sync beats no
sync (D31: only a total feed failure on schedule is Sentry-worthy).

Run: cd fastapi_backend && python -m platform.services.generative_ai_service.registry.sync
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import date
from pathlib import Path
from typing import Any, Callable

import httpx

from .feeds import litellm, models_dev, openrouter


logger = logging.getLogger(__name__)

_DATA_PATH = Path(__file__).parent / "data" / "models.json"

# Feed registry: name -> (url, adapter, merge priority — higher wins per-field):
_FEEDS: list[tuple[str, str, Callable[[dict], dict[str, dict]], int]] = [
    ("openrouter", "https://openrouter.ai/api/v1/models", openrouter.parse, 1),
    ("litellm",
     "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json",
     litellm.parse, 2),
    ("models_dev", "https://models.dev/api.json", models_dev.parse, 3),
]

# Sanity bounds — an entry violating these is dropped and reported rather than
# poisoning the registry (fat-fingered feed data happens):
_MAX_SANE_COST_PER_MTOK = 5_000.0
_MIN_SANE_CONTEXT = 1_000


def _fetch(url: str) -> dict | None:
    """Fetching one feed with a hard timeout; None (not an exception) on failure:"""
    try:
        response = httpx.get(url, timeout=60, follow_redirects=True)
        response.raise_for_status()
        return response.json()
    except Exception as exc:  # noqa: BLE001 — a dead feed must not kill the sync:
        logger.warning("registry sync: feed %s failed: %r", url, exc)
        return None


def _sane(entry: dict) -> bool:
    """Applying sanity bounds so absurd feed values never enter the registry:"""
    cost = entry.get("cost") or {}
    for dim in ("input", "output"):
        value = cost.get(dim)
        if value is not None and (value < 0 or value > _MAX_SANE_COST_PER_MTOK):
            return False
    window = entry.get("context_window")
    if window is not None and window < _MIN_SANE_CONTEXT:
        return False
    return True


def _merge_field_wise(layers: list[dict[str, dict]]) -> dict[str, dict]:
    """
    Merging feed layers per-field: for each model id, later layers overwrite
    the fields they actually carry (None values do NOT overwrite real data —
    a feed that lacks a price must not erase another feed's price):
    """
    merged: dict[str, dict] = {}
    for layer in layers:
        for model_id, entry in layer.items():
            base = merged.setdefault(model_id, {})
            for key, value in entry.items():
                if isinstance(value, dict) and isinstance(base.get(key), dict):
                    # One-level nested merge where None never clobbers data:
                    for sub_key, sub_value in value.items():
                        if sub_value is not None or sub_key not in base[key]:
                            base[key][sub_key] = sub_value
                elif value is not None or key not in base:
                    base[key] = value
    return merged


def run_sync(write: bool = True) -> dict[str, Any]:
    """
    Running a full sync. Returns a report dict (feeds fetched, model counts,
    dropped entries, disagreements on actively-tracked fields). Writes the
    deterministic data file when `write` is True:
    """
    report: dict[str, Any] = {"feeds": {}, "dropped": [], "total": 0}

    # Fetching + parsing in priority order (lowest first so later wins):
    layers: list[dict[str, dict]] = []
    for name, url, adapter, _priority in sorted(_FEEDS, key=lambda f: f[3]):
        payload = _fetch(url)
        if payload is None:
            report["feeds"][name] = "FAILED"
            continue
        try:
            parsed = adapter(payload)
        except Exception as exc:  # noqa: BLE001 — adapter bugs surface in the report:
            logger.warning("registry sync: adapter %s failed: %r", name, exc)
            report["feeds"][name] = f"ADAPTER_ERROR: {exc!r}"
            continue
        layers.append(parsed)
        report["feeds"][name] = len(parsed)

    merged = _merge_field_wise(layers)

    # Dropping insane entries (reported, never silent):
    clean: dict[str, dict] = {}
    for model_id, entry in merged.items():
        if _sane(entry):
            entry["synced_at"] = date.today().isoformat()
            clean[model_id] = entry
        else:
            report["dropped"].append(model_id)

    report["total"] = len(clean)

    if write and clean:
        _DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
        # Writing deterministically — sorted keys, stable indent — so the git
        # diff of a sync is a reviewable list of actual changes:
        with open(_DATA_PATH, "w", encoding="utf-8") as fh:
            json.dump({"models": {k: clean[k] for k in sorted(clean)}}, fh, indent=1, sort_keys=True)
            fh.write("\n")
        logger.info("registry sync: wrote %d models to %s", len(clean), _DATA_PATH)

    return report


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    result = run_sync()
    print(json.dumps({k: v for k, v in result.items() if k != "dropped"}, indent=2))
    print(f"dropped: {len(result['dropped'])}")
    sys.exit(0 if result["total"] > 0 else 1)
