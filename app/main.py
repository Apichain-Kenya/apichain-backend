import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.exc import DataError

from app.config import settings
from app.errors import APIError, api_error_handler, data_error_handler
from app.middleware.body_limit import UploadSizeLimit
from app.middleware.request_id import RequestId
from app.middleware.security_headers import SecurityHeaders
from app.observability import configure_logging, init_sentry
from app.routers import ops, v2_router

logger = logging.getLogger("apichain")

# Before the app exists: uvicorn has configured its loggers by the time it
# imports this module, and Sentry's FastAPI integration must be in place
# before the first request (P5-C).
configure_logging()
init_sentry()


app = FastAPI(
    title="ApiChain v2 Backend",
    description="Honey provenance and traceability: hash-chained audit log, "
    "Merkle anchoring via OpenTimestamps, PostGIS geospatial records.",
    version="2.0.0",
)
# No lifespan scheduler: every background job runs in the worker process,
# `python -m app.worker` (P5-D, 14 D9).

app.add_exception_handler(APIError, api_error_handler)
app.add_exception_handler(DataError, data_error_handler)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in settings.frontend_origins.split(",") if o.strip()],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Bounds what the server will receive on the upload route at all: FastAPI
# spools a whole multipart body before any handler runs (P3b-E).
app.add_middleware(UploadSizeLimit)

# Every answer gets the headers, a 413 from the size limit included (P5-B).
app.add_middleware(SecurityHeaders)

# Outermost, so the id is bound before anything else runs and every response
# that passes through user middleware carries it (P5-C).
app.add_middleware(RequestId)

app.include_router(v2_router)
app.include_router(ops.router)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}
