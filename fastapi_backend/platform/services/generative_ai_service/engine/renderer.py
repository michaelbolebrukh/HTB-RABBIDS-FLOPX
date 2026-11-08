"""
Renderer — turning the transcript path into a per-model context (Phase 2).

This module owns the SUMMARIZATION POLICY (the O6 decisions) and the decision
of WHAT goes into a model's context. It deliberately does NOT import
`pydantic_ai` (D3): it returns OUR items, and the adapter performs the two-lane
translation into SDK messages. The actual summary generation is an injected
callback (`SummarizeFn`), so this stays pure policy and is fully testable with
a fake summarizer.

Conditional summarization (requirement 6): large-context models get the full
history untouched even if a smaller model summarized the same conversation
earlier — because summaries are per-budget cache rows, and the transcript is
never mutated. Small-context models get a summary block + a verbatim tail.

Boundary atomicity (O6.3, DECIDED): a summary cut never separates a tool_call
from its tool_result and never splits an assistant turn — cuts snap to turn
seams. Failure policy (O6.7, DECIDED): if summarization fails, fall back to
plain truncation and alert; never inject error strings into the model context.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Awaitable, Callable

from ..transcript.types import Item, ItemKind, Role
from .model_info import ModelInfo, quantize_budget


# A summarizer: given items to compress and a token budget, returns summary
# text. Injected by the engine (backed by a cheap model, O6.1); the renderer
# never constructs a model itself:
SummarizeFn = Callable[[list[Item], int], Awaitable[str]]

# An alert sink for summarization failures (wired to Sentry by the engine;
# defaults to a no-op so the renderer is usable standalone in tests):
AlertFn = Callable[[str], None]


# Rough token estimate: ~4 chars/token plus a fixed per-item framing overhead.
# Deliberately approximate — the target/trigger hysteresis (O6.8) absorbs the
# error, and provider-reported usage corrects drift over the conversation:
_CHARS_PER_TOKEN = 4
_PER_ITEM_OVERHEAD = 8


@dataclass
class RenderedContext:
    """
    The result of rendering: the items to send (already summarized if needed)
    plus an optional summary preamble. The adapter translates `items` via the
    two-lane rule and prepends `summary_text` as a delimited context block.
    """
    items: list[Item]
    summary_text: str | None = None
    model_id: str = ""
    was_summarized: bool = False
    # Ids of items covered by the summary (excluded from `items`) — for tracing:
    summarized_item_ids: list = field(default_factory=list)


def estimate_item_tokens(item: Item, tokenizer_family: str) -> int:
    """
    Estimating an item's token cost for a tokenizer family, memoized on the
    item's `token_estimates` cache (carried from the legacy TokenCache idea):
    """
    cached = item.token_estimates.get(tokenizer_family)
    if cached is not None:
        return cached

    # Summing the character length of text-bearing parts:
    text = item.extract_text() or ""
    chars = len(text)
    # Counting structured/data parts by their serialized size (tool args/results):
    for part in item.parts:
        part_type = getattr(part, "type", None)
        if part_type == "data":
            chars += len(str(getattr(part, "data", "")))

    estimate = (chars // _CHARS_PER_TOKEN) + _PER_ITEM_OVERHEAD
    item.token_estimates[tokenizer_family] = estimate
    return estimate


def _is_turn_seam(prev: Item | None, nxt: Item) -> bool:
    """
    Deciding whether a cut BEFORE `nxt` is a legal turn seam (O6.3): a seam sits
    before a user or assistant message that opens a new turn, never between a
    tool_call and its tool_result and never before a lone tool_result/thinking
    that belongs to the previous turn:
    """
    if nxt.kind == ItemKind.message and nxt.role in (Role.user, Role.assistant):
        # A cut before a fresh user/assistant message is a clean seam, UNLESS the
        # previous item was a tool_call awaiting this... (tool results are role=tool,
        # so an assistant/user message never continues a tool pair):
        return True
    return False


def _snap_to_seam(items: list[Item], ideal_cut: int) -> int:
    """
    Moving an ideal cut index EARLIER until it lands on a turn seam, so the
    kept tail always begins at a turn boundary (never mid tool-pair). Returns
    the index where the kept tail starts:
    """
    cut = min(max(ideal_cut, 0), len(items))
    while cut > 0:
        prev = items[cut - 1] if cut - 1 >= 0 else None
        if cut < len(items) and _is_turn_seam(prev, items[cut]):
            return cut
        cut -= 1
    return 0


def _select_tail_by_budget(items: list[Item], tokenizer_family: str, tail_budget: int) -> int:
    """
    Walking backward from the newest item, keeping items until `tail_budget` is
    reached, then snapping the resulting cut to a turn seam. Returns the start
    index of the verbatim tail:
    """
    cumulative = 0
    idx = len(items)
    for i in range(len(items) - 1, -1, -1):
        cumulative += estimate_item_tokens(items[i], tokenizer_family)
        if cumulative > tail_budget:
            idx = i + 1
            break
        idx = i
    return _snap_to_seam(items, idx)


# Per-kind inclusion when BUILDING the text handed to the summarizer (O6.2):
# thinking is model-internal and excluded; tool results are truncated (bulkiest,
# least referenced later); media becomes a short placeholder plus any transcript.
_TOOL_RESULT_CHAR_CAP = 500


def summarization_input_text(items: list[Item]) -> str:
    """
    Rendering the items-to-summarize into the text the summarizer reads,
    applying per-kind inclusion rules (O6.2):
    """
    lines: list[str] = []
    for item in items:
        if item.kind == ItemKind.thinking:
            continue  # Excluding model-internal reasoning:
        role = item.role.value if item.role else item.kind.value
        text = item.extract_text() or ""

        if item.kind == ItemKind.tool_call:
            lines.append(f"[tool_call {item.tool_name}] {text}")
        elif item.kind == ItemKind.tool_result:
            # Truncating potentially huge tool payloads:
            capped = text[:_TOOL_RESULT_CHAR_CAP]
            if len(text) > _TOOL_RESULT_CHAR_CAP:
                capped += " …[truncated]"
            lines.append(f"[tool_result {item.tool_name}] {capped}")
        else:
            # Noting any media presence as a placeholder (transcript already in text):
            media_note = ""
            if any(getattr(p, "type", None) == "media" for p in item.parts):
                media_note = " [+media]"
            lines.append(f"{role}: {text}{media_note}")
    return "\n".join(lines)


async def render_context(
    path: list[Item],
    info: ModelInfo,
    *,
    summarize: SummarizeFn,
    cache_get: Callable[["object", int, str], str | None],
    cache_put: Callable[[int, str, str, "object"], None],
    target_pct: float = 0.4,
    trigger_pct: float = 0.75,
    summary_max_pct: float = 0.1,
    allow_summarization: bool = True,
    alert: AlertFn | None = None,
) -> RenderedContext:
    """
    Rendering the active path into a model context for `info`.

    Thresholds (fractions of the context window, O6/legacy three-threshold
    idea; wide hysteresis per O6.8): under `trigger` the full path is used; at
    or above it, the head is summarized down and a verbatim tail (sized to
    `target`) is kept. `summary_max_pct` bounds the summary itself.

    cache_get(span_end_item_id, quantized_budget, tokenizer_family) -> summary | None
    cache_put(quantized_budget, tokenizer_family, summary_text, span_end_item_id)
    are injected so the renderer touches the summary cache without importing the
    store's ORM types. Both are keyed on the SAME span_end_item_id the renderer
    computes below, so a read always matches its corresponding write:
    """
    window = info.context_window
    family = info.tokenizer_family

    # Estimating the whole path once:
    total = sum(estimate_item_tokens(item, family) for item in path)
    trigger = int(window * trigger_pct)

    # Fitting comfortably -> full history, no summarization (requirement 6:
    # large-window models never summarize, even if a smaller one did earlier):
    if total < trigger:
        return RenderedContext(items=list(path), model_id=info.id)

    # Overflow with summarization disabled -> deliberate typed failure
    # (D-features). Checked BEFORE the single-item shortcut below so one giant
    # item raises the promised typed error instead of silently reaching the
    # provider as a raw 400:
    if not allow_summarization:
        from .types import ContextOverflowError
        raise ContextOverflowError(
            f"Path ~{total} tokens exceeds trigger {trigger} for {info.id} and "
            f"summarization is disabled for this agent"
        )

    # A single overflowing item has no head/tail seam to summarize across ->
    # the full path goes through (the tail selector below would say the same):
    if len(path) <= 1:
        return RenderedContext(items=list(path), model_id=info.id)

    # Choosing the verbatim tail (target-sized) at a legal turn seam:
    target = int(window * target_pct)
    tail_start = _select_tail_by_budget(path, family, target)
    if tail_start == 0:
        # Nothing summarizable after snapping (e.g. one giant turn) -> full path:
        return RenderedContext(items=list(path), model_id=info.id)

    head = path[:tail_start]
    tail = path[tail_start:]
    span_end_item_id = head[-1].id
    budget = quantize_budget(int(window * summary_max_pct))

    # Consulting the summary cache on the SAME span the write below uses
    # (per span/budget/family — D24/O6.6), so cache hits actually happen:
    summary_text = cache_get(span_end_item_id, budget, family)
    if summary_text is None:
        try:
            summary_text = await summarize(head, budget)
            cache_put(budget, family, summary_text, span_end_item_id)
        except Exception as exc:  # noqa: BLE001 — failure policy is deliberate (O6.7):
            # Falling back to plain truncation: drop the head, keep the tail
            # verbatim; alert loudly; NEVER put the error in the model context:
            if alert is not None:
                alert(f"summarization failed for {info.id}: {exc!r}; truncating head")
            return RenderedContext(
                items=tail, summary_text=None, model_id=info.id,
                was_summarized=True, summarized_item_ids=[i.id for i in head],
            )

    return RenderedContext(
        items=tail,
        summary_text=summary_text,
        model_id=info.id,
        was_summarized=True,
        summarized_item_ids=[i.id for i in head],
    )
