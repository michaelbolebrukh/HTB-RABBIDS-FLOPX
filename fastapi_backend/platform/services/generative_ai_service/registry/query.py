"""
Registry query API — capability/budget model selection (Phase 3).

A plain predicate filter over the in-memory index. This is the surface a
router, an agent-picker, or an ops script uses to answer questions like
"reasoning models with >=400k context under $5/Mtok input".
"""

from __future__ import annotations

from .loader import all_models
from .schema import ModelInfo


def find_models(
    *,
    provider: str | None = None,
    reasoning: bool | None = None,
    vision: bool | None = None,
    tool_call: bool | None = None,
    structured_output: bool | None = None,
    input_modalities: list[str] | None = None,
    min_context: int | None = None,
    max_input_cost: float | None = None,     # USD per Mtok
    max_output_cost: float | None = None,    # USD per Mtok
    priced_only: bool = False,
    exclude_deprecated: bool = True,
) -> list[ModelInfo]:
    """
    Returning registry models matching every given filter (None = don't care),
    ordered by input cost (cheapest first, unpriced last). Each filter is a
    simple predicate; unpriced models fail any cost filter:
    """
    results: list[ModelInfo] = []
    for info in all_models():
        if provider is not None and info.provider != provider:
            continue
        if reasoning is not None and info.capabilities.reasoning != reasoning:
            continue
        if vision is not None and info.capabilities.vision != vision:
            continue
        if tool_call is not None and info.capabilities.tool_call != tool_call:
            continue
        if structured_output is not None and info.capabilities.structured_output != structured_output:
            continue
        if input_modalities is not None and not set(input_modalities) <= set(info.modalities.input):
            continue
        if min_context is not None and info.context_window < min_context:
            continue
        if max_input_cost is not None and (info.cost.input is None or info.cost.input > max_input_cost):
            continue
        if max_output_cost is not None and (info.cost.output is None or info.cost.output > max_output_cost):
            continue
        if priced_only and not info.cost.is_priced():
            continue
        if exclude_deprecated and info.lifecycle == "deprecated":
            continue
        results.append(info)

    # Ordering cheapest-first so callers can take the head; unpriced sink to the end:
    results.sort(key=lambda m: (m.cost.input is None, m.cost.input or 0.0, m.id))
    return results
