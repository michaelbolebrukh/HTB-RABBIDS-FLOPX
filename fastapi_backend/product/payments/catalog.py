"""Product-owned payments catalog: FLOPX sells nothing (brief F15, Stripe dormant).

`register_catalog([])` is accepted by the platform (`_validate_catalog` passes
an empty list and the registry simply holds no items; `reconcile_catalog_with_
stripe([])` loops over nothing), and `scripts/stripe_bootstrap.py` reads this
export unchanged. FLOPX does not register the catalog at boot: there is no
payments startup task, so a half-configured Stripe is still shouted about by the
platform's own `validate_stripe_config` wherever it runs, but no reconcile
thread is started for an empty list.
"""

from platform.services.payment_services import CatalogItem


CATALOG: list[CatalogItem] = []
