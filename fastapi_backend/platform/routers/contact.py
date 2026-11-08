from fastapi import APIRouter, BackgroundTasks, Response, status as st
from starlette.requests import Request

from infrastructure.rate_limiter import limiter
from infrastructure.dependencies import db_dependency, optional_user_dependency
from platform.schemas import ContactSubmissionRequest, ContactSubmissionResponse
from platform.services import contact_service as cs


router = APIRouter(prefix="/contact", tags=["Contact"])


@router.post("/", status_code=st.HTTP_201_CREATED, response_model=ContactSubmissionResponse)
@limiter.limit("4/minute, 10/hour, 20/day")
def submit_contact_form(
    submission_data: ContactSubmissionRequest,
    db: db_dependency,
    background_tasks: BackgroundTasks,
    user: optional_user_dependency,
    request: Request,
    response: Response,
):
    return cs.submit_contact_form(db, submission_data, background_tasks, user=user)
