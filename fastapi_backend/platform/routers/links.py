"""Public resolve + operator CRUD for managed short links."""

from typing import List

from fastapi import APIRouter, Request, Response

from infrastructure.dependencies import db_dependency, require_capability
from infrastructure.rate_limiter import limiter
from platform.capabilities import LINKS_MANAGE
from platform.schemas import (
    ManagedLinkCreateRequest,
    ManagedLinkResolveResponse,
    ManagedLinkResponse,
    ManagedLinkUpdateRequest,
)
from platform.services import link_service as ls


router = APIRouter(prefix="/links", tags=["Links"])

links_manager_dependency = require_capability(LINKS_MANAGE)


@router.get("", response_model=List[ManagedLinkResponse])
@limiter.limit("30/minute, 200/hour")
def list_managed_links(
    request: Request,
    response: Response,
    db: db_dependency,
    _: links_manager_dependency,
):
    return ls.list_links(db)


@router.post("", response_model=ManagedLinkResponse)
@limiter.limit("20/minute, 100/hour")
def create_managed_link(
    request: Request,
    response: Response,
    db: db_dependency,
    _: links_manager_dependency,
    body: ManagedLinkCreateRequest,
):
    return ls.create_link(db, body)


@router.get("/{key}", response_model=ManagedLinkResolveResponse)
@limiter.limit("60/minute, 600/hour")
def resolve_managed_link(request: Request, response: Response, db: db_dependency, key: str):
    return ls.resolve_link(db, key)


@router.patch("/{key}", response_model=ManagedLinkResponse)
@limiter.limit("30/minute, 200/hour")
def update_managed_link(
    request: Request,
    response: Response,
    db: db_dependency,
    _: links_manager_dependency,
    key: str,
    body: ManagedLinkUpdateRequest,
):
    return ls.update_link(db, key, body)


@router.delete("/{key}", status_code=204)
@limiter.limit("30/minute, 200/hour")
def delete_managed_link(
    request: Request,
    response: Response,
    db: db_dependency,
    _: links_manager_dependency,
    key: str,
):
    ls.delete_link(db, key)
