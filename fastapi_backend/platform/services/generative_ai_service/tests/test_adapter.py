"""
Adapter tests: two-lane translation (fast lane vs universal fields), event
translation, and result->items conversion with raw/usage/model_used.
"""

from uuid import uuid4

from pydantic_ai import FunctionToolCallEvent, PartStartEvent
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    TextPart as PaiTextPart,
    ThinkingPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)

from platform.services.generative_ai_service.engine import adapter
from platform.services.generative_ai_service.transcript.types import (
    DataPart,
    Item,
    ItemKind,
    Role,
    TextPart,
)


CONV = uuid4()


def _msg(text, role):
    return Item(id=uuid4(), conversation_id=CONV, kind=ItemKind.message, role=role, parts=[TextPart(text=text)])


def test_translation_lane_groups_by_direction():
    items = [
        _msg("hi", Role.user),
        _msg("hello", Role.assistant),
        _msg("how are you", Role.user),
    ]
    history = adapter.to_message_history(items, provider="openai")
    # user -> request, assistant -> response, user -> request:
    assert [type(m).__name__ for m in history] == ["ModelRequest", "ModelResponse", "ModelRequest"]
    assert isinstance(history[0].parts[0], UserPromptPart)
    assert isinstance(history[1].parts[0], PaiTextPart)


def test_summary_prepended_as_user_context_block():
    items = [_msg("recent", Role.user)]
    history = adapter.to_message_history(items, provider="openai", summary_text="EARLIER STUFF")
    first = history[0]
    assert isinstance(first, ModelRequest)
    assert "EARLIER STUFF" in first.parts[0].content
    assert "summary" in first.parts[0].content.lower()


def test_fast_lane_restores_thinking_signature_same_provider():
    # A thinking item carrying raw (with a signature) from the same provider:
    thinking_raw = adapter._THINKING_ADAPTER.dump_python(
        ThinkingPart(content="reasoned", signature="sig-123"), mode="json"
    )
    item = Item(id=uuid4(), conversation_id=CONV, kind=ItemKind.thinking, role=Role.assistant,
                parts=[TextPart(text="reasoned")], model_used="anthropic:claude-sonnet-4-6", raw=thinking_raw)
    part = adapter._part_from_item(item, provider="anthropic")
    # Fast lane used -> signature preserved:
    assert isinstance(part, ThinkingPart) and part.signature == "sig-123"


def test_fast_lane_skipped_cross_provider():
    thinking_raw = adapter._THINKING_ADAPTER.dump_python(
        ThinkingPart(content="reasoned", signature="sig-123"), mode="json"
    )
    item = Item(id=uuid4(), conversation_id=CONV, kind=ItemKind.thinking, role=Role.assistant,
                parts=[TextPart(text="reasoned")], model_used="anthropic:claude-sonnet-4-6", raw=thinking_raw)
    # Different provider -> translation lane, built from universal fields (no signature):
    part = adapter._part_from_item(item, provider="openai")
    assert isinstance(part, ThinkingPart) and part.signature is None


def test_tool_pair_translates_to_response_then_request():
    call = Item(id=uuid4(), conversation_id=CONV, kind=ItemKind.tool_call, role=Role.assistant,
                tool_name="get_x", tool_call_id="c1", parts=[DataPart(data={"q": 1})])
    result = Item(id=uuid4(), conversation_id=CONV, kind=ItemKind.tool_result, role=Role.tool,
                  tool_name="get_x", tool_call_id="c1", parts=[DataPart(data={"r": 2})])
    history = adapter.to_message_history([call, result], provider="openai")
    assert isinstance(history[0], ModelResponse) and isinstance(history[0].parts[0], ToolCallPart)
    assert isinstance(history[1], ModelRequest) and isinstance(history[1].parts[0], ToolReturnPart)


def test_translate_event_text_and_tool():
    run_id = uuid4()
    start = PartStartEvent(index=0, part=PaiTextPart(content="Hello"))
    ev = adapter.translate_event(start, run_id)
    assert ev.type == "text_delta" and ev.delta == "Hello"

    call = FunctionToolCallEvent(part=ToolCallPart(tool_name="f", args={"a": 1}, tool_call_id="c1"))
    ev2 = adapter.translate_event(call, run_id)
    assert ev2.type == "tool_call_started" and ev2.tool_name == "f" and ev2.args == {"a": 1}


def test_items_from_messages_populates_raw_usage_model():
    response = ModelResponse(parts=[
        ThinkingPart(content="thought", signature="s1"),
        PaiTextPart(content="the answer"),
        ToolCallPart(tool_name="f", args={"x": 1}, tool_call_id="c1"),
    ])
    tool_return = ModelRequest(parts=[ToolReturnPart(tool_name="f", content={"r": 2}, tool_call_id="c1")])
    items = adapter.items_from_messages([response, tool_return], CONV, "openai:gpt-5.2")
    kinds = [i.kind.value for i in items]
    assert kinds == ["thinking", "message", "tool_call", "tool_result"]
    # Every model-produced item is tagged with the serving model and carries raw:
    assert all(i.model_used == "openai:gpt-5.2" for i in items)
    assert items[0].raw is not None and items[2].raw is not None
    # The tool_result (request-side) has no raw but is correctly typed:
    assert items[3].kind.value == "tool_result" and items[3].tool_call_id == "c1"


def test_round_trip_items_to_history_and_back_preserves_thread():
    # Persisted items -> history -> the shape a same-provider replay would send:
    items = adapter.items_from_messages(
        [ModelResponse(parts=[PaiTextPart(content="reply")])], CONV, "openai:gpt-5.2"
    )
    history = adapter.to_message_history(items, provider="openai")
    assert len(history) == 1 and isinstance(history[0], ModelResponse)
    # Fast lane restored the exact TextPart content:
    assert history[0].parts[0].content == "reply"
