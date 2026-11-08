from fastapi import FastAPI
from starlette.testclient import TestClient

from platform.routers.webhooks import router


def _client():
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_webhook_dormant_when_disabled(monkeypatch):
    monkeypatch.setattr("platform.routers.webhooks.is_enabled", lambda: False)
    r = _client().post("/webhooks/sentry", content=b"{}")
    assert r.status_code == 204  # acknowledged, nothing scheduled


def test_webhook_accepts_and_dispatches_when_enabled(monkeypatch):
    monkeypatch.setattr("platform.routers.webhooks.is_enabled", lambda: True)
    seen = {}
    monkeypatch.setattr("platform.routers.webhooks.handle_sentry_webhook",
                        lambda body, sig: seen.update(body=body, sig=sig))
    r = _client().post("/webhooks/sentry", content=b'{"data":{}}',
                       headers={"sentry-hook-signature": "abc"})
    assert r.status_code == 202
    # BackgroundTasks run after the response in TestClient:
    assert seen["body"] == b'{"data":{}}'
    assert seen["sig"] == "abc"
