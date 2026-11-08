from fastapi import APIRouter

from platform.routers.root import router as root_router
from platform.routers.auth import router as auth_router
from platform.routers.users import router as users_router
from platform.routers.contact import router as contact_router
from platform.routers.payments import router as payments_router
from platform.routers.inbox import router as inbox_router
from platform.routers.admin import router as admin_router
from platform.routers.marketing import router as marketing_router
from platform.routers.partnerships import router as partnerships_router
from platform.routers.notifications import router as notifications_router
from platform.routers.question_flows import router as question_flows_router
from platform.routers.webhooks import router as webhooks_router
from platform.routers.links import router as links_router
from platform.services.generative_ai_service.routers import router as ai_router


router = APIRouter()
router.include_router(root_router)
router.include_router(auth_router)
router.include_router(users_router)
router.include_router(contact_router)
router.include_router(payments_router)
router.include_router(inbox_router)
router.include_router(admin_router)
router.include_router(marketing_router)
router.include_router(partnerships_router)
router.include_router(notifications_router)
router.include_router(question_flows_router)
router.include_router(webhooks_router)
router.include_router(links_router)
router.include_router(ai_router)
