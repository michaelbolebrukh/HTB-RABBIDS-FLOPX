"""One-time, idempotent Stripe account bootstrap for a startup instance.

Run from `fastapi_backend/` with STRIPE_SECRET_KEY available (env or .env):

    python scripts/stripe_bootstrap.py --api-origin https://your-backend.onrender.com

What it does (all via API - no dashboard product configuration):
  1. Registers the webhook endpoint for /payments/stripe/webhook and prints the
     signing secret (put it in the STRIPE_WEBHOOK_SECRET env var).
  2. Creates/updates a default Customer Portal configuration.
  3. Reconciles the code-defined catalog into Stripe (same as app startup).
  4. Prints the remaining one-time dashboard checklist (email toggles etc.).
"""
import os
import sys
import shutil
import tempfile
import argparse

# Making backend-root imports work when run as a script:
BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND_ROOT)

from dotenv import load_dotenv
load_dotenv(os.path.join(BACKEND_ROOT, ".env"))

BRANDING_DIR = os.path.join(
    BACKEND_ROOT, "platform", "services", "payment_services", "branding"
)


DASHBOARD_CHECKLIST = """
================= ONE-TIME STRIPE DASHBOARD CHECKLIST =================
(per Stripe account - these toggles have no API; ~5 minutes)

 1. Complete KYC / bank payout details.
 2. Settings -> Emails: toggle ON "Successful payments" and "Refunds".
 3. Settings -> Billing -> Automatic: enable failed-payment emails,
    renewal reminders, expiring-card notices; set the "Manage
    subscription" link to the Customer Portal.
 4. Settings -> Branding: set the PARENT brand (logo + colour). This
    governs receipts, the customer portal, and Stripe emails. The
    checkout PAGE brand (colours/font/name/logo) is set per session in
    code - nothing to configure here for it.
 5. Settings -> Public details: verify support email / website.
 6. Profile -> Notifications: enable "Successful payments" emails to
    yourself if you want per-order (e.g. merch fulfilment) alerts.
 7. Put the STRIPE_WEBHOOK_SECRET printed above into the backend env.
=======================================================================
"""


def ensure_webhook_endpoint(stripe, api_origin: str) -> None:
    """Creates or updates the backend webhook endpoint and prints its secret"""
    # Deriving the event list from the handler map keeps the subscription in lockstep
    # with what the backend actually processes (single source of truth):
    from platform.services.payment_services.webhook_service import SUBSCRIBED_WEBHOOK_EVENTS
    events = list(SUBSCRIBED_WEBHOOK_EVENTS)
    url = f"{api_origin.rstrip('/')}/payments/stripe/webhook"

    endpoints = stripe.WebhookEndpoint.list(limit=100)
    existing = next((e for e in endpoints.data if e.url == url), None)

    if existing:
        stripe.WebhookEndpoint.modify(existing.id, enabled_events=events)
        print(f"Webhook endpoint already registered: {url}")
        print("(Signing secret is only revealed at creation - reuse the stored one, or delete and re-run.)")
    else:
        endpoint = stripe.WebhookEndpoint.create(url=url, enabled_events=events)
        print(f"Webhook endpoint created: {url}")
        print(f"STRIPE_WEBHOOK_SECRET={endpoint.secret}")


def ensure_portal_configuration(stripe, frontend_origin: str) -> None:
    """Creates a default Customer Portal configuration if none exists"""
    configs = stripe.billing_portal.Configuration.list(limit=1)
    if configs.data:
        print("Customer Portal configuration already exists.")
        return

    stripe.billing_portal.Configuration.create(
        business_profile={"headline": None},
        default_return_url=frontend_origin,
        features={
            "invoice_history": {"enabled": True},
            "payment_method_update": {"enabled": True},
            "subscription_cancel": {"enabled": True, "mode": "at_period_end"},
        },
    )
    print("Customer Portal configuration created.")


def ensure_checkout_logo(stripe) -> None:
    """Uploads the generated checkout logo (idempotent).

    The backend resolves this File at runtime by its per-instance filename, so
    there is nothing to copy into env after upload. The filename keeps re-runs
    idempotent and isolates instances that share one Stripe account.
    """
    from platform.services.payment_services.branding_service import checkout_logo_filename

    logo_path = os.path.join(BRANDING_DIR, "checkout_logo.png")
    if not os.path.exists(logo_path):
        print("Checkout logo asset not found (run `npm run build:stripe-branding`); skipping logo upload.")
        return

    filename = checkout_logo_filename()

    # Paginating the idempotency check: on a shared account with many logo files
    # the target can be beyond page one, and a page-one-only check would wrongly
    # re-upload a duplicate on every run:
    existing = stripe.File.list(purpose="business_logo", limit=100).auto_paging_iter()
    match = next((f for f in existing if getattr(f, "filename", None) == filename), None)
    if match:
        print(f"Checkout logo already uploaded ({filename}).")
        return

    # Uploading under the per-instance filename by copying to a temp file whose
    # basename Stripe uses as the File's filename:
    tmp_dir = tempfile.mkdtemp()
    try:
        tmp_path = os.path.join(tmp_dir, filename)
        shutil.copyfile(logo_path, tmp_path)
        with open(tmp_path, "rb") as fp:
            stripe.File.create(purpose="business_logo", file=fp)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    print(f"Checkout logo uploaded ({filename}).")


def main() -> None:
    parser = argparse.ArgumentParser(description="Idempotent Stripe account bootstrap")
    parser.add_argument("--api-origin", required=True, help="Public origin of the deployed backend API")
    args = parser.parse_args()

    from infrastructure.settings import settings as s
    # Provisioning accessor: this script CREATES the webhook endpoint, so it runs
    # before the STRIPE_WEBHOOK_SECRET it emits can exist. Runtime code uses the
    # fail-closed get_stripe() instead:
    from platform.services.payment_services.stripe_gateway import get_stripe_for_provisioning
    from platform.services.payment_services import reconcile_catalog_with_stripe
    from product.payments import CATALOG

    stripe = get_stripe_for_provisioning()

    ensure_webhook_endpoint(stripe, args.api_origin)
    ensure_portal_configuration(stripe, s.PUBLIC_FRONTEND_ORIGIN)

    if s.STRIPE_CHECKOUT_BRANDING:
        ensure_checkout_logo(stripe)

    print("Reconciling catalog into Stripe...")
    reconcile_catalog_with_stripe(CATALOG, sdk=stripe)

    print(DASHBOARD_CHECKLIST)


if __name__ == "__main__":
    main()
