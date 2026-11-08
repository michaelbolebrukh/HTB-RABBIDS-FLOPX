"""Loader for the generated per-instance identity manifest.

`instance/instance.json` is generated from the frontend single source of truth
(`next-pwa/product/instance.config.ts`) by
`next-pwa/scripts/instance-manifest/build-instance-manifest.cjs`. It is the
canonical mirror of instance IDENTITY (brand name, domain, contact local-part,
currency) so the backend never re-derives values that would drift from the
frontend (notably SENDER_NAME, which used to be domain-derived to "Quant Soc"
while the brand is "QuantSoc").

This module only READS that artifact and exposes it to settings derivation. It
holds no secrets - identity only. Loading is fail-open: a missing or malformed
manifest yields None and settings fall back to their prior domain-derivation, so
a backend can still boot without the generated file (though CI keeps it fresh).
"""
import json
import logging
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

MANIFEST_PATH = Path(__file__).resolve().parent / "instance" / "instance.json"


@dataclass(frozen=True)
class InstanceManifest:
    """Resolved instance identity read from instance.json (identity only)."""

    brand_name: str
    brand_wordmark: str
    brand_short_name: str
    domain: str
    contact_local_part: str
    default_currency: Optional[str]
    supported_currencies: tuple[str, ...]


@lru_cache(maxsize=1)
def get_instance_manifest() -> Optional[InstanceManifest]:
    """Loads and caches the instance manifest, or None when unavailable.

    Fail-open by design: identity is not security, so a missing/broken manifest
    logs a warning and lets settings fall back to domain-derivation rather than
    blocking startup.
    """
    try:
        raw = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        logger.warning(
            "instance.json not found at %s; falling back to domain-derived identity. "
            "Run `npm run build:instance-manifest` to generate it.",
            MANIFEST_PATH,
        )
        return None
    except (OSError, ValueError) as exc:
        logger.warning("instance.json could not be read/parsed (%s); using domain-derived identity.", exc)
        return None

    brand = raw.get("brand") or {}
    contact = raw.get("contact") or {}
    currency = raw.get("currency") or {}

    supported = tuple(
        code.strip().upper()
        for code in (currency.get("supported") or [])
        if isinstance(code, str) and code.strip()
    )
    default_currency = currency.get("default")
    if isinstance(default_currency, str) and default_currency.strip():
        default_currency = default_currency.strip().upper()
    else:
        default_currency = None

    return InstanceManifest(
        brand_name=str(brand.get("name") or "").strip(),
        brand_wordmark=str(brand.get("wordmark") or "").strip(),
        brand_short_name=str(brand.get("shortName") or "").strip(),
        domain=str(raw.get("domain") or "").strip().lower(),
        contact_local_part=str(contact.get("localPart") or "").strip().lower(),
        default_currency=default_currency,
        supported_currencies=supported,
    )
