"""
Private object-storage plane — local backend, zero network.

The private plane is where CVs live: no ambient URL ever exists for a private
object, the private directory is a sibling never served by the /uploads mount,
and erasure works per user prefix exactly like the public plane. These tests
pin those properties plus the traversal guard on every private entry point.
"""

import os

import pytest

from platform.services.storage.local import LocalObjectStorage


@pytest.fixture()
def storage(tmp_path):
    return LocalObjectStorage(base_dir=str(tmp_path / "uploads"), public_origin="http://test.local")


def test_private_dir_is_unmounted_sibling(storage):
    assert storage.private_dir == storage.base_dir + "-private"


def test_save_private_returns_nothing_and_stays_out_of_public_dir(storage):
    result = storage.save_private(b"cv-bytes", "users/1/cv/a.pdf", "application/pdf")

    assert result is None  # No URL for private objects, by contract
    assert os.path.exists(os.path.join(storage.private_dir, "users/1/cv/a.pdf"))
    assert not os.path.exists(os.path.join(storage.base_dir, "users/1/cv/a.pdf"))


def test_read_private_round_trip(storage):
    storage.save_private(b"cv-bytes", "users/1/cv/a.pdf", "application/pdf")
    assert storage.read_private("users/1/cv/a.pdf") == b"cv-bytes"


def test_read_private_missing_raises_file_not_found(storage):
    with pytest.raises(FileNotFoundError):
        storage.read_private("users/1/cv/missing.pdf")


def test_presigned_url_is_none_for_local_backend(storage):
    storage.save_private(b"cv-bytes", "users/1/cv/a.pdf", "application/pdf")
    assert storage.presigned_url("users/1/cv/a.pdf", 600) is None


def test_delete_private_prefix_scopes_to_one_user(storage):
    storage.save_private(b"a", "users/1/cv/a.pdf", "application/pdf")
    storage.save_private(b"b", "users/2/cv/b.pdf", "application/pdf")

    storage.delete_private_prefix("users/1/")

    assert not os.path.exists(os.path.join(storage.private_dir, "users/1"))
    assert os.path.exists(os.path.join(storage.private_dir, "users/2/cv/b.pdf"))


def test_delete_private_prefix_missing_is_noop(storage):
    storage.delete_private_prefix("users/999/")  # No error (idempotent erasure hook)


def test_private_traversal_guard(storage):
    with pytest.raises(ValueError):
        storage.save_private(b"x", "../outside.pdf", "application/pdf")
    with pytest.raises(ValueError):
        storage.read_private("../outside.pdf")
    with pytest.raises(ValueError):
        storage.delete_private_prefix("../outside")
    with pytest.raises(ValueError):
        storage.delete_private_prefix(".")
