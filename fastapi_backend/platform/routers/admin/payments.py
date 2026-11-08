from fastapi import APIRouter, status as st
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import Response

from infrastructure.rate_limiter import limiter
from infrastructure.dependencies import db_dependency, require_capability
from platform.capabilities import PAYMENTS_RECONCILE
from platform.schemas import ReconcileSummaryResponse
from platform.services.payment_services import reconcile_with_stripe


router = APIRouter(prefix="/admin/payments", tags=["Admin - Payments"])

# Reconciling against Stripe mutates the purchase mirror, so it requires the
# payments:reconcile grant (admins hold all capabilities implicitly):
payments_reconciler_dependency = require_capability(PAYMENTS_RECONCILE)


@router.post("/reconcile", response_model=ReconcileSummaryResponse, status_code=st.HTTP_200_OK)
@limiter.limit("4/minute, 20/hour")
async def reconcile_payments(_: payments_reconciler_dependency, db: db_dependency, request: Request, response: Response):
    # Offloading the Stripe-heavy sweep to the threadpool:
    return await run_in_threadpool(reconcile_with_stripe, db)
