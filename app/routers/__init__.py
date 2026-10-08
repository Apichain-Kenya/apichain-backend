from fastapi import APIRouter

from app.routers import apiaries, audit, auth, batches, documents, farmers, meta, public

# All v2 endpoints mount under this router.
v2_router = APIRouter(prefix="/v2")
v2_router.include_router(meta.router)
v2_router.include_router(auth.router)
v2_router.include_router(farmers.router)
v2_router.include_router(documents.router)
v2_router.include_router(apiaries.router)
v2_router.include_router(batches.router)
v2_router.include_router(public.router)
v2_router.include_router(audit.router)
