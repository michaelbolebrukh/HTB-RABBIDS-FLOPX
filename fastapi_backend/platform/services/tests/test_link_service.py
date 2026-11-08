"""Managed link registry: key/destination validation and resolve behaviour."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import platform.models  # noqa: F401 — register tables on Base.metadata
from infrastructure.database import Base
from platform.exceptions import (
    InvalidManagedLinkDestinationException,
    InvalidManagedLinkKeyException,
    ManagedLinkExistsException,
    ManagedLinkNotFoundException,
)
from platform.schemas import ManagedLinkCreateRequest, ManagedLinkUpdateRequest
from platform.services import link_service as ls


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(autocommit=False, autoflush=False, bind=engine)()
    try:
        yield session
    finally:
        session.close()


def test_create_and_resolve_relative_destination(db):
    row = ls.create_link(
        db,
        ManagedLinkCreateRequest(key="Freshers-Leaflet", destination="/events/welcome", label=" O-week "),
    )
    assert row.key == "freshers-leaflet"
    assert row.label == "O-week"
    assert ls.resolve_link(db, "freshers-leaflet").destination == "/events/welcome"


def test_create_https_destination(db):
    row = ls.create_link(
        db,
        ManagedLinkCreateRequest(key="discord", destination="https://discord.gg/example"),
    )
    assert row.destination == "https://discord.gg/example"


def test_rejects_reserved_and_bad_keys(db):
    with pytest.raises(InvalidManagedLinkKeyException):
        ls.create_link(db, ManagedLinkCreateRequest(key="manage", destination="/"))
    with pytest.raises(InvalidManagedLinkKeyException):
        ls.create_link(db, ManagedLinkCreateRequest(key="Bad Key", destination="/"))
    with pytest.raises(InvalidManagedLinkKeyException):
        ls.create_link(db, ManagedLinkCreateRequest(key="-leading", destination="/"))


def test_rejects_open_redirect_shapes(db):
    with pytest.raises(InvalidManagedLinkDestinationException):
        ls.create_link(db, ManagedLinkCreateRequest(key="x1", destination="//evil.example"))
    with pytest.raises(InvalidManagedLinkDestinationException):
        ls.create_link(db, ManagedLinkCreateRequest(key="x2", destination="javascript:alert(1)"))
    with pytest.raises(InvalidManagedLinkDestinationException):
        ls.create_link(db, ManagedLinkCreateRequest(key="x3", destination="ftp://files.example"))
    with pytest.raises(InvalidManagedLinkDestinationException):
        ls.create_link(
            db,
            ManagedLinkCreateRequest(key="x4", destination="https://evil.example/\r\nX"),
        )
    with pytest.raises(InvalidManagedLinkDestinationException):
        ls.create_link(
            db,
            ManagedLinkCreateRequest(key="x5", destination="https://evil.example/\x01path"),
        )


def test_bare_host_destination_becomes_https(db):
    row = ls.create_link(
        db, ManagedLinkCreateRequest(key="ext", destination="example.com/path")
    )
    assert row.destination == "https://example.com/path"
    updated = ls.update_link(
        db, "ext", ManagedLinkUpdateRequest(destination="discord.gg/abc")
    )
    assert updated.destination == "https://discord.gg/abc"


def test_duplicate_key_conflicts(db):
    ls.create_link(db, ManagedLinkCreateRequest(key="card", destination="/team"))
    with pytest.raises(ManagedLinkExistsException):
        ls.create_link(db, ManagedLinkCreateRequest(key="card", destination="/contact"))


def test_inactive_resolve_looks_like_missing(db):
    ls.create_link(db, ManagedLinkCreateRequest(key="old", destination="/"))
    ls.update_link(db, "old", ManagedLinkUpdateRequest(is_active=False))
    with pytest.raises(ManagedLinkNotFoundException):
        ls.resolve_link(db, "old")
    with pytest.raises(ManagedLinkNotFoundException):
        ls.resolve_link(db, "missing")


def test_resolve_reserved_key_looks_like_missing(db):
    with pytest.raises(ManagedLinkNotFoundException):
        ls.resolve_link(db, "api")


def test_update_retargets_without_changing_key(db):
    ls.create_link(db, ManagedLinkCreateRequest(key="biz", destination="/team"))
    updated = ls.update_link(db, "biz", ManagedLinkUpdateRequest(destination="https://example.com/x"))
    assert updated.key == "biz"
    assert updated.destination == "https://example.com/x"


def test_resolve_rejects_poisoned_destination(db):
    row = ls.create_link(db, ManagedLinkCreateRequest(key="ok", destination="/team"))
    # Bypass service validation to simulate a poisoned stored value:
    row.destination = "javascript:alert(1)"
    db.commit()
    with pytest.raises(ManagedLinkNotFoundException):
        ls.resolve_link(db, "ok")


def test_delete_removes_row(db):
    ls.create_link(db, ManagedLinkCreateRequest(key="bye", destination="/team"))
    ls.delete_link(db, "bye")
    assert ls.list_links(db) == []
    with pytest.raises(ManagedLinkNotFoundException):
        ls.get_link(db, "bye")


def test_delete_missing_raises(db):
    with pytest.raises(ManagedLinkNotFoundException):
        ls.delete_link(db, "missing")
