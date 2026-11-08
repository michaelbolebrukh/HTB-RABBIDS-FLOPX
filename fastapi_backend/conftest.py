"""
Backend-wide pytest bootstrap.

Pytest imports a test module's parent packages (e.g. `platform.services`)
BEFORE executing any nested conftest body, and importing those packages pulls
`infrastructure.settings`, which requires env vars at import time. Setting
safe test defaults here — the rootdir conftest loads before all of that —
so test runs never depend on a developer's local .env. Real values from the
environment always win (setdefault only fills gaps).
"""

import os


# Providing the minimal required host settings for import-time construction:
os.environ.setdefault("DATABASE_URL", "sqlite://")

# Forcing error tracking OFF for test runs (override, not setdefault: the
# container may carry a real SENTRY_DSN, and expected-failure test paths log
# at error level — test noise must never reach production Sentry):
os.environ["SENTRY_DSN"] = ""

# Forcing the document identity AI fallback OFF for test runs, same reasoning: the
# container may carry a real OPENAI_API_KEY (used to empirically verify the
# fallback against the live API while building it), and the test suite must
# never make a real network call. A cleared key makes document_identity's
# fallback fail open immediately (no key -> no call), exactly like a
# deployment with the feature dormant. Tests that DO want to exercise the
# fallback path monkeypatch `document_identity.s.OPENAI_API_KEY` back to a
# dummy value alongside a faked OpenAI client — see
# infrastructure/tests/test_document_identity_ai.py:
os.environ["OPENAI_API_KEY"] = ""
# Same reasoning for the Alpaca market-data keys: the container carries the founder's real
# keys, and `product.startup.load_research_archive` (which the flopx startup tests call) would
# otherwise fetch eight tickers of real bars from data.alpaca.markets on every run. Cleared,
# the loader reports `not-configured` and loads nothing; the tests that exercise it set dummy
# keys on the settings object beside an `httpx.MockTransport` (test_alpaca_archive.py).
os.environ["PRODUCT_ALPACA_API_KEY"] = ""
os.environ["PRODUCT_ALPACA_SECRET_KEY"] = ""
# Same for the EIA adapter (product/flopx/sources/eia.py): a developer's own key must not put the
# ERCOT and PJM series into every terminal read under test; test_eia_grid.py sets a dummy on settings.
os.environ["EIA_API_KEY"] = ""
os.environ.setdefault("HASH_KEY", "test-only-hash-key")
os.environ.setdefault("APP_DOMAIN", "test.local")

# A test run is not a production deployment, and saying so is what lets the
# suite exercise the real code paths without production-only infrastructure:
# ENV defaults to "production" in code, which (correctly) makes the durable
# storage gate refuse uploads when no bucket is configured. Tests write to a
# temp directory and need nothing to survive, so they declare development.
# Tests that assert production behaviour set ENV explicitly themselves:
os.environ.setdefault("ENV", "development")
