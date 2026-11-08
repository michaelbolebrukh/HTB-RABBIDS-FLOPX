import logging

from fastapi import APIRouter, BackgroundTasks, Request, Response
from starlette import status as st

from platform.services.error_diagnosis import handle_sentry_webhook, is_enabled


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks", tags=["Webhooks"])


@router.post("/sentry", include_in_schema=False)
async def sentry_webhook(request: Request, background: BackgroundTasks) -> Response:
    """Receives Sentry issue webhooks for AI diagnosis.

    Dormant unless the diagnosis feature is configured. The raw body is read for
    HMAC verification, then processing runs off the request path so Sentry gets
    an immediate acknowledgement.
    """
    if not is_enabled():
        return Response(status_code=st.HTTP_204_NO_CONTENT)
    body = await request.body()
    signature = request.headers.get("sentry-hook-signature")
    background.add_task(handle_sentry_webhook, body, signature)
    return Response(status_code=st.HTTP_202_ACCEPTED)
