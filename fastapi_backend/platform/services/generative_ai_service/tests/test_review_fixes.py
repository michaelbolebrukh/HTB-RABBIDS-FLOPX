"""
Regression tests for the Phase 2 adversarial-review findings. Each test pins a
confirmed defect so it cannot silently return.
"""

from uuid import uuid4

import pytest
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    RetryPromptPart,
    TextPart as PaiTextPart,
    ToolCallPart,
    ToolReturnPart,
)

from platform.services.generative_ai_service.engine import adapter
from platform.services.generative_ai_service.engine import providers
from platform.services.generative_ai_service.transcript import (
    append_items,
    create_conversation,
    full_history,
    get_summary,
    put_summary,
)
from platform.services.generative_ai_service.transcript.types import Item, ItemKind, Role, TextPart


CONV = uuid4()


def test_retry_prompt_part_becomes_a_tool_result_pairing_the_call():
    """Finding #1 (HIGH): a tool retry must resolve its tool_call, not orphan it."""
    messages = [
        # Model emits a bad tool call:
        ModelResponse(parts=[ToolCallPart(tool_name="f", args={"bad": 1}, tool_call_id="c1")]),
        # pydantic-ai records a RetryPromptPart (tool-scoped) in the next request:
        ModelRequest(parts=[RetryPromptPart(content="invalid args", tool_name="f", tool_call_id="c1")]),
        # Model retries successfully:
        ModelResponse(parts=[ToolCallPart(tool_name="f", args={"good": 1}, tool_call_id="c2")]),
        ModelRequest(parts=[ToolReturnPart(tool_name="f", content={"r": 1}, tool_call_id="c2")]),
        ModelResponse(parts=[PaiTextPart(content="done")]),
    ]
    items = adapter.items_from_messages(messages, CONV, "openai:gpt-5.2")
    kinds = [(i.kind.value, i.tool_call_id) for i in items]
    # Every tool_call must have a matching tool_result (c1 via retry, c2 via return):
    call_ids = {i.tool_call_id for i in items if i.kind == ItemKind.tool_call}
    result_ids = {i.tool_call_id for i in items if i.kind == ItemKind.tool_result}
    assert call_ids == {"c1", "c2"}
    assert call_ids <= result_ids, f"orphaned tool_call(s): {call_ids - result_ids}; kinds={kinds}"


def test_output_level_retry_is_not_treated_as_tool_result():
    """A RetryPromptPart WITHOUT a tool_name is an output retry, not a tool_result."""
    messages = [
        ModelResponse(parts=[PaiTextPart(content="wrong shape")]),
        ModelRequest(parts=[RetryPromptPart(content="use the schema")]),   # no tool_name
        ModelResponse(parts=[PaiTextPart(content="right")]),
    ]
    items = adapter.items_from_messages(messages, CONV, "openai:gpt-5.2")
    assert all(i.kind != ItemKind.tool_result for i in items)


def test_atomic_append_items_keeps_tool_pair_together(db):
    """Finding #10 (MEDIUM): output items persist in one transaction."""
    conv = create_conversation(db, owner_id="u1")
    call = Item(conversation_id=conv.id, kind=ItemKind.tool_call, role=Role.assistant,
                tool_name="f", tool_call_id="c1", parts=[])
    result = Item(conversation_id=conv.id, kind=ItemKind.tool_result, role=Role.tool,
                  tool_name="f", tool_call_id="c1", parts=[])
    text = Item(conversation_id=conv.id, kind=ItemKind.message, role=Role.assistant, parts=[TextPart(text="ok")])
    stored = append_items(db, [call, result, text], owner_id="u1")
    hist = full_history(db, conv.id, owner_id="u1")
    # All three landed, chained, in order:
    assert [i.kind.value for i in hist] == ["tool_call", "tool_result", "message"]
    assert hist[1].parent_id == hist[0].id and hist[2].parent_id == hist[1].id
    assert len(stored) == 3


def test_summary_cache_key_round_trips(db):
    """Findings #3/#5 (MEDIUM): a written summary is found by the same-span read."""
    conv = create_conversation(db, owner_id="u1")
    span_item = Item(conversation_id=conv.id, kind=ItemKind.message, role=Role.user, parts=[TextPart(text="x")])
    from platform.services.generative_ai_service.transcript import append_item
    stored = append_item(db, span_item, owner_id="u1")
    # Writing under a span/budget/family and reading back the SAME key hits:
    put_summary(db, conv.id, stored.id, 4000, "openai", "the summary")
    assert get_summary(db, conv.id, stored.id, 4000, "openai").summary_text == "the summary"
    # A different span misses (proving the key is real, not a catch-all):
    assert get_summary(db, conv.id, uuid4(), 4000, "openai") is None


def test_bare_model_id_is_rejected(monkeypatch):
    """Finding #7 (MEDIUM): a provider-only id must raise, not build an empty model."""
    from infrastructure.settings import settings
    monkeypatch.setattr(settings, "OPENAI_API_KEY", "sk-openai", raising=False)
    with pytest.raises(ValueError, match="Invalid model id"):
        providers.build_model("openai")


def test_xai_builds_via_openai_compatible_without_xai_sdk(monkeypatch):
    """Build fix: xAI routes through the OpenAI-compatible endpoint, not xai-sdk.

    xai-sdk pins packaging>=25, which is mutually incompatible with the rate
    limiter's limits<25, so the xai extra was dropped. This pins the routing
    decision so a future edit can't silently reintroduce the xai-sdk dependency
    (and the build conflict) — it asserts the constructed model is the
    OpenAI-compatible class regardless of whether xai_sdk happens to be present.
    """
    from infrastructure.settings import settings
    from pydantic_ai.models.fallback import FallbackModel
    from pydantic_ai.models.openai import OpenAIChatModel
    monkeypatch.setattr(settings, "XAI_API_KEY", "test-xai", raising=False)

    built = providers.build_model("xai:grok-4.3")
    # The primary of the fallback chain (or the model itself) is OpenAI-compatible:
    model = built.model
    primary = model.models[0] if isinstance(model, FallbackModel) else model
    assert isinstance(primary, OpenAIChatModel), f"xai must route via OpenAIChatModel, got {type(primary)}"


def test_unbuildable_spec_fallback_is_skipped(monkeypatch):
    """Finding #6 (MEDIUM): an OpenRouter fallback with no key is skipped, not crashed on."""
    from infrastructure.settings import settings
    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", "sk-anthropic", raising=False)
    monkeypatch.setattr(settings, "OPENROUTER_API_KEY", None, raising=False)
    # Primary builds fine; the openrouter fallback id must be silently skipped:
    built = providers.build_model("anthropic:claude-sonnet-4-6", ["openrouter:x-ai/grok-4.3"])
    assert built.served_by == "anthropic"  # No crash, primary intact
