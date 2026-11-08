from fastapi import APIRouter, status as st
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import Response

from infrastructure.rate_limiter import limiter
from infrastructure.dependencies import db_dependency, user_dependency, require_capability
from platform.capabilities import PAYMENTS_QA_RESET
from platform.schemas import (
    CatalogItemResponse,
    CheckoutSessionRequest,
    CheckoutSessionResponse,
    PortalSessionResponse,
    PaymentsMeResponse,
)
import platform.services.payment_services as ps


router = APIRouter(prefix="/payments", tags=["Payments"])


@router.get("/catalog", response_model=list[CatalogItemResponse], status_code=st.HTTP_200_OK)
@limiter.limit("60/minute, 1000/hour")
def get_catalog(request: Request, response: Response):
    return ps.list_catalog_items()


@router.get("/catalog/{item_key}", response_model=CatalogItemResponse, status_code=st.HTTP_200_OK)
@limiter.limit("60/minute, 1000/hour")
def get_catalog_item(item_key: str, request: Request, response: Response):
    return ps.get_public_catalog_item(item_key)


@router.post("/checkout", response_model=CheckoutSessionResponse, status_code=st.HTTP_201_CREATED)
@limiter.limit("10/minute, 60/hour")
def create_checkout_session(user: user_dependency, db: db_dependency, body: CheckoutSessionRequest, request: Request, response: Response):
    return CheckoutSessionResponse(url=ps.create_checkout_session(db, user, body, request.headers.get("origin")))


@router.post("/portal", response_model=PortalSessionResponse, status_code=st.HTTP_201_CREATED)
@limiter.limit("10/minute, 60/hour")
def create_portal_session(user: user_dependency, db: db_dependency, request: Request, response: Response):
    return PortalSessionResponse(url=ps.create_portal_session(db, user, request.headers.get("origin")))


@router.get("/me", response_model=PaymentsMeResponse, status_code=st.HTTP_200_OK)
@limiter.limit("60/minute, 1000/hour")
def get_payments_me(user: user_dependency, db: db_dependency, request: Request, response: Response):
    return ps.get_payments_me(db, user)


# TODO TEMP (QA harness) - remove before launch, with reset_own_subscription and the
# frontend's unused `resetSubscription` (platform/api/payments.ts). Its only UI, the
# pricing page's temp "delete entitlement" button, went with the pricing table on
# 2026-09-22, so this is now reachable only by a direct call. Gated payments:qa-reset: deleting a
# paid-for entitlement is destructive and has no legitimate self-service case (a member
# who wants out cancels via the billing portal), so only an admin — or an explicitly
# QA-granted account — can wipe their own subscription to re-test the purchase flow:
payments_qa_reset_dependency = require_capability(PAYMENTS_QA_RESET)


@router.post("/test/reset", status_code=st.HTTP_200_OK)
@limiter.limit("10/minute")
def reset_own_subscription(user: payments_qa_reset_dependency, db: db_dependency, request: Request, response: Response):
    return ps.reset_own_subscription(db, user)


@router.post("/stripe/webhook", status_code=st.HTTP_200_OK, include_in_schema=False)
@limiter.limit("240/minute")
async def stripe_webhook(db: db_dependency, request: Request, response: Response):
    payload = await request.body()
    # Offloading the synchronous handler (DB + Stripe calls) to the threadpool
    # so webhook bursts don't block the event loop:
    await run_in_threadpool(ps.handle_webhook, db, payload, request.headers.get("stripe-signature"))
    return {"received": True}
