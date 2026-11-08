"""The public-URL contract of the S3 backend.

The defect this pins: with a custom endpoint (R2, MinIO) and no
STORAGE_S3_PUBLIC_BASE_URL, the old code concatenated the SigV4 API endpoint
into a "public" URL — a link that R2 answers with 400 for anonymous GETs — and
stored the bytes anyway, so the row looked healthy while pointing at nothing
reachable. Misconfiguration must refuse the upload instead.
"""

import sys
import types

import pytest


class _FakeClient:
    def __init__(self) -> None:
        self.put_calls: list[dict] = []

    def put_object(self, **kwargs) -> None:
        self.put_calls.append(kwargs)


def _make_storage(monkeypatch, **kwargs):
    client = _FakeClient()
    fake_boto3 = types.SimpleNamespace(client=lambda **_: client)
    monkeypatch.setitem(sys.modules, "boto3", fake_boto3)
    from platform.services.storage.s3 import S3ObjectStorage

    return S3ObjectStorage(**kwargs), client


def test_configured_public_base_url_is_used(monkeypatch):
    # The production path: an explicit public origin wins, trailing slash and all.
    storage, client = _make_storage(
        monkeypatch,
        bucket="uploads",
        endpoint_url="https://abc123.r2.cloudflarestorage.com",
        region="auto",
        public_base_url="https://cdn.example.com/",
    )
    url = storage.save(b"bytes", "avatars/1/a.png", "image/png")
    assert url == "https://cdn.example.com/avatars/1/a.png"
    assert len(client.put_calls) == 1


def test_custom_endpoint_without_public_base_url_refuses_the_write(monkeypatch):
    storage, client = _make_storage(
        monkeypatch,
        bucket="uploads",
        endpoint_url="https://abc123.r2.cloudflarestorage.com",
        region="auto",
        public_base_url=None,
    )
    with pytest.raises(RuntimeError, match="STORAGE_S3_PUBLIC_BASE_URL"):
        storage.save(b"bytes", "avatars/1/a.png", "image/png")
    # Fail closed BEFORE the write: no orphaned object the app cannot address.
    assert client.put_calls == []


def test_bare_aws_still_gets_the_virtual_hosted_url(monkeypatch):
    # No custom endpoint means real AWS S3, whose public-bucket URL form is
    # genuinely fetchable — that path stays as it was.
    storage, _ = _make_storage(
        monkeypatch,
        bucket="uploads",
        endpoint_url=None,
        region="us-east-1",
        public_base_url=None,
    )
    url = storage.save(b"bytes", "avatars/1/a.png", "image/png")
    assert url == "https://uploads.s3.amazonaws.com/avatars/1/a.png"


def test_the_startup_alarm_fires_only_for_the_broken_shape(monkeypatch, caplog):
    with caplog.at_level("ERROR"):
        _make_storage(
            monkeypatch,
            bucket="uploads",
            endpoint_url="https://abc123.r2.cloudflarestorage.com",
            region="auto",
            public_base_url="https://cdn.example.com",
        )
    assert not caplog.records

    with caplog.at_level("ERROR"):
        _make_storage(
            monkeypatch,
            bucket="uploads",
            endpoint_url="https://abc123.r2.cloudflarestorage.com",
            region="auto",
            public_base_url=None,
        )
    # ERROR level specifically, because that is what reaches error tracking:
    assert any(r.levelname == "ERROR" for r in caplog.records)
