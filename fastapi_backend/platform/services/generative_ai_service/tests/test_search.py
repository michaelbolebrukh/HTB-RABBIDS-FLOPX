"""
Search contract tests: keyless text backend, embedding backend behind a fake
embedder, and the automatic backend selection between them.
"""

from platform.services.generative_ai_service.transcript import (
    append_item,
    create_conversation,
    embed_pending_items,
    search_conversation,
)
from platform.services.generative_ai_service.transcript.types import Item, ItemKind, Role, TextPart


class FakeEmbedder:
    """
    Deterministic 2-d embedder: axis 0 counts 'apple' mentions, axis 1 counts
    'stock' mentions — enough to make cosine ranking fully predictable:
    """

    def embed(self, texts):
        return [[float(t.lower().count("apple")), float(t.lower().count("stock"))] for t in texts]


def _msg(conversation_id, text):
    return Item(conversation_id=conversation_id, kind=ItemKind.message, role=Role.user, parts=[TextPart(text=text)])


def test_text_search_matches_case_insensitively(db):
    conversation = create_conversation(db, owner_id="42")
    append_item(db, _msg(conversation.id, "The Quarterly Report is ready"), owner_id="42")
    append_item(db, _msg(conversation.id, "unrelated chatter"), owner_id="42")

    results = search_conversation(db, conversation.id, "quarterly report", owner_id="42")
    assert len(results) == 1
    assert results[0].extract_text() == "The Quarterly Report is ready"

    # Misses return an empty list, never an error:
    assert search_conversation(db, conversation.id, "zebra", owner_id="42") == []


def test_text_search_respects_k(db):
    conversation = create_conversation(db, owner_id="42")
    for i in range(5):
        append_item(db, _msg(conversation.id, f"note {i}"), owner_id="42")
    assert len(search_conversation(db, conversation.id, "note", k=3, owner_id="42")) == 3


def test_embedding_backfill_and_semantic_ranking(db):
    conversation = create_conversation(db, owner_id="42")
    append_item(db, _msg(conversation.id, "apple apple apple"), owner_id="42")
    append_item(db, _msg(conversation.id, "stock market stock"), owner_id="42")
    append_item(db, _msg(conversation.id, "apple stock"), owner_id="42")

    embedder = FakeEmbedder()

    # Backfilling embeds every text-bearing item exactly once:
    assert embed_pending_items(db, conversation.id, embedder, owner_id="42") == 3
    assert embed_pending_items(db, conversation.id, embedder, owner_id="42") == 0

    # A pure-apple query ranks the pure-apple item first (cosine, not substring):
    results = search_conversation(db, conversation.id, "apple", k=2, embedder=embedder, owner_id="42")
    assert results[0].extract_text() == "apple apple apple"
    assert len(results) == 2


def test_embedder_without_vectors_falls_back_to_text(db):
    conversation = create_conversation(db, owner_id="42")
    append_item(db, _msg(conversation.id, "find me by text"), owner_id="42")

    # No vectors were backfilled, so the text backend must serve the query:
    results = search_conversation(db, conversation.id, "find me", embedder=FakeEmbedder(), owner_id="42")
    assert len(results) == 1


def test_abandoned_branches_are_excluded_by_default(db):
    from platform.services.generative_ai_service.transcript import branch_from

    conversation = create_conversation(db, owner_id="42")
    first = append_item(db, _msg(conversation.id, "my salary is 50k"), owner_id="42")

    # Editing the message retracts the original (sibling branch becomes active):
    branch_from(db, first.parent_id, _msg(conversation.id, "my budget is flexible"), owner_id="42")

    # Default scope treats the edited-away branch as retracted (D30):
    assert search_conversation(db, conversation.id, "salary", owner_id="42") == []

    # The explicit opt-in scope still reaches it (P5 gates this per agent):
    results = search_conversation(db, conversation.id, "salary", scope="all_branches", owner_id="42")
    assert len(results) == 1


def test_semantic_search_respects_active_path_scope(db):
    from platform.services.generative_ai_service.transcript import branch_from

    conversation = create_conversation(db, owner_id="42")
    first = append_item(db, _msg(conversation.id, "apple apple apple"), owner_id="42")
    branch_from(db, first.parent_id, _msg(conversation.id, "stock talk only"), owner_id="42")

    embedder = FakeEmbedder()
    embed_pending_items(db, conversation.id, embedder, owner_id="42")

    # The abandoned pure-apple item exists with a vector, but is out of scope:
    results = search_conversation(db, conversation.id, "apple", embedder=embedder, owner_id="42")
    assert [r.extract_text() for r in results] == ["stock talk only"]

    # Widening the scope surfaces it again:
    results = search_conversation(db, conversation.id, "apple", embedder=embedder, scope="all_branches", owner_id="42")
    assert results[0].extract_text() == "apple apple apple"
