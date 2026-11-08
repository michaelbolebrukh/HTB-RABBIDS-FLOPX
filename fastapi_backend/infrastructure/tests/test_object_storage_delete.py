"""
Object-storage deletion (the erasure hook) — local backend, zero network.

`delete_prefix` is what account deletion and avatar replacement call so
uploaded files never outlive the account (the privacy policy's erasure
promise). These tests pin the three properties that matter: a user's whole
prefix disappears, other users' files survive, and the traversal guard
refuses to reach outside the storage root.
"""

import os

import pytest

from platform.services.storage.local import LocalObjectStorage


@pytest.fixture()
def storage(tmp_path):
    return LocalObjectStorage(base_dir=str(tmp_path / "uploads"), public_origin="http://test.local")


def test_delete_prefix_removes_user_directory(storage):
    storage.save(b"a", "avatars/1/x.png", "image/png")
    storage.save(b"b", "avatars/1/y.png", "image/png")
    storage.save(b"c", "avatars/2/z.png", "image/png")

    storage.delete_prefix("avatars/1/")

    assert not os.path.exists(os.path.join(storage.base_dir, "avatars/1"))
    assert os.path.exists(os.path.join(storage.base_dir, "avatars/2/z.png"))


def test_delete_prefix_removes_single_file_key(storage):
    storage.save(b"a", "avatars/1/x.png", "image/png")
    storage.save(b"b", "avatars/1/y.png", "image/png")

    storage.delete_prefix("avatars/1/x.png")

    assert not os.path.exists(os.path.join(storage.base_dir, "avatars/1/x.png"))
    assert os.path.exists(os.path.join(storage.base_dir, "avatars/1/y.png"))


def test_delete_prefix_missing_is_noop(storage):
    storage.delete_prefix("avatars/999/")  # No error (idempotent erasure hook)


def test_delete_prefix_rejects_traversal_and_root(storage):
    with pytest.raises(ValueError):
        storage.delete_prefix("../outside")
    with pytest.raises(ValueError):
        storage.delete_prefix(".")
