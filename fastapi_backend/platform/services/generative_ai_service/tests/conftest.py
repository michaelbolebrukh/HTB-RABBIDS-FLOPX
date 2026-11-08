"""
Test fixtures for the transcript store — pure SQLite, zero network (P1 rule).

Setting required host env vars BEFORE any infrastructure import, because
`infrastructure.database` builds its engine from settings at import time. The
tests then use their OWN in-memory engine and create only this package's
tables, so nothing here touches the host schema or a real database.
"""

import os

# Providing minimal host settings before infrastructure imports (import-order sensitive):
os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("HASH_KEY", "test-only-hash-key")
os.environ.setdefault("APP_DOMAIN", "test.local")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from infrastructure.database import Base
from platform.services.generative_ai_service.models import (
    AIBudget,
    AIConversation,
    AIMemory,
    AISpendEvent,
    AITranscriptItem,
    AITranscriptSummary,
    AIUserExtension,
)


@pytest.fixture(autouse=True)
def _reset_sse_app_status():
    """
    Resetting sse-starlette's module-level exit-signal Event before each test.
    It binds to the first test's event loop, but pytest-asyncio creates a fresh
    loop per test, so a second SSE test would hit "bound to a different event
    loop". This is a test-harness artifact only — production runs one loop:
    """
    try:
        from sse_starlette.sse import AppStatus
        AppStatus.should_exit_event = None
    except Exception:
        pass
    yield


@pytest.fixture()
def db_factory():
    """
    Yielding a session factory bound to a fresh in-memory SQLite database per
    test. StaticPool keeps every connection on the same in-memory DB; only
    this package's tables are created (host models never need importing). The
    factory itself is what the D35 background-embedding tests inject as
    RunDeps.session_factory:
    """
    engine = create_engine(
        "sqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(
        engine,
        tables=[AIConversation.__table__, AITranscriptItem.__table__, AITranscriptSummary.__table__,
                AISpendEvent.__table__, AIBudget.__table__, AIUserExtension.__table__,
                AIMemory.__table__],
    )
    session_factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    try:
        yield session_factory
    finally:
        engine.dispose()


@pytest.fixture()
def db(db_factory):
    """Yielding one session from the per-test factory (the common fixture):"""
    session = db_factory()
    try:
        yield session
    finally:
        session.close()
