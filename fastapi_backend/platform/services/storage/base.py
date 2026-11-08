from abc import ABC, abstractmethod
from typing import Optional


class ObjectStorage(ABC):
    """Stores opaque byte blobs under a key, on two planes with different reach.

    The PUBLIC plane (`save`) returns a URL anyone can fetch — right for avatars
    and other content meant to be embedded. The PRIVATE plane (`save_private` /
    `read_private` / `presigned_url`) never yields a permanently fetchable URL:
    reads go through an authorized endpoint that either streams the bytes or
    redirects to a short-lived presigned URL. Right for documents shared with
    named parties only (e.g. CVs).

    Private keys are namespaced `users/{user_id}/...` by convention so account
    erasure can purge `users/{user_id}/` generically, without the platform
    knowing what product features stored there.
    """

    #: Whether a blob written here survives a restart, redeploy or rescheduling
    #: of the process. Declared by the backend rather than assumed by callers,
    #: because the two planes above say nothing about PERSISTENCE: a backend can
    #: implement every method faithfully and still lose the bytes on the next
    #: deploy (a container filesystem does exactly that, which is how stored CVs
    #: went missing while their database rows survived).
    #:
    #: Consumers pick their own degradation from this:
    #:   - blobs that must not vanish call require_durable_storage() and fail
    #:     closed in production rather than writing somewhere temporary,
    #:   - best-effort blobs degrade to something self-contained (see the inline
    #:     avatar fallback in user_service).
    #: Defaults to False so a new backend is treated as untrustworthy until it
    #: explicitly claims otherwise.
    is_durable: bool = False

    @abstractmethod
    def save(self, data: bytes, key: str, content_type: str) -> str:
        """Persisting data at key and returning a publicly fetchable URL."""
        raise NotImplementedError

    @abstractmethod
    def delete_prefix(self, prefix: str) -> None:
        """Deleting every object whose key starts with prefix (or the exact
        object at that key). Idempotent: deleting a missing prefix is a no-op.

        This is the erasure hook: account deletion removes a user's uploaded
        files (e.g. avatars/{user_id}/) so blobs never outlive the account."""
        raise NotImplementedError

    @abstractmethod
    def save_private(self, data: bytes, key: str, content_type: str) -> None:
        """Persisting data at key on the private plane. No URL is returned on
        purpose — callers record the KEY and serve reads through an authorized
        endpoint (stream or presigned redirect), never a stored URL."""
        raise NotImplementedError

    @abstractmethod
    def read_private(self, key: str) -> bytes:
        """Reading the private object at key, raising FileNotFoundError when it
        does not exist. The streaming fallback for backends without presigning."""
        raise NotImplementedError

    def presigned_url(
        self, key: str, expires_seconds: int, download_filename: Optional[str] = None
    ) -> Optional[str]:
        """A short-lived direct download URL for the private object, or None when
        the backend cannot presign (local disk) — callers then stream via
        read_private. download_filename, when given, pins the browser-facing
        filename via Content-Disposition."""
        return None

    @abstractmethod
    def delete_private_prefix(self, prefix: str) -> None:
        """delete_prefix for the private plane. Idempotent, same erasure contract."""
        raise NotImplementedError
