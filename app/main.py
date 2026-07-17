from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.routers import v2_router

app = FastAPI(
    title="ApiChain v2 Backend",
    description="Honey provenance and traceability: hash-chained audit log, "
    "Merkle anchoring via OpenTimestamps, PostGIS geospatial records.",
    version="2.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in settings.frontend_origins.split(",") if o.strip()],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(v2_router)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}
