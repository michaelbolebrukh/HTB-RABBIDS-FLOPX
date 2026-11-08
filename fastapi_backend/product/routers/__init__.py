from fastapi import APIRouter

from product.routers.flopx import router as flopx_router


router = APIRouter()
router.include_router(flopx_router, prefix="/flopx")
