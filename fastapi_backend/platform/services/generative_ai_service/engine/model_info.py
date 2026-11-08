"""
Model info — the P2 contract, now registry-backed (Phase 3 shim).

Phase 2 callers import `ModelInfo`, `get_model_info`, `quantize_budget`,
`DEFAULT_SUMMARIZER_MODEL`, and `SUMMARY_BUDGET_BUCKETS` from this module.
Phase 3 moved the data source to the synced registry package; this module now
just re-exports so every existing import keeps working unchanged (the "swap
the backend, keep the signature" promise in PHASE3.md).

The hand-maintained seed table this module used to hold is gone — the registry
carries live-synced windows/costs/capabilities for ~600 models, with the same
conservative-default behavior for unknown ids and the same schema superset
(see registry/schema.py). No `pydantic_ai` import here (D3).
"""

from __future__ import annotations

from ..registry import ModelInfo, get_model_info  # noqa: F401 — re-exported P2 contract


# The cheap default summarizer (O6.1) — a small model, never the conversation model:
DEFAULT_SUMMARIZER_MODEL = "openai:gpt-5-mini"   # Real, priced in feeds (gpt-5.2-mini does not exist upstream)

# Summary-cache budget buckets (O6.6): models quantize their exact budget to one
# of these so similar-window models share cache rows instead of each minting one:
SUMMARY_BUDGET_BUCKETS = (2_000, 4_000, 8_000, 16_000, 32_000)


def quantize_budget(token_budget: int) -> int:
    """Snapping a raw token budget up to the nearest cache bucket (O6.6):"""
    for bucket in SUMMARY_BUDGET_BUCKETS:
        if token_budget <= bucket:
            return bucket
    return SUMMARY_BUDGET_BUCKETS[-1]
