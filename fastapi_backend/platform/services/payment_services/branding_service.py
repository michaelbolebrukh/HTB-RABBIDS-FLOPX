"""Checkout branding assembly.

Turns the generated branding manifest (`branding/checkout_branding.json`,
produced by the frontend build from the brand system) into the Stripe Checkout
`branding_settings` payload passed per session. Colors, font, shape, and name
come from the manifest; the logo is resolved at runtime from the File uploaded
once per account during bootstrap (matched by its per-instance filename, so no
env var or manual step is needed).

On a shared account this is what lets each product instance's checkout page
carry its own brand, since the account dashboard holds only one brand. It always
degrades cleanly: when disabled, or when the manifest is missing, Checkout falls
back to the account's dashboard branding.
"""
import json
import logging
import os
import time
from functools import lru_cache
from typing import Optional

from infrastructure.settings import settings as s


logger = logging.getLogger(__name__)

# Resolved logo File id cache. Only a successful resolution is cached for the
# process lifetime; a miss or transient error is negative-cached briefly so a
# logo uploaded after boot is picked up without a redeploy, while an absent logo
# does not paginate the account's files on every single checkout:
_LOGO_FILE_ID: Optional[str] = None
_LOGO_NEG_UNTIL: float = 0.0
_LOGO_NEG_TTL_SECONDS = 300.0

_MANIFEST_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "branding", "checkout_branding.json"
)

# Manifest fields passed straight through to Stripe (colors, font, shape, name).
# `_generated` and `logo_asset` are build metadata and never sent:
_PASSTHROUGH_FIELDS = (
    "display_name",
    "font_family",
    "border_style",
    "background_color",
    "button_color",
)


@lru_cache(maxsize=1)
def _load_manifest() -> Optional[dict]:
    """Loads the generated branding manifest once, or None when unavailable"""
    try:
        with open(_MANIFEST_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        logger.info("Checkout branding manifest not found; using dashboard branding.")
        return None
    except (OSError, ValueError) as exc:
        logger.warning("Checkout branding manifest unreadable (%s); using dashboard branding.", exc)
        return None


def get_checkout_branding_settings() -> Optional[dict]:
    """Builds the Stripe Checkout `branding_settings` payload, or None to skip it.

    Returns None (so the caller omits the parameter entirely and Stripe applies
    the account's dashboard branding) when branding is disabled or the manifest
    is absent.
    """
    if not s.STRIPE_CHECKOUT_BRANDING:
        return None

    manifest = _load_manifest()
    if not manifest:
        return None

    branding: dict = {
        key: manifest[key]
        for key in _PASSTHROUGH_FIELDS
        if manifest.get(key)
    }

    # The logo is resolved from the File uploaded in bootstrap. Stripe expects a
    # {type, file} reference. Absent it, the checkout logo falls back to the
    # dashboard brand while colors, font, and name still apply. The favicon always
    # uses the dashboard (parent) icon:
    logo_file_id = _resolve_logo_file_id()
    if logo_file_id:
        branding["logo"] = {"type": "file", "file": logo_file_id}

    return branding or None


def checkout_logo_filename() -> str:
    """Per-instance filename for the uploaded checkout logo.

    Shared by bootstrap (upload) and runtime (resolution) so re-runs are
    idempotent and instances sharing one Stripe account stay isolated.
    """
    return f"checkout_logo_{s.APP_DOMAIN.replace('.', '-')}.png"


def _resolve_logo_file_id() -> Optional[str]:
    """Finds this instance's uploaded checkout logo File id, or None.

    Matches the per-instance filename among the account's business_logo files, so
    no env var or manual step is needed. A hit is cached for the process lifetime;
    a miss or transient error is negative-cached only briefly, so (unlike a plain
    lru_cache) a transient failure never permanently suppresses the logo, and an
    absent logo still does not paginate the account's files on every checkout.
    Any failure degrades to the dashboard logo.
    """
    global _LOGO_FILE_ID, _LOGO_NEG_UNTIL
    if _LOGO_FILE_ID is not None:
        return _LOGO_FILE_ID
    if time.monotonic() < _LOGO_NEG_UNTIL:
        return None

    from platform.services.payment_services.stripe_gateway import get_stripe, stripe_is_configured

    if not stripe_is_configured():
        return None

    try:
        stripe = get_stripe()
        filename = checkout_logo_filename()
        # Paginating: on a shared account there can be more than one page of
        # business_logo files, so the target must not be limited to page one:
        for f in stripe.File.list(purpose="business_logo", limit=100).auto_paging_iter():
            if getattr(f, "filename", None) == filename:
                _LOGO_FILE_ID = f.id
                return f.id
        _LOGO_NEG_UNTIL = time.monotonic() + _LOGO_NEG_TTL_SECONDS
        return None
    except Exception as exc:
        logger.warning("Could not resolve checkout logo (%s); using dashboard logo.", exc)
        _LOGO_NEG_UNTIL = time.monotonic() + _LOGO_NEG_TTL_SECONDS
        return None
