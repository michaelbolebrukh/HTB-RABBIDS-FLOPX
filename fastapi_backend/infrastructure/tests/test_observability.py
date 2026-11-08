import logging

from fastapi import FastAPI
from fastapi.testclient import TestClient

from infrastructure.observability import (
    RequestIDMiddleware,
    _RequestIDLogFilter,
    get_request_id,
    init_error_tracking,
    set_user,
)


def make_app():
    app = FastAPI()
    app.add_middleware(RequestIDMiddleware)

    @app.get("/echo")
    def echo():
        return {"request_id": get_request_id()}

    return app


# ============================================================================
# REQUEST ID MIDDLEWARE
# ============================================================================

def test_request_id_generated_and_echoed():
    client = TestClient(make_app())
    response = client.get("/echo")

    header_id = response.headers["x-request-id"]
    assert header_id
    # The id the route observed is the one on the response header:
    assert response.json()["request_id"] == header_id


def test_inbound_request_id_is_honored():
    client = TestClient(make_app())
    response = client.get("/echo", headers={"X-Request-ID": "abc-123.DEF"})

    assert response.headers["x-request-id"] == "abc-123.DEF"
    assert response.json()["request_id"] == "abc-123.DEF"


def test_malformed_inbound_request_id_is_replaced():
    client = TestClient(make_app())
    response = client.get("/echo", headers={"X-Request-ID": "bad value\twith spaces"})

    header_id = response.headers["x-request-id"]
    assert header_id != "bad value\twith spaces"
    assert len(header_id) == 32  # generated uuid4 hex


def test_request_ids_differ_between_requests():
    client = TestClient(make_app())
    first = client.get("/echo").headers["x-request-id"]
    second = client.get("/echo").headers["x-request-id"]
    assert first != second


def test_no_request_id_outside_requests():
    assert get_request_id() is None


# ============================================================================
# LOG FILTER
# ============================================================================

def test_log_filter_defaults_request_id_outside_requests():
    record = logging.LogRecord("test", logging.INFO, __file__, 1, "msg", None, None)
    assert _RequestIDLogFilter().filter(record) is True
    assert record.request_id == "-"


def test_log_filter_injects_request_id_inside_request():
    app = FastAPI()
    app.add_middleware(RequestIDMiddleware)
    captured = {}

    @app.get("/log")
    def log_route():
        record = logging.LogRecord("test", logging.INFO, __file__, 1, "msg", None, None)
        _RequestIDLogFilter().filter(record)
        captured["rid"] = record.request_id
        return {"ok": True}

    client = TestClient(app)
    response = client.get("/log", headers={"X-Request-ID": "rid-for-logs"})
    assert response.status_code == 200
    assert captured["rid"] == "rid-for-logs"


# ============================================================================
# ERROR TRACKING (no-DSN mode)
# ============================================================================

def test_init_and_set_user_are_noops_without_dsn():
    # SENTRY_DSN is unset in the test env — both must be safe no-ops:
    init_error_tracking()
    set_user(42)


# ============================================================================
# READINESS
# ============================================================================

def test_readyz_reports_db_state():
    from fastapi import APIRouter
    from platform.routers import root as root_module

    app = FastAPI()
    app.include_router(root_module.router)

    class GoodSession:
        def execute(self, *_):
            return None

    class DeadSession:
        def execute(self, *_):
            raise ConnectionError("db down")

    from infrastructure.dependencies import get_db

    app.dependency_overrides[get_db] = lambda: GoodSession()
    client = TestClient(app)
    ready = client.get("/readyz")
    assert ready.status_code == 200
    assert ready.text == "READY"

    app.dependency_overrides[get_db] = lambda: DeadSession()
    not_ready = client.get("/readyz")
    assert not_ready.status_code == 503

    # Liveness never touches the database:
    assert client.get("/healthz").text == "OK"


# ============================================================================
# SENTRY REQUEST-ID STAMPING (before_send)
# ============================================================================

def test_stamp_request_id_adds_tag_from_contextvar():
    from infrastructure.observability import _stamp_request_id, _request_id

    token = _request_id.set("rid-xyz")
    try:
        event = _stamp_request_id({}, None)
        assert event["tags"]["request_id"] == "rid-xyz"
    finally:
        _request_id.reset(token)


def test_stamp_request_id_noop_without_request():
    from infrastructure.observability import _stamp_request_id

    # Outside a request the contextvar is unset; no tag is forced on:
    event = _stamp_request_id({}, None)
    assert "tags" not in event or "request_id" not in event.get("tags", {})
