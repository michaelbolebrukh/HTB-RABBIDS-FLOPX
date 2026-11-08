"""Resolving Cloudflare R2 coordinates from a single API token.

One credential: CLOUDFLARE_API_TOKEN (or CLOUDFLARE_R2_API_TOKEN). Account id,
S3 endpoint, Access Key ID, and Secret Access Key are all derived from that
token — no AWS_* hand-set vars. Bucket names are derived from APP_DOMAIN the
same way storage-ops does, so provision and runtime agree without a second
secret plane.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Optional


logger = logging.getLogger(__name__)

R2_REGION = "auto"
_CF_API = "https://api.cloudflare.com/client/v4"
_TOKEN_VERIFY_PATH = "/user/tokens/verify"
_ACCOUNTS_PATH = "/accounts"


@dataclass(frozen=True)
class R2Resolved:
    """Everything boto3 needs to talk to this instance's R2 buckets."""

    access_key_id: str
    secret_access_key: str
    endpoint_url: str
    region: str
    public_bucket: str
    private_bucket: str
    account_id: str


def bucket_name_for_domain(domain: str, plane: str) -> str:
    """Deterministic account-scoped bucket name — keep in lockstep with
    storage-ops `r2.py:_bucket_name`."""
    slug = re.sub(r"[^a-z0-9]+", "-", domain.strip().lower()).strip("-")
    if not slug:
        raise ValueError(f"Cannot derive a bucket name from domain {domain!r}")
    name = f"{slug}-{plane}"
    if len(name) > 63:
        name = f"{slug[: 63 - len(plane) - 1]}-{plane}"
    if len(name) < 3:
        raise ValueError(f"Derived bucket name {name!r} is too short for R2")
    return name


def _cf_get(token: str, path: str) -> dict:
    request = urllib.request.Request(
        f"{_CF_API}{path}",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:300]
        raise RuntimeError(f"Cloudflare API {path} failed: HTTP {exc.code}: {body}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Cloudflare API {path} unreachable: {exc.reason}") from exc
    if not payload.get("success", True):
        raise RuntimeError(f"Cloudflare API {path} error: {payload.get('errors')}")
    return payload


def resolve_r2_from_token(
    token: str,
    *,
    app_domain: str,
    account_id_override: Optional[str] = None,
) -> R2Resolved:
    """Deriving R2 S3 credentials and bucket names from one Cloudflare API token.

    Access Key ID = the token's id (GET /user/tokens/verify).
    Secret Access Key = SHA-256 hex digest of the token value (Cloudflare's
    documented mapping for using API tokens with the S3 API).
    """
    token = token.strip()
    if not token:
        raise ValueError("Cloudflare API token is empty")

    verify = _cf_get(token, _TOKEN_VERIFY_PATH)
    token_id = (verify.get("result") or {}).get("id")
    if not token_id:
        raise RuntimeError("Cloudflare token verify returned no token id")

    if account_id_override:
        account_id = account_id_override.strip()
    else:
        accounts = _cf_get(token, _ACCOUNTS_PATH).get("result") or []
        if len(accounts) != 1:
            raise RuntimeError(
                f"Cloudflare token reaches {len(accounts)} accounts; set "
                "CLOUDFLARE_ACCOUNT_ID to disambiguate."
            )
        account_id = accounts[0]["id"]

    secret = hashlib.sha256(token.encode("utf-8")).hexdigest()
    return R2Resolved(
        access_key_id=token_id,
        secret_access_key=secret,
        endpoint_url=f"https://{account_id}.r2.cloudflarestorage.com",
        region=R2_REGION,
        public_bucket=bucket_name_for_domain(app_domain, "public"),
        private_bucket=bucket_name_for_domain(app_domain, "private"),
        account_id=account_id,
    )
