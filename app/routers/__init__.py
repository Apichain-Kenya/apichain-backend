from fastapi import APIRouter

# All v2 endpoints mount under this router. Phase 1 adds the real routers.
v2_router = APIRouter(prefix="/v2")
