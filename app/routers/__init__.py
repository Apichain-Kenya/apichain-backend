from fastapi import APIRouter

from app.routers import meta

# All v2 endpoints mount under this router. Phase 1 adds the real routers.
v2_router = APIRouter(prefix="/v2")
v2_router.include_router(meta.router)
