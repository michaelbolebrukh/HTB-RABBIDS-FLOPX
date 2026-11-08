"""
Router tests: conversation creation and the SSE run endpoint, driven through
an in-process ASGI client with auth and the model faked (offline).
"""

import json

import pytest
from pydantic_ai.models.test import TestModel

from platform.services.generative_ai_service.engine import engine as engine_mod
from platform.services.generative_ai_service.engine.providers import BuiltModel


@pytest.fixture()
def app_client(db, monkeypatch):
    """
    Building a tiny FastAPI app mounting just the AI router, with the DB and
    authenticated-user dependencies overridden to the test session and a stub
    user, and build_model faked to a TestModel:
    """
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from infrastructure.dependencies import get_db, get_current_verified_user
    from platform.services.generative_ai_service.routers import router

    def fake_build(model_id, fallbacks=None):
        return BuiltModel(model=TestModel(custom_output_text="hi there"),
                          requested_id=model_id, served_by=model_id.split(":")[0])
    monkeypatch.setattr(engine_mod, "build_model", fake_build)

    class StubUser:
        id = 7

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_verified_user] = lambda: StubUser()

    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


@pytest.mark.asyncio
async def test_create_conversation_and_stream_run(app_client):
    async with app_client as client:
        # Creating a conversation:
        resp = await client.post("/ai/conversations", json={"title": "demo"})
        assert resp.status_code == 200
        conversation_id = resp.json()["conversation_id"]

        # Streaming a run — collecting the SSE body:
        run = await client.post(f"/ai/conversations/{conversation_id}/runs", json={"text": "hello"})
        assert run.status_code == 200
        body = run.text

        # The stream contains our event types in order:
        assert "run_started" in body
        assert "text_delta" in body
        assert "run_completed" in body

        # Parsing the data lines confirms they are our RunEvent JSON:
        events = []
        for line in body.splitlines():
            if line.startswith("data:"):
                events.append(json.loads(line[len("data:"):].strip()))
        assert events[0]["type"] == "run_started"
        assert any(e["type"] == "text_delta" for e in events)
        assert events[-1]["type"] == "run_completed"


@pytest.mark.asyncio
async def test_per_turn_model_override(app_client):
    async with app_client as client:
        resp = await client.post("/ai/conversations", json={})
        conversation_id = resp.json()["conversation_id"]
        # Overriding the model for this turn (mid-conversation switching path):
        run = await client.post(
            f"/ai/conversations/{conversation_id}/runs",
            json={"text": "hi", "model": "anthropic:claude-sonnet-4-6"},
        )
        assert run.status_code == 200
        assert "anthropic" in run.text  # run_started reports the switched model
