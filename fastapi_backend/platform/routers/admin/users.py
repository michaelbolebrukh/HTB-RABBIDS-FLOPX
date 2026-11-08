from fastapi import APIRouter, status as st, Query
from pydantic import EmailStr
from starlette.requests import Request
from starlette.responses import Response

from infrastructure.rate_limiter import limiter
from infrastructure.dependencies import db_dependency, require_capability
from platform.capabilities import USERS_READ
from platform.schemas import EmailListResponse, UserResponse, Email
from platform.enums import UserListFilter
from platform.services import user_service as us


router = APIRouter(prefix="/admin/users", tags=["Admin - Users"])

# Reading account PII (emails, verification state) requires the users:read
# grant (admins hold all capabilities implicitly):
users_reader_dependency = require_capability(USERS_READ)


@router.get("/emails", response_model=EmailListResponse, status_code=st.HTTP_200_OK)
@limiter.limit("30/minute, 200/hour")
def list_user_emails(
    _: users_reader_dependency,
    db: db_dependency,
    request: Request,
    response: Response,
    filter: UserListFilter = Query(UserListFilter.ALL),
):
    return us.list_user_emails(db, filter)


@router.get("/by-email", response_model=UserResponse, status_code=st.HTTP_200_OK)
@limiter.limit("30/minute, 200/hour")
def get_user_by_email(
    _: users_reader_dependency,
    db: db_dependency,
    request: Request,
    response: Response,
    email: Email = Query(...),
):
    return us.get_user_by_email(db, str(email))


