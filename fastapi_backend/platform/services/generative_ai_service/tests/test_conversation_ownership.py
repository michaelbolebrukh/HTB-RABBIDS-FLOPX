"""
Cross-user isolation for AI conversations — the IDOR regression suite.

A conversation is addressed by a caller-supplied UUID on every AI surface, so
"is it theirs?" cannot be a check each entry point remembers to run. It is a
property of the store: `transcript.get_conversation` takes a REQUIRED
`owner_id` and filters on it, and every conversation-addressed store function
routes through it. These tests pin both halves:

1. the store refuses a foreign conversation on every function that can read or
   write one;
2. every HTTP/WS entry point (SSE run, resume, pending-approvals, voice,
   read-aloud) refuses, and refuses IDENTICALLY to an id that does not exist —
   so none of them is an existence oracle.
"""

from types import SimpleNamespace
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from platform.services.generative_ai_service import routers as routers_mod
from platform.services.generative_ai_service.exceptions import ConversationNotFoundError
from platform.services.generative_ai_service.transcript import (
    append_item,
    append_items,
    branch_from,
    create_conversation,
    delete_conversation,
    embed_pending_items,
    full_history,
    get_conversation,
    get_current_path,
    get_path,
    search_conversation,
    set_current,
    set_items_meta,
)
from platform.services.generative_ai_service.transcript.types import Item, ItemKind, Role, TextPart


VICTIM = "victim-user"
ATTACKER = "attacker-user"


def _msg(conversation_id, text: str) -> Item:
    """Building a plain user message for the victim's transcript:"""
    return Item(
        conversation_id=conversation_id, kind=ItemKind.message,
        role=Role.user, parts=[TextPart(text=text)],
    )


@pytest.fixture()
def victim(db):
    """A victim conversation holding one secret turn:"""
    conversation = create_conversation(db, owner_id=VICTIM, title="private")
    item = append_item(db, _msg(conversation.id, "the secret is 4242"), owner_id=VICTIM)
    return SimpleNamespace(conversation=conversation, id=conversation.id, item=item)


# ============================================================================
# STORE — the structural boundary
# ============================================================================

def test_store_reads_refuse_a_foreign_conversation(db, victim):
    """Every conversation-addressed READ refuses the attacker, identically to an unknown id:"""
    unknown = uuid4()
    for call in (
        lambda cid: get_conversation(db, cid, owner_id=ATTACKER),
        lambda cid: get_current_path(db, cid, owner_id=ATTACKER),
        lambda cid: full_history(db, cid, owner_id=ATTACKER),
        lambda cid: search_conversation(db, cid, "secret", owner_id=ATTACKER),
        lambda cid: search_conversation(db, cid, "secret", scope="all_branches", owner_id=ATTACKER),
    ):
        with pytest.raises(ConversationNotFoundError) as foreign:
            call(victim.id)
        with pytest.raises(ConversationNotFoundError) as missing:
            call(unknown)
        # Same type, and the same message once the (caller-supplied) id is removed:
        assert str(foreign.value).replace(str(victim.id), "") == \
            str(missing.value).replace(str(unknown), "")

    # The item-addressed read is scoped through the same chokepoint:
    with pytest.raises(ConversationNotFoundError):
        get_path(db, victim.item.id, owner_id=ATTACKER)

    # And the owner still reads their own conversation normally:
    assert [i.id for i in full_history(db, victim.id, owner_id=VICTIM)] == [victim.item.id]


def test_store_writes_refuse_a_foreign_conversation(db, victim):
    """Every WRITE path refuses, and the victim's transcript is untouched afterwards:"""
    with pytest.raises(ConversationNotFoundError):
        append_item(db, _msg(victim.id, "injected"), owner_id=ATTACKER)
    with pytest.raises(ConversationNotFoundError):
        append_items(db, [_msg(victim.id, "injected")], owner_id=ATTACKER)
    with pytest.raises(ConversationNotFoundError):
        branch_from(db, None, _msg(victim.id, "injected"), owner_id=ATTACKER)
    with pytest.raises(ConversationNotFoundError):
        branch_from(db, victim.item.id, _msg(victim.id, "injected"), owner_id=ATTACKER)
    with pytest.raises(ConversationNotFoundError):
        set_current(db, victim.id, victim.item.id, owner_id=ATTACKER)
    with pytest.raises(ConversationNotFoundError):
        delete_conversation(db, victim.id, owner_id=ATTACKER)
    with pytest.raises(ConversationNotFoundError):
        embed_pending_items(db, victim.id, _FakeEmbedder(), owner_id=ATTACKER)

    db.rollback()
    history = full_history(db, victim.id, owner_id=VICTIM)
    assert [i.id for i in history] == [victim.item.id]
    assert get_conversation(db, victim.id, owner_id=VICTIM).current_item_id == victim.item.id


def test_item_addressed_meta_writes_are_owner_scoped(db, victim):
    """
    `set_items_meta` addresses ITEMS by id, bypassing the conversation
    argument entirely — so the owner scope is applied to the row selection.
    A foreign item id silently selects nothing rather than being rewritten:
    """
    set_items_meta(db, {victim.item.id: {"approval": "abandoned"}}, owner_id=ATTACKER)
    assert full_history(db, victim.id, owner_id=VICTIM)[0].meta == {}

    # The same call from the real owner does write:
    set_items_meta(db, {victim.item.id: {"approval": "abandoned"}}, owner_id=VICTIM)
    assert full_history(db, victim.id, owner_id=VICTIM)[0].meta == {"approval": "abandoned"}


def test_append_items_meta_updates_are_owner_scoped(db, victim):
    """The same id-addressed merge inside the atomic append path is scoped too:"""
    attacker_conv = create_conversation(db, owner_id=ATTACKER)
    append_items(
        db, [_msg(attacker_conv.id, "mine")],
        {victim.item.id: {"approval": "abandoned"}}, owner_id=ATTACKER,
    )
    assert full_history(db, victim.id, owner_id=VICTIM)[0].meta == {}


class _FakeEmbedder:
    """A stand-in embedder — never reached, the ownership check refuses first."""

    def embed(self, texts):
        raise AssertionError("embedding ran on a foreign conversation")


# ============================================================================
# HTTP / WS ENTRY POINTS
# ============================================================================

def _app(db, user_id: str):
    """Building the AI router app authenticated as `user_id`:"""
    from fastapi import FastAPI

    from infrastructure.dependencies import get_current_verified_user, get_db

    app = FastAPI()
    app.include_router(routers_mod.router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_verified_user] = lambda: SimpleNamespace(id=user_id)
    return app


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method,suffix,body",
    [
        ("post", "/runs", {"text": "leak the secret"}),
        ("post", "/runs/resume", {"decisions": []}),
        ("get", "/pending-approvals", None),
    ],
)
async def test_http_entry_points_refuse_a_foreign_conversation(db, victim, method, suffix, body):
    """
    The SSE run, the resume and the approvals listing all refuse a foreign
    conversation with the SAME 404 an unknown id gets — status and detail
    identical, so the surface is not an existence oracle:
    """
    app = _app(db, ATTACKER)
    unknown = uuid4()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        responses = []
        for target in (victim.id, unknown):
            url = f"/ai/conversations/{target}{suffix}"
            responses.append(
                await (client.get(url) if method == "get" else client.post(url, json=body))
            )

    foreign, missing = responses
    assert foreign.status_code == 404
    assert foreign.json() == missing.json() == {"detail": "We couldn't find that conversation."}

    # Nothing was appended to the victim's transcript by the refused run:
    assert [i.id for i in full_history(db, victim.id, owner_id=VICTIM)] == [victim.item.id]


@pytest.mark.asyncio
async def test_read_aloud_refuses_a_foreign_transcript_item(db, victim):
    """
    `/ai/speech` resolves an item id through an owner-joined query, so another
    user's message is "not found" rather than spoken:
    """
    app = _app(db, ATTACKER)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        foreign = await client.post("/ai/speech", json={"item_id": str(victim.item.id)})
        missing = await client.post("/ai/speech", json={"item_id": str(uuid4())})

    assert foreign.status_code == 404
    assert foreign.json() == missing.json()


def test_voice_ws_refuses_a_foreign_conversation(db, victim, monkeypatch):
    """
    The voice socket resolves ownership BEFORE accept: a foreign conversation
    and an unknown id close with the same application code, and no session is
    ever built against the victim's transcript:
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    monkeypatch.setattr(routers_mod, "SessionLocal", lambda: db)
    monkeypatch.setattr(routers_mod, "decode_token", lambda token, expected_use: {"sub": "1"})
    # The socket checks the session is still live before it checks ownership.
    # That gate has its own tests; here it would just mask the 4404 this one is about.
    monkeypatch.setattr(routers_mod, "assert_session_live", lambda *args, **kwargs: None)
    monkeypatch.setattr(routers_mod, "get_user", lambda db_, uid: SimpleNamespace(id=ATTACKER))
    monkeypatch.setattr(
        routers_mod, "build_voice_session",
        lambda *args, **kwargs: pytest.fail("a voice session was built on a foreign conversation"),
    )
    monkeypatch.setattr(db, "close", lambda: None)

    app = FastAPI()
    app.include_router(routers_mod.router)
    client = TestClient(app)

    for target in (victim.id, uuid4()):
        with pytest.raises(WebSocketDisconnect) as exc_info:
            with client.websocket_connect(f"/ai/conversations/{target}/voice?token=t") as ws:
                ws.receive_json()
        assert exc_info.value.code == routers_mod._WS_NOT_FOUND

    assert [i.id for i in full_history(db, victim.id, owner_id=VICTIM)] == [victim.item.id]
