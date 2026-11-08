"""Declarative reconcile of the code-defined catalog into Stripe.

Runs at startup (via a product startup task). Idempotent: Products/Prices are
matched by price ``lookup_key`` (the catalog item key) and only created or
rotated when the authored definition changed. Entitlement Features are matched
by their own lookup_key and attached to the product. Removing an item from the
manifest stops selling it (no checkout path) but does not archive historical
Stripe objects; archive manually in Stripe if desired.
"""
import logging

from platform.services.payment_services.catalog import CatalogItem
from platform.services.payment_services.stripe_gateway import get_stripe, stripe_is_configured


logger = logging.getLogger(__name__)


def _item_metadata(item: CatalogItem) -> dict:
    """Builds the Stripe product metadata mirror of an authored catalog item"""
    return {
        "item_key": item.key,
        "item_type": item.type.value,
        "launch_stage": item.launch_stage.value,
        "per_user_limit": str(item.per_user_limit) if item.per_user_limit else "",
        "requires_shipping": "true" if item.requires_shipping else "false",
        "entitlement_feature": item.entitlement_feature or "",
    }


def _price_matches(price, item: CatalogItem) -> bool:
    """Checks whether an existing Stripe price still matches the authored definition"""
    if price.unit_amount != item.amount_minor: return False
    if price.currency.lower() != item.currency.lower(): return False

    recurring = getattr(price, "recurring", None)
    interval = recurring.interval if recurring else None
    return interval == item.interval


def _ensure_product(stripe, item: CatalogItem, product_id: str | None) -> str:
    """Creates or updates the Stripe product mirroring the catalog item"""
    if product_id:
        stripe.Product.modify(product_id, name=item.name, metadata=_item_metadata(item))
        return product_id

    product = stripe.Product.create(name=item.name, metadata=_item_metadata(item))
    return product.id


def _create_price(stripe, item: CatalogItem, product_id: str, transfer_lookup_key: bool):
    params = {
        "product": product_id,
        "currency": item.currency.lower(),
        "unit_amount": item.amount_minor,
        "lookup_key": item.key,
        "transfer_lookup_key": transfer_lookup_key,
    }
    if item.interval:
        params["recurring"] = {"interval": item.interval}
    return stripe.Price.create(**params)


def _ensure_feature(stripe, item: CatalogItem, product_id: str) -> None:
    """Ensures the entitlement Feature exists and is attached to the product"""
    if not item.entitlement_feature:
        return

    features = stripe.entitlements.Feature.list(lookup_key=item.entitlement_feature, limit=1)
    if features.data:
        feature = features.data[0]
    else:
        feature = stripe.entitlements.Feature.create(
            lookup_key=item.entitlement_feature,
            name=item.entitlement_feature.replace("_", " ").replace("-", " ").title(),
        )

    # Skipping when already attached (idempotent, without parsing error strings):
    attached = stripe.Product.list_features(product_id, limit=100)
    if any(pf.entitlement_feature.id == feature.id for pf in attached.data):
        return

    stripe.Product.create_feature(product_id, entitlement_feature=feature.id)


def ensure_stripe_price(item: CatalogItem, sdk=None) -> str:
    """Ensures a Stripe price exists for the item and returns its id (caching ids on the item).

    `sdk` is provisioning-only (stripe_bootstrap passes get_stripe_for_provisioning());
    runtime callers omit it and go through the fail-closed get_stripe().
    """
    stripe = sdk if sdk is not None else get_stripe()

    prices = stripe.Price.list(lookup_keys=[item.key], limit=1)
    existing = prices.data[0] if prices.data else None

    if existing and _price_matches(existing, item):
        item.stripe_product_id = _ensure_product(stripe, item, existing.product)
        item.stripe_price_id = existing.id
    elif existing:
        # Authored price changed - rotating to a new price and moving the lookup key:
        product_id = _ensure_product(stripe, item, existing.product)
        new_price = _create_price(stripe, item, product_id, transfer_lookup_key=True)
        stripe.Price.modify(existing.id, active=False)
        item.stripe_product_id = product_id
        item.stripe_price_id = new_price.id
    else:
        product_id = _ensure_product(stripe, item, None)
        new_price = _create_price(stripe, item, product_id, transfer_lookup_key=False)
        item.stripe_product_id = product_id
        item.stripe_price_id = new_price.id

    _ensure_feature(stripe, item, item.stripe_product_id)
    return item.stripe_price_id


def reconcile_catalog_with_stripe(items: list[CatalogItem], sdk=None) -> None:
    """Reconciles every catalog item into Stripe; failures are logged per item.

    Runtime callers omit `sdk` and the whole pass is skipped unless payments are
    FULLY configured (fail-closed). The bootstrap passes its provisioning SDK,
    since it runs before the webhook secret it emits can exist.
    """
    if sdk is None and not stripe_is_configured():
        logger.info("Stripe not fully configured; skipping catalog reconcile")
        return

    for item in items:
        try:
            ensure_stripe_price(item, sdk=sdk)
            logger.info("Stripe catalog item ready: %s (price=%s)", item.key, item.stripe_price_id)
        except Exception:
            logger.exception("Failed to reconcile Stripe catalog item: %s", item.key)
