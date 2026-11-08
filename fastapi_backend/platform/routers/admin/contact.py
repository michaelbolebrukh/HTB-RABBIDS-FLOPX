from typing import List, Optional

from fastapi import APIRouter, Query, status as st

from infrastructure.dependencies import db_dependency, require_capability
from platform.capabilities import CONTACT_MANAGE
from platform.schemas import ContactSubmissionResponse
from platform.services import contact_service as cs


router = APIRouter(prefix="/admin/contact", tags=["Admin - Contact"])

# Contact submissions carry visitor PII, so the whole surface requires the
# contact:manage grant (admins hold all capabilities implicitly):
contact_manager_dependency = require_capability(CONTACT_MANAGE)


@router.get("/", response_model=List[ContactSubmissionResponse])
def get_contact_submissions(
    db: db_dependency,
    _: contact_manager_dependency,
    resolved: Optional[bool] = Query(None, description="Filter by resolved status (true/false), None for all"),
):
    return cs.get_contact_submissions(db, resolved)


@router.get("/{contact_id}", response_model=ContactSubmissionResponse)
def get_contact_submission(db: db_dependency, _: contact_manager_dependency, contact_id: int):
    return cs.get_contact_submission(db, contact_id)


@router.patch("/{contact_id}/resolve", response_model=ContactSubmissionResponse)
def resolve_contact_submission(db: db_dependency, _: contact_manager_dependency, contact_id: int):
    return cs.resolve_contact_submission(db, contact_id)


@router.delete("/{contact_id}", status_code=st.HTTP_204_NO_CONTENT)
def delete_contact_submission(db: db_dependency, _: contact_manager_dependency, contact_id: int):
    cs.delete_contact_submission(db, contact_id)
