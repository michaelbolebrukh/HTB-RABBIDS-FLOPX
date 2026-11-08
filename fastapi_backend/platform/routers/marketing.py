from fastapi import APIRouter, Query, Response
from fastapi import status as st
from pydantic import BaseModel, EmailStr, Field

from infrastructure.dependencies import db_dependency, require_capability
from platform.capabilities import MARKETING_MANAGE
from platform.services import marketing_service as ms


router = APIRouter(prefix="/marketing", tags=["Marketing"])

# Managing the do-not-send list touches raw recipient addresses, so it requires
# the marketing:manage grant (admins hold all capabilities implicitly):
marketing_manager_dependency = require_capability(MARKETING_MANAGE)


class UnsubscribeTokenRequest(BaseModel):
    token: str = Field(..., min_length=1, max_length=256)


class MarketingConsentResponse(BaseModel):
    """Masked so the response never exposes the full address to a shared link."""
    email_masked: str
    marketing_consent: bool


class SuppressionRequest(BaseModel):
    email: EmailStr
    reason: str = Field("manual", max_length=32)


class SuppressionListResponse(BaseModel):
    emails: list[str]


# -- Public, token-authorized (no login) --------------------------------------

@router.post("/unsubscribe", response_model=MarketingConsentResponse, status_code=st.HTTP_200_OK)
def unsubscribe(body: UnsubscribeTokenRequest, db: db_dependency):
    masked = ms.apply_unsubscribe_token(db, body.token, suppressed=True)
    return MarketingConsentResponse(email_masked=masked, marketing_consent=False)


@router.post("/resubscribe", response_model=MarketingConsentResponse, status_code=st.HTTP_200_OK)
def resubscribe(body: UnsubscribeTokenRequest, db: db_dependency):
    masked = ms.apply_unsubscribe_token(db, body.token, suppressed=False)
    return MarketingConsentResponse(email_masked=masked, marketing_consent=True)


@router.post("/unsubscribe/one-click", status_code=st.HTTP_200_OK)
def one_click_unsubscribe(db: db_dependency, token: str = Query(..., min_length=1, max_length=256)):
    """RFC 8058 target: mail clients POST here directly from their native
    Unsubscribe button, so this acts immediately with no confirm step. Accepts
    both user- and email-keyed tokens (bulk recipients are addresses, not users)."""
    ms.apply_unsubscribe_token(db, token, suppressed=True)
    return Response(status_code=st.HTTP_200_OK)


# -- Suppression management (marketing:manage) ---------------------------------------------

@router.get("/suppressions", response_model=SuppressionListResponse, status_code=st.HTTP_200_OK)
def get_suppressions(db: db_dependency, _: marketing_manager_dependency,
                     limit: int = Query(1000, ge=1, le=5000), offset: int = Query(0, ge=0)):
    return SuppressionListResponse(emails=ms.list_suppressions(db, limit=limit, offset=offset))


@router.post("/suppressions", status_code=st.HTTP_204_NO_CONTENT)
def add_suppression(body: SuppressionRequest, db: db_dependency, _: marketing_manager_dependency):
    ms.suppress_email(db, body.email, reason=body.reason)
    return Response(status_code=st.HTTP_204_NO_CONTENT)


@router.delete("/suppressions/{email}", status_code=st.HTTP_204_NO_CONTENT)
def remove_suppression(email: str, db: db_dependency, _: marketing_manager_dependency):
    ms.unsuppress_email(db, email)
    return Response(status_code=st.HTTP_204_NO_CONTENT)


# -- Ops export (mailbox-ops read-back) ----------------------------------------

@router.get("/suppressions/export", response_model=SuppressionListResponse, status_code=st.HTTP_200_OK)
def export_suppressions(db: db_dependency, _: marketing_manager_dependency):
    """Read the whole current do-not-send list in one call, so an ops tool (the
    mailbox-ops skill) can honour web unsubscribes at send time.

    Same raw recipient addresses as its `GET /suppressions` sibling, so it
    carries the same single gate: `marketing:manage`. A machine consumer is a
    service account holding that grant — never a second way in. It previously
    accepted a timestamped HASH_KEY signature instead, which was a third
    authorization primitive with no account and no audit attribution behind
    it; authorization here is one capability check, like everywhere else."""
    return SuppressionListResponse(emails=ms.list_suppressions(db, limit=5000))
