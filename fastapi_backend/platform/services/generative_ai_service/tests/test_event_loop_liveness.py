"""
Event-loop-liveness regression tests for `routers.py`.

The production incident this guards against: a blocking (synchronous) DB call
made directly inside an `async def` handler runs ON the asyncio event loop,
which means it blocks EVERY concurrent request on that worker, not just its
own — including the health check, which is how a slow-but-not-broken DB call
turned into the whole instance getting killed and replaced (see
`infrastructure/rate_limiter.py` for the full incident writeup).

These tests assert the OBSERVABLE property: while a router-level blocking
call is in flight, something else running on the same event loop keeps
making progress. They do not grep the source for `to_thread` (test-integrity
rule) — a heartbeat/second-request that stalls proves the loop is wedged
regardless of what the code happens to say.
"""

import asyncio
import threading
import time
from types import SimpleNamespace

import pytest
from pydantic_ai.models.test import TestModel

from platform.services.generative_ai_service.engine import engine as engine_mod
from platform.services.generative_ai_service.engine.providers import BuiltModel


# ---------------------------------------------------------------------------
# stream_run_endpoint: the ownership check runs a blocking DB call before the
# SSE stream opens. If it runs directly on the loop, a concurrent heartbeat
# task on the SAME loop starves for the call's whole duration.
# ---------------------------------------------------------------------------


@pytest.fixture()
def app_client(db, monkeypatch):
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


_BLOCK_SECONDS = 0.3
_HEARTBEAT_INTERVAL = 0.02


@pytest.mark.asyncio
async def test_stream_run_ownership_check_does_not_freeze_the_event_loop(app_client, monkeypatch):
    """
    `_require_owned_conversation` is a synchronous DB call. Patched here to a
    plain `time.sleep` stand-in for "a slow DB round trip". httpx's
    ASGITransport runs the app in-process on the CURRENT event loop (no
    separate thread), so a concurrent asyncio task on this same loop is a
    direct probe of whether that call froze the loop.
    """
    from platform.services.generative_ai_service import routers as routers_mod

    def slow_ownership_check(db, conversation_id, owner_id):
        time.sleep(_BLOCK_SECONDS)

    monkeypatch.setattr(routers_mod, "_require_owned_conversation", slow_ownership_check)

    tick_count = 0

    async def heartbeat():
        nonlocal tick_count
        while True:
            await asyncio.sleep(_HEARTBEAT_INTERVAL)
            tick_count += 1

    async with app_client as client:
        resp = await client.post("/ai/conversations", json={})
        conversation_id = resp.json()["conversation_id"]

        hb_task = asyncio.create_task(heartbeat())
        start = time.monotonic()
        run = await client.post(f"/ai/conversations/{conversation_id}/runs", json={"text": "hi"})
        elapsed = time.monotonic() - start
        hb_task.cancel()
        try:
            await hb_task
        except asyncio.CancelledError:
            pass

    assert run.status_code == 200
    # Sanity: the blocking call really ran and really took its full duration:
    assert elapsed >= _BLOCK_SECONDS * 0.8

    # The crux: if the blocking call ran directly on the event loop, the
    # heartbeat task could not run AT ALL while it was in flight, so
    # tick_count would be ~0 regardless of elapsed time. Offloaded via
    # asyncio.to_thread, the loop keeps servicing the heartbeat task
    # throughout, so tick_count scales with elapsed time:
    expected_min_ticks = (elapsed / _HEARTBEAT_INTERVAL) * 0.5
    assert tick_count >= expected_min_ticks, (
        f"heartbeat only ticked {tick_count} times over {elapsed:.2f}s "
        f"(expected >= {expected_min_ticks:.1f}) — the event loop appears to "
        f"have been blocked by the ownership check"
    )


# ---------------------------------------------------------------------------
# voice_ws_endpoint: the pre-accept auth check (assert_session_live/get_user)
# is a blocking DB call too. Proven via a real concurrent second request over
# a TestClient shared between two threads — if the auth check runs directly
# on the loop, EVERY other request on the same worker (including this
# unrelated GET) queues behind it.
# ---------------------------------------------------------------------------


def test_voice_ws_auth_check_does_not_freeze_other_requests(db, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from infrastructure.dependencies import get_db, get_current_verified_user
    from platform.services.generative_ai_service import routers as routers_mod
    from platform.services.generative_ai_service.transcript import create_conversation as _create

    conv = _create(db, owner_id="7")

    entered_slow_check = threading.Event()

    def slow_assert_session_live(db_, sid, user_id):
        entered_slow_check.set()
        time.sleep(_BLOCK_SECONDS)

    monkeypatch.setattr(routers_mod, "decode_token", lambda token, expected_use: {"sub": "7", "sid": "3"})
    monkeypatch.setattr(routers_mod, "assert_session_live", slow_assert_session_live)
    monkeypatch.setattr(routers_mod, "get_user", lambda db_, user_id: SimpleNamespace(id=user_id))
    # voice_ws_endpoint opens its OWN session via `SessionLocal()` directly
    # (it doesn't go through the get_db dependency override below). Point it
    # at the test's own thread-safe in-memory session rather than the real
    # production engine (built from a bare "sqlite://" with no
    # check_same_thread=False) — otherwise this test trips an unrelated
    # cross-thread SQLite teardown error of its own, from a session this
    # test doesn't otherwise care about:
    monkeypatch.setattr(routers_mod, "SessionLocal", lambda: db)
    monkeypatch.setattr(db, "close", lambda: None)

    app = FastAPI()
    app.include_router(routers_mod.router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_verified_user] = lambda: SimpleNamespace(id=7)

    ws_result = {}
    timing = {}

    # Both calls MUST share the same portal (event loop + thread): the
    # TestClient only reuses one portal across calls when entered via `with`
    # (see starlette.testclient's `_portal_factory` — outside a `with` block
    # each call spins its OWN fresh portal/loop, which would make this test
    # measure nothing). Entering it here is what makes the GET below a real
    # probe of the SAME loop the websocket auth check is blocking:
    with TestClient(app) as client:

        def open_socket():
            try:
                # The socket will be refused (no real build_voice_session
                # stubbed in), which is fine — the auth check runs BEFORE
                # that, and that's the only part under test.
                with client.websocket_connect(f"/ai/conversations/{conv.id}/voice?token=t"):
                    pass
            except Exception as exc:  # noqa: BLE001 — any close/refusal is fine here
                ws_result["exc"] = exc

        ws_thread = threading.Thread(target=open_socket, daemon=True)
        ws_thread.start()

        # Wait until the websocket connection is demonstrably mid-block, so
        # the GET below genuinely overlaps the slow auth check rather than
        # racing it:
        assert entered_slow_check.wait(timeout=5), "the auth check never ran"

        start = time.monotonic()
        resp = client.get("/ai/usage")
        timing["get_elapsed"] = time.monotonic() - start

        ws_thread.join(timeout=5)

    get_elapsed = timing["get_elapsed"]
    assert resp.status_code == 200
    # The crux: an unrelated GET issued while the auth check is mid-sleep
    # must not be forced to wait out that sleep. A generous fraction of
    # _BLOCK_SECONDS still catches the failure mode (queued behind the whole
    # sleep) while tolerating scheduling jitter:
    assert get_elapsed < _BLOCK_SECONDS * 0.6, (
        f"/ai/usage took {get_elapsed:.2f}s while the voice auth check was "
        f"sleeping for {_BLOCK_SECONDS}s — it was queued behind a blocking "
        f"call on the event loop instead of running concurrently"
    )


# ---------------------------------------------------------------------------
# The voice-socket auth watchdog:
#
# (a) must not share the request's own SQLAlchemy Session — it runs
#     concurrently with the uplink loop's own use of that Session for the
#     LIFETIME of the socket, and a Session is not safe for concurrent use;
# (b) must not HOLD a pooled connection between polls — this service sets no
#     pool_size/max_overflow (default 5+10 per worker), so a session held for
#     the whole socket life doubles per-socket steady-state consumption on
#     top of the handler's own `db`;
# (c) must not create a session on one thread and close it on another —
#     asyncio.to_thread does not pin repeat calls to the same worker thread.
#
# All three are proven by observable behaviour (session identity + a
# create/close timeline), not by reading source.
# ---------------------------------------------------------------------------


def test_voice_ws_watchdog_opens_and_closes_a_fresh_session_every_poll(db_factory, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from platform.services.generative_ai_service import routers as routers_mod
    from platform.services.generative_ai_service.transcript import create_conversation as _create

    class _FakeSession:
        async def start(self): pass
        async def send_audio(self, audio_b64): pass
        async def end_turn(self): pass
        async def close(self): pass

        async def events(self):
            await asyncio.Event().wait()
            yield  # pragma: no cover

    async def _build(config, deps):
        return _FakeSession()

    lock = threading.Lock()
    created_sessions = []          # one entry per SessionLocal() call, in order
    timeline = []                  # ("create"|"close", session_index, monotonic ts)

    def session_local():
        with lock:
            idx = len(created_sessions)
            session = db_factory()
            created_sessions.append(session)
            timeline.append(("create", idx, time.monotonic()))

        real_close = session.close

        def spy_close():
            with lock:
                timeline.append(("close", idx, time.monotonic()))
            real_close()

        session.close = spy_close
        return session

    watchdog_seen_dbs = []

    def tracking_is_session_live(db_, session_id, user_id):
        watchdog_seen_dbs.append(db_)
        return True  # keep the socket open; the test closes it itself

    monkeypatch.setattr(routers_mod, "SessionLocal", session_local)
    monkeypatch.setattr(routers_mod, "decode_token", lambda token, expected_use: {"sub": "7", "sid": "3"})
    monkeypatch.setattr(routers_mod, "get_user", lambda db_, user_id: SimpleNamespace(id=user_id))
    monkeypatch.setattr(routers_mod, "assert_session_live", lambda db_, sid, user_id: None)
    monkeypatch.setattr(routers_mod, "build_voice_session", _build)
    monkeypatch.setattr(routers_mod, "is_session_live", tracking_is_session_live)
    monkeypatch.setattr(routers_mod, "_WS_AUTH_RECHECK_SECONDS", 0.05)

    app = FastAPI()
    app.include_router(routers_mod.router)
    # Use the same in-memory DB the websocket's own SessionLocal() will hit,
    # for setting up the conversation row:
    setup_db = db_factory()
    conv = _create(setup_db, owner_id="7")
    setup_db.close()

    deadline = time.monotonic() + 5
    with TestClient(app).websocket_connect(f"/ai/conversations/{conv.id}/voice?token=t"):
        while len(watchdog_seen_dbs) < 3 and time.monotonic() < deadline:
            time.sleep(0.02)
        # Leaving the `with` block closes the socket; __exit__ handles that.

    # Sanity: the watchdog really ran more than once, and each poll got its
    # own session — the request's own `db = SessionLocal()` is index 0, so
    # 3+ watchdog polls means indices 1..3 at least:
    assert len(watchdog_seen_dbs) >= 3, "the watchdog did not run enough times for this test"
    assert len(created_sessions) >= 4, "the watchdog is not opening a fresh session per poll"
    request_session = created_sessions[0]
    for watchdog_db in watchdog_seen_dbs:
        assert watchdog_db is not request_session, (
            "the watchdog reused the request's own Session — a Session is not "
            "safe for concurrent use, and the watchdog runs concurrently with "
            "the uplink loop's own use of the request session for the whole "
            "life of the socket"
        )
    assert len({id(s) for s in watchdog_seen_dbs}) == len(watchdog_seen_dbs), (
        "the watchdog reused the SAME session across polls instead of opening "
        "a fresh one each time — that holds a pooled connection for the whole "
        "socket lifetime instead of milliseconds per poll"
    )

    # The crux for "no connection held between polls": each watchdog session's
    # close() must land before the NEXT session is created. If the watchdog
    # held a session open across the sleep interval instead of closing it
    # right after the query, closes and creates would interleave instead of
    # alternating strictly create-N, close-N, create-N+1, close-N+1, ...
    watchdog_indices = sorted({idx for _, idx, _ in timeline if idx != 0})
    events_by_index = {idx: {} for idx in watchdog_indices}
    for kind, idx, ts in timeline:
        if idx in events_by_index:
            events_by_index[idx][kind] = ts
    ordered = sorted(watchdog_indices)
    for i in range(len(ordered) - 1):
        this_close = events_by_index[ordered[i]].get("close")
        next_create = events_by_index[ordered[i + 1]].get("create")
        assert this_close is not None, f"watchdog session {ordered[i]} was never closed"
        assert next_create is not None, f"watchdog session {ordered[i + 1]} was never created"
        assert this_close <= next_create, (
            f"watchdog session {ordered[i]} closed AFTER session {ordered[i + 1]} was "
            f"created — sessions are overlapping instead of one being fully closed "
            f"before the next poll opens one, which means a connection is being "
            f"held across the poll interval"
        )
