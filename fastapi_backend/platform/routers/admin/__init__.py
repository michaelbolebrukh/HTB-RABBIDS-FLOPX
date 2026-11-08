from fastapi import APIRouter

from platform.routers.admin.contact import router as contact_router
from platform.routers.admin.payments import router as payments_router
from platform.routers.admin.users import router as users_router


router = APIRouter()
router.include_router(contact_router)
router.include_router(payments_router)
router.include_router(users_router)
