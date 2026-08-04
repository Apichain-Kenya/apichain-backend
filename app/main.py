import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.exc import DataError

from app.config import settings
from app.database import SessionLocal
from app.errors import APIError, api_error_handler, data_error_handler
from app.routers import v2_router
from app.services import anchoring, integrity

logger = logging.getLogger("apichain")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    scheduler: BackgroundScheduler | None = None
    if settings.scheduler_enabled:
        scheduler = BackgroundScheduler()
        scheduler.add_job(
            lambda: integrity.run_check(SessionLocal),
            "interval",
            seconds=settings.integrity_check_interval_seconds,
            id="audit_integrity",
        )
        if settings.anchor_enabled:
            # Anchoring has its own switch on top of the scheduler one, so a
            # box with no outbound network can keep checking chain integrity
            # without failing an anchor run every tick (P2-E).
            scheduler.add_job(
                lambda: anchoring.run_stamp(SessionLocal),
                "interval",
                seconds=settings.anchor_interval_seconds,
                id="anchor_stamp",
            )
            # Separate job on a slower interval: Bitcoin confirmation takes
            # hours, so polling the calendars faster than that is pure waste.
            scheduler.add_job(
                lambda: anchoring.run_upgrade(SessionLocal),
                "interval",
                seconds=settings.anchor_upgrade_interval_seconds,
                id="anchor_upgrade",
            )
        scheduler.start()
        logger.info(
            "scheduler started (anchoring %s)",
            "enabled" if settings.anchor_enabled else "disabled",
        )
    try:
        yield
    finally:
        if scheduler is not None:
            scheduler.shutdown(wait=False)


app = FastAPI(
    title="ApiChain v2 Backend",
    description="Honey provenance and traceability: hash-chained audit log, "
    "Merkle anchoring via OpenTimestamps, PostGIS geospatial records.",
    version="2.0.0",
    lifespan=lifespan,
)

app.add_exception_handler(APIError, api_error_handler)
app.add_exception_handler(DataError, data_error_handler)

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
