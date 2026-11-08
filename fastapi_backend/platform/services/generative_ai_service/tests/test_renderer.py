"""
Renderer tests: full-history vs summarized paths, turn-seam boundary snapping,
summary cache hit/miss, failure->truncation, and the per-kind summary input.
All offline — the summarizer is a fake callback.
"""

from uuid import uuid4

import pytest

from platform.services.generative_ai_service.engine.model_info import ModelInfo
from platform.services.generative_ai_service.engine.renderer import (
    render_context,
    summarization_input_text,
)
from platform.services.generative_ai_service.transcript.types import (
    DataPart,
    Item,
    ItemKind,
    Role,
    TextPart,
)


CONV = uuid4()


def _msg(text, role=Role.user):
    return Item(id=uuid4(), conversation_id=CONV, kind=ItemKind.message, role=role, parts=[TextPart(text=text)])


def _small_window(window):
    return ModelInfo(id="test:m", provider="test", context_window=window, tokenizer_family="test")


async def _fake_summarize(items, budget):
    return f"SUMMARY of {len(items)} items"


def _no_cache():
    return (lambda s, b, f: None, lambda b, f, s, e: None)


@pytest.mark.asyncio
async def test_full_history_when_it_fits():
    path = [_msg("hi"), _msg("hello", Role.assistant)]
    get, put = _no_cache()
    ctx = await render_context(path, _small_window(1_000_000), summarize=_fake_summarize, cache_get=get, cache_put=put)
    assert not ctx.was_summarized
    assert len(ctx.items) == 2
    assert ctx.summary_text is None


@pytest.mark.asyncio
async def test_large_window_never_summarizes_even_with_many_items():
    # Requirement 6: a big model gets the whole history untouched:
    path = [_msg("x" * 400) for _ in range(50)]
    get, put = _no_cache()
    ctx = await render_context(path, _small_window(1_000_000), summarize=_fake_summarize, cache_get=get, cache_put=put)
    assert not ctx.was_summarized and len(ctx.items) == 50


@pytest.mark.asyncio
async def test_small_window_summarizes_head_keeps_tail():
    # Tiny window forces summarization; alternate user/assistant so seams exist:
    path = []
    for i in range(20):
        path.append(_msg("word " * 120, Role.user if i % 2 == 0 else Role.assistant))
    get, put = _no_cache()
    ctx = await render_context(
        path, _small_window(2000), summarize=_fake_summarize, cache_get=get, cache_put=put,
    )
    assert ctx.was_summarized
    assert ctx.summary_text.startswith("SUMMARY")
    assert len(ctx.items) < len(path)          # A verbatim tail, not everything
    assert ctx.summarized_item_ids             # Head items were summarized


@pytest.mark.asyncio
async def test_boundary_never_splits_a_tool_pair():
    # A tool_call immediately followed by its result must stay together in the tail:
    path = [_msg("start")]
    for i in range(10):
        path.append(_msg("filler " * 120, Role.assistant))
    call = Item(id=uuid4(), conversation_id=CONV, kind=ItemKind.tool_call, role=Role.assistant,
                tool_name="f", tool_call_id="c1", parts=[DataPart(data={"a": 1})])
    result = Item(id=uuid4(), conversation_id=CONV, kind=ItemKind.tool_result, role=Role.tool,
                  tool_name="f", tool_call_id="c1", parts=[DataPart(data={"r": 2})])
    path += [call, result, _msg("after", Role.assistant)]
    get, put = _no_cache()
    ctx = await render_context(path, _small_window(1500), summarize=_fake_summarize, cache_get=get, cache_put=put)
    kept_ids = {i.id for i in ctx.items}
    # If the result is kept, the call must be kept too (never orphaned):
    if result.id in kept_ids:
        assert call.id in kept_ids


@pytest.mark.asyncio
async def test_summary_cache_hit_skips_summarizer():
    path = [_msg("word " * 120, Role.user if i % 2 == 0 else Role.assistant) for i in range(20)]
    calls = {"n": 0}

    async def counting_summarize(items, budget):
        calls["n"] += 1
        return "CACHED-STYLE SUMMARY"

    # cache_get returns a hit -> summarizer must not be called:
    ctx = await render_context(
        path, _small_window(2000), summarize=counting_summarize,
        cache_get=lambda s, b, f: "PRECACHED", cache_put=lambda b, f, s, e: None,
    )
    assert ctx.summary_text == "PRECACHED"
    assert calls["n"] == 0


@pytest.mark.asyncio
async def test_summarizer_failure_truncates_and_alerts():
    path = [_msg("word " * 120, Role.user if i % 2 == 0 else Role.assistant) for i in range(20)]
    alerts = []

    async def failing_summarize(items, budget):
        raise RuntimeError("summarizer down")

    ctx = await render_context(
        path, _small_window(2000), summarize=failing_summarize,
        cache_get=lambda s, b, f: None, cache_put=lambda b, f, s, e: None,
        alert=alerts.append,
    )
    # Failure policy (O6.7): truncate to the tail, no summary text, alert fired,
    # and crucially NO error string injected into the context:
    assert ctx.was_summarized and ctx.summary_text is None
    assert alerts and "failed" in alerts[0]
    assert all("summarizer down" not in (i.extract_text() or "") for i in ctx.items)


@pytest.mark.asyncio
async def test_disabled_summarization_raises_on_overflow():
    from platform.services.generative_ai_service.engine.types import ContextOverflowError
    path = [_msg("word " * 120, Role.user if i % 2 == 0 else Role.assistant) for i in range(20)]
    with pytest.raises(ContextOverflowError):
        await render_context(
            path, _small_window(2000), summarize=_fake_summarize,
            cache_get=lambda s, b, f: None, cache_put=lambda b, f, s, e: None,
            allow_summarization=False,
        )


def test_summary_input_applies_per_kind_rules():
    thinking = Item(id=uuid4(), conversation_id=CONV, kind=ItemKind.thinking, role=Role.assistant,
                    parts=[TextPart(text="internal reasoning")])
    tool_result = Item(id=uuid4(), conversation_id=CONV, kind=ItemKind.tool_result, role=Role.tool,
                       tool_name="f", tool_call_id="c1", parts=[TextPart(text="X" * 2000)])
    msg = _msg("actual message")
    text = summarization_input_text([thinking, tool_result, msg])
    # Thinking excluded; tool result truncated; message included (O6.2):
    assert "internal reasoning" not in text
    assert "[truncated]" in text
    assert "actual message" in text
