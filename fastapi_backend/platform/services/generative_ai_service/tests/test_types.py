"""
Round-trip and validation tests for transcript item types (P1 done-criterion:
every kind serializes through JSON and back byte-for-byte).
"""

from uuid import uuid4

import pytest
from pydantic import ValidationError

from platform.services.generative_ai_service.transcript.types import (
    DataPart,
    Item,
    ItemKind,
    MediaPart,
    Role,
    TextPart,
    Usage,
    content_parts_adapter,
)


CONVERSATION_ID = uuid4()


def _round_trip(item: Item) -> Item:
    """Serializing to JSON-mode dict and validating back (the store's exact path):"""
    return Item.model_validate(item.model_dump(mode="json"))


# Building one representative item per kind for exhaustive round-trip coverage:
KIND_EXAMPLES = [
    Item(
        conversation_id=CONVERSATION_ID,
        kind=ItemKind.message,
        role=Role.user,
        parts=[TextPart(text="hello"), MediaPart(media="image", url="https://x/y.png", width=64, height=64)],
    ),
    Item(
        conversation_id=CONVERSATION_ID,
        kind=ItemKind.tool_call,
        role=Role.assistant,
        tool_name="get_weather",
        tool_call_id="call_1",
        parts=[DataPart(data={"location": "Paris"})],
        model_used="openai:gpt-5.2",
        usage=Usage(input_tokens=10, output_tokens=5, reasoning_tokens=2),
    ),
    Item(
        conversation_id=CONVERSATION_ID,
        kind=ItemKind.tool_result,
        role=Role.tool,
        tool_name="get_weather",
        tool_call_id="call_1",
        parts=[DataPart(data={"forecast": "sunny"})],
    ),
    Item(
        conversation_id=CONVERSATION_ID,
        kind=ItemKind.thinking,
        role=Role.assistant,
        parts=[TextPart(text="reasoning summary")],
        model_used="anthropic:claude-sonnet-4-6",
        raw={"provider": "anthropic", "signature": "abc"},
    ),
    Item(conversation_id=CONVERSATION_ID, kind=ItemKind.summary_marker),
    Item(
        conversation_id=CONVERSATION_ID,
        kind=ItemKind.event,
        parts=[DataPart(data={"event": "voice_session_started"})],
        meta={"source": "realtime"},
    ),
]


@pytest.mark.parametrize("item", KIND_EXAMPLES, ids=[i.kind.value for i in KIND_EXAMPLES])
def test_every_kind_round_trips_exactly(item):
    restored = _round_trip(item)
    assert restored == item


def test_parts_adapter_round_trips_discriminated_union():
    parts = [TextPart(text="a"), MediaPart(media="audio", data_b64="qq", transcript="hi"), DataPart(data={"k": 1})]
    dumped = content_parts_adapter.dump_python(parts, mode="json")
    restored = content_parts_adapter.validate_python(dumped)
    assert restored == parts
    # The discriminator must have selected the right classes, not a lookalike:
    assert isinstance(restored[1], MediaPart) and restored[1].transcript == "hi"


def test_tool_items_require_tool_name():
    with pytest.raises(ValidationError):
        Item(conversation_id=CONVERSATION_ID, kind=ItemKind.tool_call, role=Role.assistant)


def test_non_tool_items_reject_tool_fields():
    with pytest.raises(ValidationError):
        Item(conversation_id=CONVERSATION_ID, kind=ItemKind.message, role=Role.user, tool_name="x")


def test_message_items_require_role():
    with pytest.raises(ValidationError):
        Item(conversation_id=CONVERSATION_ID, kind=ItemKind.message)


def test_extract_text_joins_text_and_transcripts_only():
    item = Item(
        conversation_id=CONVERSATION_ID,
        kind=ItemKind.message,
        role=Role.user,
        parts=[
            TextPart(text="first"),
            MediaPart(media="image", url="https://x/y.png"),          # No transcript — contributes nothing:
            MediaPart(media="audio", data_b64="zz", transcript="spoken"),
            DataPart(data={"ignored": True}),
        ],
    )
    assert item.extract_text() == "first\nspoken"


def test_extract_text_returns_none_when_no_text():
    item = Item(conversation_id=CONVERSATION_ID, kind=ItemKind.event, parts=[DataPart(data={})])
    assert item.extract_text() is None
