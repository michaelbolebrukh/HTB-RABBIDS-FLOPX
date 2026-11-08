"""User-facing web push + operator broadcast.

Push subscription is open to any authenticated user so an end user can opt a
device into announcements. Broadcasts require the notifications:broadcast
capability (the seeded service account holds it, so agents can drive it; admins
pass implicitly) and carry the full Web Notification API surface, so "push 'new
season live now' → /learn" is a single authenticated call — no composer UI needed.
"""

from fastapi import APIRouter, Request, Response
from fastapi import status as st
from pydantic import BaseModel, Field

from infrastructure.dependencies import db_dependency, require_capability, user_dependency
from infrastructure.rate_limiter import limiter
from platform.capabilities import NOTIFICATIONS_BROADCAST
from platform.services.notification_service import push as push_service
from platform.services.notification_service.push import PushNotification


router = APIRouter(prefix="/notifications", tags=["Notifications"])

# Broadcasting reaches every opted-in device, so it requires the
# notifications:broadcast grant (admins hold all capabilities implicitly):
broadcaster_dependency = require_capability(NOTIFICATIONS_BROADCAST)


# -- schemas -------------------------------------------------------------------

class PushKeyResponse(BaseModel):
    key: str


class PushSubscribeRequest(BaseModel):
    endpoint: str = Field(..., min_length=1, max_length=2048)
    p256dh: str = Field(..., min_length=1, max_length=512)
    auth: str = Field(..., min_length=1, max_length=512)


class PushUnsubscribeRequest(BaseModel):
    endpoint: str = Field(..., min_length=1, max_length=2048)


class BroadcastAction(BaseModel):
    action: str = Field(..., min_length=1, max_length=64)
    title: str = Field(..., min_length=1, max_length=64)
    url: str | None = Field(None, max_length=2048)
    icon: str | None = Field(None, max_length=2048)


class BroadcastRequest(BaseModel):
    """The full notification the announcement should render on each device."""
    title: str = Field(..., min_length=1, max_length=120)
    body: str = Field("", max_length=500)
    url: str = Field("/", max_length=2048)
    icon: str | None = Field(None, max_length=2048)
    badge: str | None = Field(None, max_length=2048)
    image: str | None = Field(None, max_length=2048)
    tag: str | None = Field(None, max_length=64)
    renotify: bool = False
    require_interaction: bool = False
    silent: bool = False
    vibrate: list[int] | None = None
    lang: str | None = Field(None, max_length=16)
    dir: str | None = Field(None, max_length=8)
    actions: list[BroadcastAction] = Field(default_factory=list, max_length=2)
    # Optional explicit target cohort: only these users' devices are pushed. This
    # is the escape hatch for conditions no built-in filter captures — an operator
    # (or an AI agent) computes the segment and passes the addresses. Omit to
    # reach every opted-in device.
    emails: list[str] | None = Field(None, max_length=50000)


class BroadcastResponse(BaseModel):
    sent: int


# -- user push subscription ----------------------------------------------------

@router.get("/push/key", response_model=PushKeyResponse, status_code=st.HTTP_200_OK)
def get_push_key(user: user_dependency, db: db_dependency):
    _private, public = push_service.get_vapid_keys(db)
    return PushKeyResponse(key=public)


@router.post("/push/subscribe", status_code=st.HTTP_204_NO_CONTENT)
@limiter.limit("20/minute")
def subscribe_push(user: user_dependency, db: db_dependency, request: Request,
                   response: Response, payload: PushSubscribeRequest):
    push_service.subscribe(db, user.id, payload.endpoint, payload.p256dh, payload.auth)


@router.post("/push/unsubscribe", status_code=st.HTTP_204_NO_CONTENT)
@limiter.limit("20/minute")
def unsubscribe_push(user: user_dependency, db: db_dependency, request: Request,
                     response: Response, payload: PushUnsubscribeRequest):
    push_service.unsubscribe(db, payload.endpoint)


# -- broadcast (notifications:broadcast) ---------------------------------------

@router.post("/broadcast", response_model=BroadcastResponse, status_code=st.HTTP_200_OK)
# A broadcast hits every opted-in device and cannot be recalled, so the grant is
# backed by a rate ceiling: a loop or a leaked token gets a handful of sends, not
# a notification storm. Generous for a human/agent operator, who sends a few:
@limiter.limit("3/minute, 20/hour")
def broadcast(body: BroadcastRequest, db: db_dependency, _user: broadcaster_dependency,
              request: Request, response: Response):
    notification = PushNotification(
        title=body.title, body=body.body, url=body.url, icon=body.icon, badge=body.badge,
        image=body.image, tag=body.tag, renotify=body.renotify,
        require_interaction=body.require_interaction, silent=body.silent,
        vibrate=body.vibrate, lang=body.lang, dir=body.dir,
        actions=[a.model_dump(exclude_none=True) for a in body.actions],
    )
    return BroadcastResponse(sent=push_service.send_broadcast(db, notification, emails=body.emails))
