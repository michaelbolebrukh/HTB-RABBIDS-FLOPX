"""Pluggable object storage for uploaded files.

The active backend is auto-detected from configuration so a fresh startup clone
needs no manual wiring:

1. Explicit `STORAGE_S3_BUCKET` → S3-compatible backend (optional AWS_* keys,
   or derived from CLOUDFLARE_* when those are unset).
2. Else a Cloudflare API token (`CLOUDFLARE_R2_API_TOKEN` or
   `CLOUDFLARE_API_TOKEN`) → R2 via one-token derivation (endpoint, buckets from
   APP_DOMAIN, S3 credentials from token id + sha256(value)).
3. Else local disk served from the /uploads mount.

Backends differ in what they can PROMISE, not just in how they store. Local
disk is best-effort (see LocalObjectStorage); a bucket is durable. Callers
choose which they need instead of every caller silently inheriting whatever the
deployment happens to have configured:

    get_object_storage()        - best-effort; degrade gracefully if it is not
                                  durable (e.g. inline the bytes instead)
    require_durable_storage()   - the blob must not evaporate; raises in
                                  production when no durable backend exists
"""

import logging
import os
from typing import Optional

from infrastructure.settings import settings as s
from platform.exceptions import DurableStorageUnavailableException
from platform.services.storage.base import ObjectStorage
from platform.services.storage.local import LocalObjectStorage
from platform.services.storage.s3 import S3ObjectStorage


logger = logging.getLogger(__name__)

# Manual cache: only durable successes (or intentional local when no cloud
# credential is configured) are sticky. A failed Cloudflare resolve must NOT
# pin LocalObjectStorage for the worker lifetime — that would turn a transient
# CF outage at boot into permanent 503s for CV upload.
_cached: Optional[ObjectStorage] = None


def _cloudflare_api_token() -> Optional[str]:
    # Prefer the R2-scoped name when both exist; fall back to the shared ops
    # token (domain-ops + storage) that the factory already keeps on Render:
    return (os.environ.get("CLOUDFLARE_R2_API_TOKEN") or os.environ.get("CLOUDFLARE_API_TOKEN") or "").strip() or None


def _local_storage() -> LocalObjectStorage:
    # The private plane derives its directory from the public one (uploads ->
    # uploads-private) rather than adding an env var; the sibling is never
    # mounted, so private objects have no ambient URL:
    return LocalObjectStorage(
        base_dir=s.STORAGE_LOCAL_DIR,
        public_origin=s.PUBLIC_BACKEND_ORIGIN,
    )


def _s3_from_explicit_bucket() -> S3ObjectStorage:
    """STORAGE_S3_BUCKET is set — use it, filling credentials from AWS_* or
    from a Cloudflare token when AWS_* are absent."""
    access_key = os.environ.get("AWS_ACCESS_KEY_ID")
    secret_key = os.environ.get("AWS_SECRET_ACCESS_KEY")
    if not (access_key and secret_key):
        token = _cloudflare_api_token()
        if token:
            from platform.services.storage.r2_resolve import resolve_r2_from_token

            resolved = resolve_r2_from_token(
                token,
                app_domain=s.APP_DOMAIN,
                account_id_override=os.environ.get("CLOUDFLARE_ACCOUNT_ID"),
            )
            return S3ObjectStorage(
                bucket=s.STORAGE_S3_BUCKET,
                endpoint_url=s.STORAGE_S3_ENDPOINT_URL or resolved.endpoint_url,
                region=s.STORAGE_S3_REGION or resolved.region,
                public_base_url=s.STORAGE_S3_PUBLIC_BASE_URL,
                private_bucket=s.STORAGE_S3_PRIVATE_BUCKET or resolved.private_bucket,
                aws_access_key_id=resolved.access_key_id,
                aws_secret_access_key=resolved.secret_access_key,
            )
    return S3ObjectStorage(
        bucket=s.STORAGE_S3_BUCKET,
        endpoint_url=s.STORAGE_S3_ENDPOINT_URL,
        region=s.STORAGE_S3_REGION,
        public_base_url=s.STORAGE_S3_PUBLIC_BASE_URL,
        private_bucket=s.STORAGE_S3_PRIVATE_BUCKET,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
    )


def _s3_from_cloudflare_token(token: str) -> S3ObjectStorage:
    from platform.services.storage.r2_resolve import resolve_r2_from_token

    resolved = resolve_r2_from_token(
        token,
        app_domain=s.APP_DOMAIN,
        account_id_override=os.environ.get("CLOUDFLARE_ACCOUNT_ID"),
    )
    return S3ObjectStorage(
        bucket=resolved.public_bucket,
        endpoint_url=resolved.endpoint_url,
        region=resolved.region,
        public_base_url=s.STORAGE_S3_PUBLIC_BASE_URL,
        private_bucket=resolved.private_bucket,
        aws_access_key_id=resolved.access_key_id,
        aws_secret_access_key=resolved.secret_access_key,
    )


def get_object_storage() -> ObjectStorage:
    """Resolving the configured object-storage backend (cached when stable)."""
    global _cached
    if _cached is not None:
        return _cached

    token = _cloudflare_api_token()
    cloud_configured = bool(s.STORAGE_S3_BUCKET or token)

    if s.STORAGE_S3_BUCKET:
        try:
            storage: ObjectStorage = _s3_from_explicit_bucket()
        except Exception:
            logger.exception(
                "Failed to build S3 storage from STORAGE_S3_BUCKET; falling back to local disk"
            )
            return _local_storage()  # not cached — retry next call
    elif token:
        try:
            storage = _s3_from_cloudflare_token(token)
        except Exception:
            logger.exception(
                "Failed to resolve R2 from Cloudflare API token; falling back to local disk"
            )
            return _local_storage()  # not cached — retry next call
    else:
        storage = _local_storage()

    # Cache durable backends, or intentional local when nothing cloud-shaped is
    # configured. Never cache a local fallback that happened despite cloud config.
    if storage.is_durable or not cloud_configured:
        _cached = storage
    return storage


def get_object_storage_cache_clear() -> None:
    """Dropping the cached backend (tests / after env changes in-process)."""
    global _cached
    _cached = None


# Compatibility with callers that used the old lru_cache API:
get_object_storage.cache_clear = get_object_storage_cache_clear  # type: ignore[attr-defined]


def validate_storage_config() -> None:
    """Shouting (ERROR -> Sentry) on every boot of a production deployment with
    no durable storage, the same contract as the inbox and Stripe gates.

    Without this the misconfiguration is invisible until a member uploads a file
    and it quietly evaporates on the next deploy. Deliberately does not raise:
    most of the app works fine without storage, so the boot should succeed with
    an alarm rather than take the service down.
    """
    if s.ENV == "development" or get_object_storage().is_durable:
        return
    logger.error(
        "No durable object storage is configured in production: uploads that must "
        "be kept will be REFUSED, and best-effort uploads degrade. Set "
        "CLOUDFLARE_API_TOKEN (R2 Edit) or STORAGE_S3_BUCKET + credentials to fix."
    )


def require_durable_storage(purpose: str) -> ObjectStorage:
    """The storage plane for blobs that must survive a redeploy.

    Fails closed in production rather than writing somewhere temporary: a file
    the product promises to keep (a CV we undertake to forward to firms) must
    never be accepted into storage that the next deploy discards, because the
    database row survives and the system then LOOKS healthy while the bytes are
    gone. Refusing the upload is recoverable; silently losing it is not.

    Outside production the best-effort backend is returned as-is, so local
    development and tests need no bucket.
    """
    storage = get_object_storage()
    if storage.is_durable:
        return storage
    if s.ENV == "development":
        return storage
    logger.error(
        "Durable object storage is required for %s but no bucket is configured; "
        "refusing the write (set CLOUDFLARE_API_TOKEN with R2 Edit, or "
        "STORAGE_S3_BUCKET / STORAGE_S3_PRIVATE_BUCKET)",
        purpose,
    )
    raise DurableStorageUnavailableException()


__all__ = ["ObjectStorage", "get_object_storage", "require_durable_storage"]
