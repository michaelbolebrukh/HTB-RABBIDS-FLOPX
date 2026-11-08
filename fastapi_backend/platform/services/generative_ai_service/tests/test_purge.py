"""
Owner purge (account-deletion hook) — pure SQLite, zero network.

The AI tables have no FK into the host's users table, so the host's cascade
never reaches them; `purge_owner` is the erasure hook the account-deletion
path calls. These tests prove the two properties that matter: everything the
owner produced disappears (including stored extension credentials), and other
owners plus the billing ledger survive untouched.
"""

from __future__ import annotations

from sqlalchemy import func, select

from platform.services.generative_ai_service.models import (
    AIBudget,
    AIConversation,
    AIMemory,
    AISpendEvent,
    AITranscriptItem,
    AITranscriptSummary,
    AIUserExtension,
)
from platform.services.generative_ai_service.purge import purge_owner
from platform.services.generative_ai_service.transcript import (
    Item,
    ItemKind,
    Role,
    TextPart,
    append_item,
    create_conversation,
)


def _seed_owner(db, owner: str):
    """Seeding one owner's full footprint: a conversation with an item, a
    memory, an extension holding a third-party token, budgets, and spend:"""
    conv = create_conversation(db, owner_id=owner, title="t")
    append_item(db, Item(conversation_id=conv.id, kind=ItemKind.message,
                         role=Role.user, parts=[TextPart(text="hi")]), owner_id=owner)
    db.add(AIMemory(owner_id=owner, scope=f"user:{owner}", text="fact", content_hash=f"h-{owner}"))
    db.add(AIUserExtension(owner_id=owner, agent_name="assistant", kind="mcp",
                           config={"url": "https://mcp.example", "headers": {"Authorization": "Bearer secret"}}))
    db.add(AIBudget(scope_type="owner", scope_id=owner, window="monthly", limit_microusd=1_000_000))
    db.add(AIBudget(scope_type="conversation", scope_id=str(conv.id), window="daily", limit_microusd=500_000))
    db.add(AISpendEvent(owner_id=owner, model_id="openai:gpt-5.2", usage={}, cost_microusd=42))
    db.commit()
    return conv


def _count(db, model, *where):
    return db.execute(select(func.count()).select_from(model).where(*where)).scalar_one()


def test_purge_owner_removes_everything_but_spend(db_factory):
    db = db_factory()
    _seed_owner(db, "u1")

    purge_owner(db, "u1")

    assert _count(db, AIConversation, AIConversation.owner_id == "u1") == 0
    assert _count(db, AITranscriptItem) == 0
    assert _count(db, AITranscriptSummary) == 0
    assert _count(db, AIMemory, AIMemory.owner_id == "u1") == 0
    assert _count(db, AIUserExtension, AIUserExtension.owner_id == "u1") == 0
    assert _count(db, AIBudget) == 0
    # The billing ledger survives (legal-records carve-out, purge.py docstring):
    assert _count(db, AISpendEvent, AISpendEvent.owner_id == "u1") == 1


def test_purge_owner_leaves_other_owners_untouched(db_factory):
    db = db_factory()
    _seed_owner(db, "u1")
    conv2 = _seed_owner(db, "u2")

    purge_owner(db, "u1")

    assert _count(db, AIConversation, AIConversation.owner_id == "u2") == 1
    assert _count(db, AITranscriptItem, AITranscriptItem.conversation_id == conv2.id) == 1
    assert _count(db, AIMemory, AIMemory.owner_id == "u2") == 1
    assert _count(db, AIUserExtension, AIUserExtension.owner_id == "u2") == 1
    assert _count(db, AIBudget, AIBudget.scope_type == "owner", AIBudget.scope_id == "u2") == 1
    assert _count(db, AIBudget, AIBudget.scope_type == "conversation", AIBudget.scope_id == str(conv2.id)) == 1


def test_purge_owner_no_commit_stays_in_host_transaction(db_factory):
    db = db_factory()
    _seed_owner(db, "u1")

    purge_owner(db, "u1", commit=False)
    db.rollback()

    # Rolled back with the host transaction — nothing was deleted:
    assert _count(db, AIConversation, AIConversation.owner_id == "u1") == 1
    assert _count(db, AIMemory, AIMemory.owner_id == "u1") == 1
