"""One-token R2 resolution: bucket naming and credential derivation."""

import hashlib
import pathlib
import sys
from unittest.mock import patch

import pytest

from platform.services.storage.r2_resolve import (
    R2Resolved,
    bucket_name_for_domain,
    resolve_r2_from_token,
)


def test_bucket_names_match_storage_ops_slug_rules():
    """The name promised a cross-check and only asserted this module against itself.

    `bucket_name_for_domain` here and `_bucket_name` in the storage-ops skill are
    independent copies of one algorithm, and provisioning breaks quietly if they
    diverge: the skill creates a bucket under one name and the backend reaches for
    another. Both are Python, so the agreement is executable rather than a matter of
    review - this imports the skill's copy and compares them directly.
    """
    assert bucket_name_for_domain("quant-soc.com", "public") == "quant-soc-com-public"
    assert bucket_name_for_domain("quant-soc.com", "private") == "quant-soc-com-private"
    assert bucket_name_for_domain("Example.ORG", "public") == "example-org-public"

    skill_tools = (
        pathlib.Path(__file__).resolve().parents[4]
        / ".cursor" / "skills" / "storage-ops" / "tools"
    )
    if not skill_tools.is_dir():
        # Environment precondition, not a defect: a checkout without the .cursor skill
        # tree has nothing to compare against. Reason must be a plain constant - the
        # suite-integrity guard reads it from the AST and an f-string parses as no reason.
        pytest.skip(reason="storage-ops skill tools are absent from this checkout")

    sys.path.insert(0, str(skill_tools))
    try:
        import r2 as storage_ops_r2
    finally:
        sys.path.remove(str(skill_tools))

    for domain in [
        "quant-soc.com",
        "Example.ORG",
        "a.b.c.example.co.uk",
        "x--y..z.com",
        "a" * 70 + ".com",
        "sub.domain-with-hyphens.io",
    ]:
        for plane in ("public", "private"):
            assert bucket_name_for_domain(domain, plane) == storage_ops_r2._bucket_name(
                domain, plane
            ), f"backend and storage-ops disagree on {domain!r}/{plane}"


def test_resolve_derives_s3_credentials_from_token_id_and_sha256():
    token = "test-token-value-not-real"
    token_id = "abcdef0123456789abcdef0123456789"
    account_id = "11112222333344445555666677778888"

    def fake_cf_get(tok, path):
        assert tok == token
        if path == "/user/tokens/verify":
            return {"success": True, "result": {"id": token_id, "status": "active"}}
        if path == "/accounts":
            return {"success": True, "result": [{"id": account_id, "name": "test"}]}
        raise AssertionError(path)

    with patch("platform.services.storage.r2_resolve._cf_get", side_effect=fake_cf_get):
        resolved = resolve_r2_from_token(token, app_domain="quant-soc.com")

    assert isinstance(resolved, R2Resolved)
    assert resolved.access_key_id == token_id
    assert resolved.secret_access_key == hashlib.sha256(token.encode()).hexdigest()
    assert resolved.endpoint_url == f"https://{account_id}.r2.cloudflarestorage.com"
    assert resolved.region == "auto"
    assert resolved.public_bucket == "quant-soc-com-public"
    assert resolved.private_bucket == "quant-soc-com-private"


def test_resolve_uses_account_override_without_listing():
    token = "x"
    token_id = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"

    def fake_cf_get(tok, path):
        if path == "/user/tokens/verify":
            return {"success": True, "result": {"id": token_id}}
        raise AssertionError(f"unexpected path {path}")

    with patch("platform.services.storage.r2_resolve._cf_get", side_effect=fake_cf_get):
        resolved = resolve_r2_from_token(
            token, app_domain="a.com", account_id_override="acc999"
        )

    assert resolved.account_id == "acc999"
    assert resolved.endpoint_url == "https://acc999.r2.cloudflarestorage.com"


def test_empty_domain_rejects():
    with pytest.raises(ValueError):
        bucket_name_for_domain("   ", "public")
