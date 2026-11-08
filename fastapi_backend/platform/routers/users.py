from fastapi import APIRouter, BackgroundTasks, File, Query, Response, UploadFile, status as st
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request

from infrastructure.rate_limiter import limiter
from platform.schemas import (
    AccessibilityPreferencesResponse,
    AccessibilityPreferencesUpdateRequest,
    CreateUserRequest,
    Email,
    EmailCooldownResponse,
    EmailExistsResponse,
    NameCheckRequest,
    NameCheckResponse,
    ResetPasswordRequest,
    ResetPasswordVerifyRequest,
    UpdateUserRequest,
    UserResponse,
    UserVerificationRequest,
)
from infrastructure.dependencies import db_dependency, unverified_user_dependency, user_dependency
from platform.exceptions import NameNotAppropriateException
from platform.services import accessibility_service as acs
from platform.services import question_flow_service as qfs
from platform.services import user_service as us


router = APIRouter(prefix="/user", tags=["User"])


@router.post("/", response_model=UserResponse, status_code=st.HTTP_201_CREATED)
@limiter.limit("10/minute, 30/hour, 100/day")
def create_user(db: db_dependency, user_data: CreateUserRequest, background_tasks: BackgroundTasks, request: Request, response: Response):
    return us.create_user(db, user_data, background_tasks)


@router.post("/request-verification", response_model=EmailCooldownResponse, status_code=st.HTTP_200_OK)
@limiter.limit("1/second, 5/minute, 15/hour, 50/day")
def request_user_verification(db: db_dependency, user: unverified_user_dependency, background_tasks: BackgroundTasks, request: Request, response: Response):
    return us.request_user_verification(db, user, background_tasks)


@router.post("/verify", response_model=UserResponse, status_code=st.HTTP_200_OK)
@limiter.limit("5/minute, 15/hour, 50/day")
def verify_user(db: db_dependency, user: unverified_user_dependency, verification_request: UserVerificationRequest, request: Request, response: Response):
    return us.verify_user(db, user, verification_request.code)


@router.get("/", response_model=UserResponse, status_code=st.HTTP_200_OK)
def read_current_user(db: db_dependency, user: unverified_user_dependency, request: Request):
    """The account contract, plus question-flow completion and per-question progress.

    This is the ONE producer of UserResponse that computes `flows` and
    `flow_questions`. Every other one (login, verify, avatar upload, ...) leaves
    both null, which the client reads as "not computed" rather than "not
    completed" — so a login response can never bounce a user back through
    onboarding they already finished.

    They are separate reads because they are keyed differently on purpose (see
    UserResponse.flow_questions): two indexed latest-per-flow queries for one
    account, plus deriving which questions each stored snapshot answers.
    Deliberately not cached in v1."""
    user.flows_summary = qfs.completion_summary(db, user.id)
    user.flow_questions = qfs.question_progress(db, user.id)
    return user


@router.patch("/", response_model=UserResponse, status_code=st.HTTP_200_OK)
def update_user(db: db_dependency, user: user_dependency, user_data: UpdateUserRequest, request: Request):
    return us.update_user(db, user, user_data.name, user_data.marketing_consent, user_data.show_on_leaderboard)


@router.post("/name/check", response_model=NameCheckResponse, status_code=st.HTTP_200_OK)
@limiter.limit("20/minute, 100/hour")
def check_display_name(db: db_dependency, user: user_dependency, name_data: NameCheckRequest, request: Request, response: Response):
    """Vet a requested display name (moderation + casing) without changing it, so
    the client can block an inappropriate name before opening the support flow."""
    appropriate, formatted = us.check_display_name(name_data.name)
    return NameCheckResponse(
        appropriate=appropriate,
        formatted=formatted,
        message=None if appropriate else NameNotAppropriateException().detail,
    )


@router.post("/avatar", response_model=UserResponse, status_code=st.HTTP_200_OK)
@limiter.limit("10/minute, 60/hour")
async def upload_avatar(
    db: db_dependency,
    user: user_dependency,
    request: Request,
    response: Response,
    file: UploadFile = File(...),
):
    data = await file.read()
    # Off the event loop: store_avatar's Put is a blocking boto3 round trip to
    # object storage now that a bucket backs it, so inline it would park this
    # `async def` route's worker on network I/O and stall every other request
    # the worker is serving. Local disk made this look free; a bucket does not.
    # The default threadpool is right here — unlike the CV rebuild this is I/O
    # rather than a hundreds-of-MB allocation, so it needs no capacity bound.
    return await run_in_threadpool(us.store_avatar, db, user, data, file.content_type)


@router.delete("/", status_code=st.HTTP_204_NO_CONTENT)
def delete_user(db: db_dependency, user: user_dependency, request: Request):
    us.delete_user(db, user)


@router.post("/request-password-reset", status_code=st.HTTP_202_ACCEPTED, response_model=EmailCooldownResponse)
@limiter.limit("2/second, 10/minute, 30/hour, 50/day")
def request_password_reset(db: db_dependency, reset_request: ResetPasswordRequest, background_tasks: BackgroundTasks, request: Request, response: Response):
    return us.request_password_reset(db, reset_request.email, background_tasks)


@router.post("/reset-password", response_model=UserResponse, status_code=st.HTTP_200_OK)
@limiter.limit("10/minute, 30/hour, 50/day")
def reset_password(db: db_dependency, password_data: ResetPasswordVerifyRequest, request: Request, response: Response):
    return us.reset_password(db, password_data.email, password_data.code, password_data.new_password)


@router.get("/exists", response_model=EmailExistsResponse, status_code=st.HTTP_200_OK)
@limiter.limit("2/second, 30/minute, 200/hour, 500/day")
def email_exists(db: db_dependency, request: Request, response: Response, email: Email = Query(...)):
    return us.email_exists(db, str(email))


@router.get("/accessibility", response_model=AccessibilityPreferencesResponse, status_code=st.HTTP_200_OK)
@limiter.limit("60/minute")
def get_accessibility_preferences(db: db_dependency, user: user_dependency, request: Request, response: Response):
    return acs.get_preferences(db, user)


@router.put("/accessibility", response_model=AccessibilityPreferencesResponse, status_code=st.HTTP_200_OK)
@limiter.limit("60/minute")
def put_accessibility_preferences(
    db: db_dependency,
    user: user_dependency,
    payload: AccessibilityPreferencesUpdateRequest,
    request: Request,
    response: Response,
):
    return acs.upsert_preferences(db, user, payload)
