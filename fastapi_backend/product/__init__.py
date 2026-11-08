"""Replaceable product-instance boundary: FLOPX, the compute-market terminal.

The domain lives in `product.flopx` (data model, provenance envelope, sample
datasets, source adapters, index, forecasting, strategies, paper broker,
procurement planner, query grammar) and is exposed through `product.routers`
under the `/flopx` prefix. The stable exports infrastructure composes against
are unchanged from the factory contract: `product.routers.router`,
`product.startup.STARTUP_TASKS`, `product.models`, `product.organisations`,
`product.payments.CATALOG`.
"""
