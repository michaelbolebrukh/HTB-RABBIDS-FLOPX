import logging
from typing import Optional

from platform.services.storage.base import ObjectStorage


logger = logging.getLogger(__name__)


class S3ObjectStorage(ObjectStorage):
    """S3-compatible object storage (AWS S3, Render, MinIO, R2, ...).

    Credentials are read from the standard AWS environment by boto3, so no
    manual wiring is needed beyond the bucket/endpoint configuration. The
    private plane is a SEPARATE bucket (never given public access) reached with
    the same credential; when it is not configured, private operations fail
    loudly rather than falling back somewhere half-private.
    """

    # A bucket is the whole point of this backend: bytes outlive the process
    # that wrote them, which is what durable-blob consumers gate on.
    is_durable = True

    def __init__(
        self,
        bucket: str,
        endpoint_url: Optional[str],
        region: Optional[str],
        public_base_url: Optional[str],
        private_bucket: Optional[str] = None,
        *,
        aws_access_key_id: Optional[str] = None,
        aws_secret_access_key: Optional[str] = None,
    ) -> None:
        import boto3  # Imported lazily so the default (local) path needs no boto3:

        self.bucket = bucket
        self.private_bucket = private_bucket
        self.endpoint_url = endpoint_url
        self.public_base_url = (public_base_url or "").rstrip("/")
        # Explicit keys win (Cloudflare-token derivation); otherwise boto3's
        # default chain reads AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY:
        client_kwargs: dict = {
            "service_name": "s3",
            "endpoint_url": endpoint_url,
            "region_name": region,
        }
        if aws_access_key_id and aws_secret_access_key:
            client_kwargs["aws_access_key_id"] = aws_access_key_id
            client_kwargs["aws_secret_access_key"] = aws_secret_access_key
        self._client = boto3.client(**client_kwargs)
        # Startup alarm (ERROR -> Sentry), same contract as validate_storage_config:
        # a custom endpoint is the SigV4 API host (R2 returns 400 to anonymous
        # GETs there), so without a public base URL public saves cannot work.
        if self.endpoint_url and not self.public_base_url:
            logger.error(
                "S3 storage uses a custom endpoint but STORAGE_S3_PUBLIC_BASE_URL "
                "is not set: public uploads will be refused until it points at the "
                "bucket's public origin (e.g. the R2 public bucket domain)."
            )

    def _require_private_bucket(self) -> str:
        # Failing closed and loudly (config-minimalism rule): a private document
        # must never be written to the public bucket or to ephemeral local disk
        # just because STORAGE_S3_PRIVATE_BUCKET was forgotten:
        if not self.private_bucket:
            raise RuntimeError(
                "Private object storage is not configured: set STORAGE_S3_PRIVATE_BUCKET "
                "to a bucket without public access."
            )
        return self.private_bucket

    def _public_url(self, key: str) -> str:
        if self.public_base_url:
            return f"{self.public_base_url}/{key}"
        if self.endpoint_url:
            # Failing closed and loudly (config-minimalism rule): concatenating
            # the API endpoint would hand out a URL that can never work — R2's
            # S3 endpoint rejects anonymous GETs — and the broken link would
            # then sit in a database row looking healthy:
            raise RuntimeError(
                "Public object storage has no public base URL: set "
                "STORAGE_S3_PUBLIC_BASE_URL to the bucket's public origin."
            )
        return f"https://{self.bucket}.s3.amazonaws.com/{key}"

    def save(self, data: bytes, key: str, content_type: str) -> str:
        # Resolved before the write, so a misconfigured deployment refuses the
        # upload instead of storing bytes it cannot address:
        url = self._public_url(key)
        self._client.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=data,
            ContentType=content_type,
        )
        return url

    def delete_prefix(self, prefix: str) -> None:
        self._delete_prefix_in(self.bucket, prefix)

    def save_private(self, data: bytes, key: str, content_type: str) -> None:
        self._client.put_object(
            Bucket=self._require_private_bucket(),
            Key=key,
            Body=data,
            ContentType=content_type,
        )

    def read_private(self, key: str) -> bytes:
        try:
            response = self._client.get_object(Bucket=self._require_private_bucket(), Key=key)
        except self._client.exceptions.NoSuchKey as exc:
            raise FileNotFoundError(key) from exc
        return response["Body"].read()

    def presigned_url(
        self, key: str, expires_seconds: int, download_filename: Optional[str] = None
    ) -> Optional[str]:
        params: dict = {"Bucket": self._require_private_bucket(), "Key": key}
        if download_filename:
            # Pinning the browser-facing filename and forcing download semantics,
            # so the object is saved as a file rather than rendered in-place:
            sanitized = download_filename.replace('"', "").replace("\r", "").replace("\n", "")
            params["ResponseContentDisposition"] = f'attachment; filename="{sanitized}"'
        return self._client.generate_presigned_url(
            "get_object", Params=params, ExpiresIn=expires_seconds
        )

    def delete_private_prefix(self, prefix: str) -> None:
        self._delete_prefix_in(self._require_private_bucket(), prefix)

    def _delete_prefix_in(self, bucket: str, prefix: str) -> None:
        paginator = self._client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            contents = page.get("Contents") or []
            if contents:
                response = self._client.delete_objects(
                    Bucket=bucket,
                    Delete={"Objects": [{"Key": obj["Key"]} for obj in contents], "Quiet": True},
                )
                # Quiet mode still reports per-key failures — surfacing them so a
                # partial erasure never passes silently (callers log and retry):
                errors = response.get("Errors") or []
                if errors:
                    raise RuntimeError(
                        f"Failed to delete {len(errors)} object(s) under prefix {prefix!r}: "
                        f"{errors[0].get('Code')} {errors[0].get('Message')}"
                    )
