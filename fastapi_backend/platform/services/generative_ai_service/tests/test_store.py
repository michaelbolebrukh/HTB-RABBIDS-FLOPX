"""
Store behavior tests: append chains, branching (the edit mechanism), path vs
full-history semantics, current-leaf tracking, deletion policy, summary cache.
"""

from uuid import uuid4

import pytest

from platform.services.generative_ai_service.exceptions import (
    ConversationNotFoundError,
    InvalidItemError,
    ItemNotFoundError,
)
from platform.services.generative_ai_service.transcript import (
    append_item,
    branch_from,
    create_conversation,
    delete_conversation,
    full_history,
    get_conversation,
    get_current_path,
    get_path,
    get_summary,
    put_summary,
    set_current,
)
from platform.services.generative_ai_service.transcript.types import Item, ItemKind, Role, TextPart


def _msg(conversation_id, text, role=Role.user):
    """Building a minimal message item for tests:"""
    return Item(conversation_id=conversation_id, kind=ItemKind.message, role=role, parts=[TextPart(text=text)])


def test_append_chains_onto_current_leaf(db):
    conversation = create_conversation(db, owner_id="42")
    first = append_item(db, _msg(conversation.id, "one"), owner_id="42")
    second = append_item(db, _msg(conversation.id, "two", role=Role.assistant), owner_id="42")

    # The second item chained onto the first without an explicit parent:
    assert second.parent_id == first.id

    # The active path is exactly the chain, in order:
    path = get_current_path(db, conversation.id, owner_id="42")
    assert [i.id for i in path] == [first.id, second.id]

    # The conversation tracks the new leaf:
    assert get_conversation(db, conversation.id, owner_id="42").current_item_id == second.id


def test_branching_is_the_edit_mechanism(db):
    conversation = create_conversation(db, owner_id="42")
    first = append_item(db, _msg(conversation.id, "original question"), owner_id="42")
    reply = append_item(db, _msg(conversation.id, "answer", role=Role.assistant), owner_id="42")

    # "Editing" the first message: a sibling branch from its parent (a root here):
    edited = branch_from(db, first.parent_id, _msg(conversation.id, "edited question"), owner_id="42")
    assert edited.parent_id is None            # New root sibling — legacy dummy-root case
    assert edited.id != first.id               # Nothing was mutated

    # The active branch is now the edited one; the old branch still exists:
    assert [i.id for i in get_current_path(db, conversation.id, owner_id="42")] == [edited.id]
    all_ids = {i.id for i in full_history(db, conversation.id, owner_id="42")}
    assert all_ids == {first.id, reply.id, edited.id}

    # Switching back to the old branch is possible (branch navigation):
    set_current(db, conversation.id, reply.id, owner_id="42")
    assert [i.id for i in get_current_path(db, conversation.id, owner_id="42")] == [first.id, reply.id]


def test_mid_conversation_branch(db):
    conversation = create_conversation(db, owner_id="42")
    a = append_item(db, _msg(conversation.id, "a"), owner_id="42")
    b = append_item(db, _msg(conversation.id, "b", role=Role.assistant), owner_id="42")
    append_item(db, _msg(conversation.id, "c"), owner_id="42")

    # Branching from b replaces c on the active path but keeps it in history:
    alt = branch_from(db, b.id, _msg(conversation.id, "c-edited"), owner_id="42")
    assert [i.id for i in get_current_path(db, conversation.id, owner_id="42")] == [a.id, b.id, alt.id]
    assert len(full_history(db, conversation.id, owner_id="42")) == 4


def test_parent_must_belong_to_same_conversation(db):
    conversation_a = create_conversation(db, owner_id="1")
    conversation_b = create_conversation(db, owner_id="1")
    item_a = append_item(db, _msg(conversation_a.id, "hello"), owner_id="1")

    # Cross-conversation parenting is a structural error:
    orphan = _msg(conversation_b.id, "bad")
    orphan.parent_id = item_a.id
    with pytest.raises(InvalidItemError):
        append_item(db, orphan, owner_id="1")


def test_unknown_ids_raise_package_errors(db):
    with pytest.raises(ConversationNotFoundError):
        get_conversation(db, uuid4(), owner_id="42")
    with pytest.raises(ItemNotFoundError):
        get_path(db, uuid4(), owner_id="42")


def test_delete_conversation_cascades_hard(db):
    conversation = create_conversation(db, owner_id="42")
    item = append_item(db, _msg(conversation.id, "to be deleted"), owner_id="42")
    put_summary(db, conversation.id, item.id, 1000, "openai", "summary text")

    # Conversation-level hard delete removes everything (agreed policy O-c):
    delete_conversation(db, conversation.id, owner_id="42")
    with pytest.raises(ConversationNotFoundError):
        get_conversation(db, conversation.id, owner_id="42")
    with pytest.raises(ItemNotFoundError):
        get_path(db, item.id, owner_id="42")


def test_summary_cache_key_and_idempotency(db):
    conversation = create_conversation(db, owner_id="42")
    item = append_item(db, _msg(conversation.id, "content"), owner_id="42")

    created = put_summary(db, conversation.id, item.id, 1000, "openai", "the summary")
    # Same key returns the existing entry untouched (append-only spans never change):
    again = put_summary(db, conversation.id, item.id, 1000, "openai", "DIFFERENT text")
    assert again.id == created.id
    assert again.summary_text == "the summary"

    # Different budget or tokenizer family is a different cache entry (renderer key):
    other_budget = put_summary(db, conversation.id, item.id, 2000, "openai", "bigger")
    other_family = put_summary(db, conversation.id, item.id, 1000, "anthropic", "other tokenizer")
    assert other_budget.id != created.id and other_family.id != created.id

    # Lookup misses return None rather than raising:
    assert get_summary(db, conversation.id, item.id, 9999, "openai") is None


def test_usage_and_raw_round_trip_through_db(db):
    from platform.services.generative_ai_service.transcript.types import Usage

    conversation = create_conversation(db, owner_id="42")
    item = Item(
        conversation_id=conversation.id,
        kind=ItemKind.thinking,
        role=Role.assistant,
        parts=[TextPart(text="thought")],
        model_used="anthropic:claude-sonnet-4-6",
        usage=Usage(input_tokens=100, output_tokens=20, cache_read_tokens=80),
        raw={"provider": "anthropic", "signature": "sig-1"},
        token_estimates={"anthropic": 25},
    )
    stored = append_item(db, item, owner_id="42")
    loaded = get_path(db, stored.id, owner_id="42")[-1]

    # Bookkeeping and fast-lane payload survive persistence exactly:
    assert loaded.usage == item.usage
    assert loaded.raw == item.raw
    assert loaded.token_estimates == {"anthropic": 25}
    assert loaded.model_used == "anthropic:claude-sonnet-4-6"
