"""The durability contract: who fails closed, who degrades, and where.

These cover the failure mode that actually happened in production — a stored
document whose bytes vanished on the next deploy while its database row lived
on, so the product reported a healthy "shared" state pointing at nothing.
"""

import base64

import pytest

from platform.exceptions import DurableStorageUnavailableException
from platform.services import storage as storage_module
from platform.services.storage.local import LocalObjectStorage
from platform.services.storage.s3 import S3ObjectStorage


class _FakeDurable:
    is_durable = True

    def __init__(self) -> None:
        self.saved: list[tuple[bytes, str]] = []

    def save(self, data: bytes, key: str, content_type: str) -> str:
        self.saved.append((data, key))
        return f"https://cdn.example/{key}"


class _FakeEphemeral(_FakeDurable):
    is_durable = False


def _use_storage(monkeypatch, backend) -> None:
    monkeypatch.setattr(storage_module, "get_object_storage", lambda: backend)


# ---- the backends declare their own durability ----------------------------


def test_local_disk_never_claims_durability():
    # The platform cannot prove a directory survives a redeploy from inside the
    # process, so local disk must stay best-effort even though it persists fine
    # on a developer machine:
    assert LocalObjectStorage(base_dir="/tmp/x", public_origin=None).is_durable is False


def test_a_bucket_backend_claims_durability():
    assert S3ObjectStorage.is_durable is True


def test_the_base_default_is_untrusting():
    # A newly added backend is best-effort until it says otherwise:
    from platform.services.storage.base import ObjectStorage

    assert ObjectStorage.is_durable is False


# ---- require_durable_storage ----------------------------------------------


def test_durable_backend_is_returned(monkeypatch):
    backend = _FakeDurable()
    _use_storage(monkeypatch, backend)
    monkeypatch.setattr(storage_module.s, "ENV", "production")
    assert storage_module.require_durable_storage("CV upload") is backend


def test_production_without_durable_storage_refuses(monkeypatch):
    _use_storage(monkeypatch, _FakeEphemeral())
    monkeypatch.setattr(storage_module.s, "ENV", "production")
    with pytest.raises(DurableStorageUnavailableException) as excinfo:
        storage_module.require_durable_storage("CV upload")
    # 503, not 500: the deployment is misconfigured, the request is not invalid.
    assert excinfo.value.status_code == 503


def test_development_still_uses_local_disk(monkeypatch):
    # Refusing here would make every clone need a bucket before it could run:
    backend = _FakeEphemeral()
    _use_storage(monkeypatch, backend)
    monkeypatch.setattr(storage_module.s, "ENV", "development")
    assert storage_module.require_durable_storage("CV upload") is backend


def test_the_startup_alarm_fires_only_when_it_should(monkeypatch, caplog):
    monkeypatch.setattr(storage_module.s, "ENV", "production")

    _use_storage(monkeypatch, _FakeDurable())
    with caplog.at_level("ERROR"):
        storage_module.validate_storage_config()
    assert not caplog.records

    _use_storage(monkeypatch, _FakeEphemeral())
    with caplog.at_level("ERROR"):
        storage_module.validate_storage_config()
    # ERROR level specifically, because that is what reaches error tracking:
    assert any(r.levelname == "ERROR" for r in caplog.records)


# ---- the best-effort consumer degrades instead of breaking -----------------

# A 1x1 PNG, so the magic-byte sniffer accepts it:
_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def test_avatar_is_inlined_when_storage_is_not_durable(monkeypatch):
    from platform.services import user_service

    backend = _FakeEphemeral()
    monkeypatch.setattr(user_service, "get_object_storage", lambda: backend)
    captured: dict = {}
    monkeypatch.setattr(
        user_service, "set_user_avatar", lambda db, user, url: captured.setdefault("url", url)
    )

    user_service.store_avatar(db=None, user=type("U", (), {"id": 1, "avatar_url": None})(),
                              data=_PNG, content_type="image/png")

    # Self-contained: no storage write happened at all, and the value needs no
    # serving path to render.
    assert backend.saved == []
    assert captured["url"].startswith("data:image/png;base64,")
    assert base64.b64decode(captured["url"].split(",", 1)[1]) == _PNG


def test_avatar_uses_the_bucket_when_one_exists(monkeypatch):
    from platform.services import user_service

    backend = _FakeDurable()
    monkeypatch.setattr(user_service, "get_object_storage", lambda: backend)
    captured: dict = {}
    monkeypatch.setattr(
        user_service, "set_user_avatar", lambda db, user, url: captured.setdefault("url", url)
    )

    user_service.store_avatar(db=None, user=type("U", (), {"id": 1, "avatar_url": None})(),
                              data=_PNG, content_type="image/png")

    assert len(backend.saved) == 1
    assert captured["url"].startswith("https://cdn.example/avatars/1/")


def test_a_large_avatar_is_not_inlined(monkeypatch):
    # Guarding the row: past the cap we take the ephemeral filesystem over
    # pushing hundreds of KB of base64 into a column read on every render.
    from platform.services import user_service

    backend = _FakeEphemeral()
    monkeypatch.setattr(user_service, "get_object_storage", lambda: backend)
    captured: dict = {}
    monkeypatch.setattr(
        user_service, "set_user_avatar", lambda db, user, url: captured.setdefault("url", url)
    )

    oversized = _PNG + b"\x00" * (user_service.INLINE_AVATAR_MAX_BYTES + 1)
    user_service.store_avatar(db=None, user=type("U", (), {"id": 1, "avatar_url": None})(),
                              data=oversized, content_type="image/png")

    assert len(backend.saved) == 1
    assert not captured["url"].startswith("data:")
