"""In-process catalog registry for purchasable items.

The catalog is declared in code by the product boundary (see
`product/payments/catalog.py`) and registered here at startup. Stripe is the
runtime source of truth for money objects: `sync_service` reconciles Stripe
Products/Prices/Features from these definitions and caches the resulting
Stripe ids back onto each item.
"""
from dataclasses import dataclass, field
from datetime import datetime, UTC
from typing import Optional

from platform.enums import ProductType, LaunchStage
from platform.exceptions import CatalogItemNotFoundException
from infrastructure.settings import settings as s


@dataclass
class CatalogItem:
    # Stable identifier; doubles as the Stripe price lookup_key:
    key: str
    name: str
    type: ProductType
    amount_minor: int
    currency: str = "GBP"

    # Optional strikethrough/compare-at price for marketing display:
    compare_at_minor: Optional[int] = None

    # Billing interval for subscriptions ("month" | "year"); None for one-time:
    interval: Optional[str] = None

    # Stripe entitlement feature lookup_key granted while a subscription is active:
    entitlement_feature: Optional[str] = None

    launch_stage: LaunchStage = LaunchStage.PUBLIC
    per_user_limit: Optional[int] = None

    # Purchase window (item available for purchase). Not a discount window:
    purchase_valid_from: Optional[datetime] = None
    purchase_valid_to: Optional[datetime] = None

    # Physical goods collect a shipping address in Checkout:
    requires_shipping: bool = False
    shipping_countries: tuple[str, ...] = ("GB",)

    # Stripe ids cached by sync_service at startup (not authored):
    stripe_product_id: Optional[str] = field(default=None, compare=False)
    stripe_price_id: Optional[str] = field(default=None, compare=False)

    def is_purchase_window_open(self) -> bool:
        """Checks whether the item is currently inside its purchase window"""
        now_utc = datetime.now(UTC)
        if self.purchase_valid_from and now_utc < self.purchase_valid_from:
            return False
        if self.purchase_valid_to and now_utc > self.purchase_valid_to:
            return False
        return True


# Module-level registry populated once at startup by the product boundary:
_CATALOG: dict[str, CatalogItem] = {}

_VALID_INTERVALS = {None, "month", "year"}


def _validate_catalog(items: list[CatalogItem]) -> None:
    """Validates authored catalog invariants, failing loudly at registration time"""
    problems: list[str] = []
    seen_keys: set[str] = set()

    for item in items:
        if item.key in seen_keys:
            problems.append(f"duplicate catalog key: {item.key}")
        seen_keys.add(item.key)

        if item.amount_minor <= 0:
            problems.append(f"{item.key}: amount_minor must be positive")
        if item.compare_at_minor is not None and item.compare_at_minor < item.amount_minor:
            problems.append(f"{item.key}: compare_at_minor must be >= amount_minor")
        if item.currency.upper() not in s.SUPPORTED_CURRENCIES_SET:
            problems.append(f"{item.key}: unsupported currency {item.currency}")
        if item.interval not in _VALID_INTERVALS:
            problems.append(f"{item.key}: interval must be one of {sorted(i for i in _VALID_INTERVALS if i)}")
        if item.purchase_valid_from and item.purchase_valid_to and item.purchase_valid_from > item.purchase_valid_to:
            problems.append(f"{item.key}: purchase_valid_from must be before purchase_valid_to")

    if problems:
        raise ValueError("Invalid payments catalog: " + "; ".join(problems))


def register_catalog(items: list[CatalogItem]) -> None:
    """Registers the product-defined catalog (idempotent; replaces previous registration)"""
    _validate_catalog(items)
    _CATALOG.clear()
    for item in items:
        _CATALOG[item.key] = item


def get_catalog_item(key: str) -> Optional[CatalogItem]:
    return _CATALOG.get(key)


def get_public_catalog_item(key: str) -> CatalogItem:
    """Returns a publicly visible catalog item, treating hidden items as absent"""
    item = _CATALOG.get(key)
    if item is None or item.launch_stage == LaunchStage.HIDDEN:
        raise CatalogItemNotFoundException
    return item


def list_catalog_items(include_hidden: bool = False) -> list[CatalogItem]:
    """Lists catalog items; hidden items are excluded from public surfaces"""
    items = list(_CATALOG.values())
    if not include_hidden:
        items = [i for i in items if i.launch_stage != LaunchStage.HIDDEN]
    return items
