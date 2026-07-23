from fastapi import APIRouter

from app.routers import auth, batches, farmers, meta

# All v2 endpoints mount under this router.
v2_router = APIRouter(prefix="/v2")
v2_router.include_router(meta.router)
v2_router.include_router(auth.router)
v2_router.include_router(farmers.router)
v2_router.include_router(batches.router)
