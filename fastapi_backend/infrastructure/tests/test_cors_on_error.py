import pytest
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.testclient import TestClient

from infrastructure import handlers
from infrastructure.settings import settings as s


# A real allowed origin in the test environment (ALLOWED_ORIGINS is computed):
_CONCRETE_ALLOWED = [o for o in s.ALLOWED_ORIGINS if "*" not in o]
_STAR = "*" in s.ALLOWED_ORIGINS


def _client() -> TestClient:
    app = FastAPI()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=s.ALLOWED_ORIGINS,
        allow_origin_regex=s.ALLOWED_ORIGIN_REGEX_COMPUTED,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_exception_handler(Exception, handlers.unhandled_exception_handler)

    @app.get("/boom")
    def boom():
        raise ZeroDivisionError("simulated bug")

    return TestClient(app, raise_server_exceptions=False)


@pytest.mark.skipif(not _CONCRETE_ALLOWED, reason="no concrete allowed origin configured")
def test_unhandled_500_carries_cors_for_allowed_origin():
    origin = _CONCRETE_ALLOWED[0]
    r = _client().get("/boom", headers={"origin": origin})
    assert r.status_code == 500
    # The fix: the browser can read the 500 instead of a CORS/network failure:
    assert r.headers.get("access-control-allow-origin") == origin
    assert r.headers.get("access-control-allow-credentials") == "true"
    assert r.json()["detail"] == "Something went wrong on our end. Try again in a moment."


@pytest.mark.skipif(_STAR, reason="wildcard origin allows everything")
def test_unhandled_500_omits_cors_for_disallowed_origin():
    r = _client().get("/boom", headers={"origin": "https://evil.not-allowed.example"})
    assert r.status_code == 500
    assert r.headers.get("access-control-allow-origin") is None


def test_origin_check_reflects_settings():
    for origin in _CONCRETE_ALLOWED:
        assert handlers._origin_is_allowed(origin) is True
    if not _STAR:
        assert handlers._origin_is_allowed("https://evil.not-allowed.example") is False
