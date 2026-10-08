"""Farmer documents: upload, list, signed download URL (P3b-E, 04 §5.6, 11 §11).

**Upload order.** No byte reaches the bucket until consent, the size cap, the
type check and the scan have all passed:

    1. farmer exists (row locked: one upload per
       farmer at a time, so the quota holds);
       actor may act for them                      -> 404 / 403
    2. document_upload consent stands              -> 422 consent_required
    3. size (the middleware bounds the body;
       this checks the file exactly)               -> 413 file_too_large
    4. magic-byte sniff                            -> 415 unsupported_media_type
    5. sha256; same bytes already on file for
       this farmer                                 -> 200 existing row, nothing written
    6. per-farmer quota                            -> 413 quota_exceeded
    7. antivirus                                   -> 422 document_rejected /
                                                      503 scanner_unavailable
    8. object store put (idempotent by hash)       -> 503 storage_unavailable
    9. INSERT documents, flush                     -> concurrent twin: 200 existing
   10. audit_log.append(document.uploaded)         <- nothing fallible below
   11. commit

Steps 7 and 8 are network calls; both sit above the append, so a failure
burns no audit id. A database failure after step 8 can leave an orphan object.
That is harmless: it is content-addressed and the next identical upload
reuses it.

**No Idempotency-Key (11 D6).** Content addressing is the idempotency: the
same bytes for the same farmer return the existing row and write nothing.

**Reads.** The list is metadata only and is not consent-gated (it is the
farmer's own data shown to the farmer or their officer). The *bytes* are:
a signed URL is issued only for a clean document while `document_upload`
consent stands, so withdrawing consent hides a farmer's documents without
deleting them. Deletion is the Phase 5 workflow.
"""

import datetime as dt
from typing import Literal

from fastapi import APIRouter, Depends, File, Form, Path, Request, UploadFile
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.deps import requires
from app.enums import ConsentPurpose, ScanStatus
from app.errors import APIError, error_responses
from app.models import Document, Farmer, User
from app.routers._boundaries import get_object_store, get_scanner
from app.routers._context import request_context
from app.schemas.documents import (
    DocumentListResponse,
    DocumentResponse,
    DownloadUrlResponse,
)
from app.services import audit_log, consent, media, ownership
from app.services.scanner import Scanner, ScannerUnavailable
from app.services.storage import ObjectStore, StorageUnavailable

router = APIRouter(tags=["documents"])
_require_upload = requires("document.upload")
_require_read = requires("document.read")

# One Path object per parameter name: FastAPI binds a shared FieldInfo to the
# first parameter it decorates, so reusing one across names breaks the second.
_FARMER_ID = Path(ge=1, le=2_147_483_647)
_DOCUMENT_ID = Path(ge=1, le=2_147_483_647)

DocType = Literal["national_id", "kra_pin_certificate", "land_document", "farm_photo", "other"]


def _farmer_or_404(db: Session, farmer_id: int, *, lock: bool = False) -> Farmer:
    query = select(Farmer).where(Farmer.id == farmer_id)
    # FOR NO KEY UPDATE, not FOR UPDATE: it serializes this farmer's uploads
    # against each other without conflicting with the FOR KEY SHARE lock every
    # foreign-key check takes, so a 30-second scan does not block creating a
    # batch (or anything else that references this farmer).
    farmer = db.execute(
        query.with_for_update(key_share=True) if lock else query
    ).scalar_one_or_none()
    if farmer is None:
        raise APIError(404, "farmer_not_found", "Farmer does not exist", {"farmer_id": farmer_id})
    return farmer


def _existing(db: Session, farmer_id: int, content_hash: bytes) -> Document | None:
    return db.execute(
        select(Document).where(
            Document.subject_type == "farmer",
            Document.subject_id == farmer_id,
            Document.content_hash == content_hash,
        )
    ).scalar_one_or_none()


def _out(doc: Document) -> DocumentResponse:
    return DocumentResponse(
        id=doc.id,
        farmer_id=doc.subject_id,
        doc_type=doc.doc_type,
        content_type=doc.content_type,
        size_bytes=doc.size_bytes,
        original_filename=doc.original_filename,
        content_hash=doc.content_hash.hex(),
        scan_status=doc.scan_status,
        uploaded_at=doc.uploaded_at.replace(tzinfo=dt.UTC),
    )


def _replay(doc: Document) -> JSONResponse:
    return JSONResponse(status_code=200, content=_out(doc).model_dump(mode="json"))


@router.post(
    "/farmers/{farmer_id}/documents",
    response_model=DocumentResponse,
    status_code=201,
    responses={
        200: {"model": DocumentResponse, "description": "Already on file for this farmer"},
        **error_responses(401, 403, 404, 413, 415, 422, 503),
    },
)
def upload_document(
    request: Request,
    farmer_id: int = _FARMER_ID,
    file: UploadFile = File(),
    doc_type: DocType = Form(),
    db: Session = Depends(get_db),
    actor: User = Depends(_require_upload),
    store: ObjectStore = Depends(get_object_store),
    scanner: Scanner = Depends(get_scanner),
) -> DocumentResponse | JSONResponse:
    # FOR UPDATE: a farmer's uploads run one at a time, so two cannot both
    # read the quota sum before either inserts (a race the security review
    # found). Lock order is farmer row, then the audit chain lock in append.
    farmer = _farmer_or_404(db, farmer_id, lock=True)
    ownership.assert_acts_for_farmer(db, actor, farmer.id)
    consent.require_consent(
        db, subject_type="farmer", subject_id=farmer.id, purpose=ConsentPurpose.document_upload
    )

    data = file.file.read(settings.max_file_bytes + 1)
    if len(data) > settings.max_file_bytes:
        raise APIError(
            413,
            "file_too_large",
            "The file exceeds the size limit",
            {"max_file_bytes": settings.max_file_bytes},
        )

    content_type = media.sniff(data[: media.SNIFF_BYTES])
    if content_type is None:
        raise APIError(
            415,
            "unsupported_media_type",
            "Only PDF, JPEG and PNG files are accepted",
            {"allowed": sorted(media.ALLOWED_TYPES)},
        )

    content_hash = media.sha256(data)
    existing = _existing(db, farmer.id, content_hash)
    if existing is not None:
        return _replay(existing)

    held = db.execute(
        select(func.coalesce(func.sum(Document.size_bytes), 0)).where(
            Document.subject_type == "farmer", Document.subject_id == farmer.id
        )
    ).scalar_one()
    if held + len(data) > settings.max_subject_bytes:
        raise APIError(
            413,
            "quota_exceeded",
            "This farmer's document storage is full",
            {"max_subject_bytes": settings.max_subject_bytes},
        )

    try:
        verdict = scanner.scan(data)
    except ScannerUnavailable as exc:
        raise APIError(503, "scanner_unavailable", "The virus scanner is unavailable") from exc
    if not verdict.clean:
        raise APIError(422, "document_rejected", "The file failed the virus scan")

    key = media.object_key(content_hash)
    try:
        store.put(key, data, content_type)
    except StorageUnavailable as exc:
        raise APIError(503, "storage_unavailable", "Document storage is unavailable") from exc

    doc = Document(
        subject_type="farmer",
        subject_id=farmer.id,
        doc_type=doc_type,
        content_hash=content_hash,
        content_type=content_type,
        size_bytes=len(data),
        original_filename=media.sanitize_filename(file.filename),
        object_key=key,
        scan_status=ScanStatus.clean,
        scan_engine=verdict.engine,
        scanned_at=dt.datetime.now(dt.UTC),
        uploaded_by=actor.id,
    )
    db.add(doc)
    try:
        db.flush()
    except IntegrityError:
        # A concurrent identical upload won the race. Before the append, so
        # nothing is burned; theirs is the row.
        db.rollback()
        twin = _existing(db, farmer.id, content_hash)
        if twin is None:  # pragma: no cover - the constraint says it exists
            raise
        return _replay(twin)
    db.refresh(doc)

    ip, user_agent = request_context(request)
    audit_log.append(
        db,
        actor_id=actor.id,
        actor_role=actor.role,
        subject_type="farmer",
        subject_id=str(farmer.id),
        action="document.uploaded",
        # No filename: it can carry a name or an ID number, and the chain is
        # append-only (11 D12).
        payload={
            "document_id": doc.id,
            "farmer_id": farmer.id,
            "doc_type": doc.doc_type,
            "content_hash": content_hash.hex(),
            "content_type": content_type,
            "size_bytes": doc.size_bytes,
            "scan_engine": doc.scan_engine,
        },
        ip=ip,
        user_agent=user_agent,
    )
    response = _out(doc)
    db.commit()
    return response


@router.get(
    "/farmers/{farmer_id}/documents",
    response_model=DocumentListResponse,
    responses=error_responses(401, 403, 404),
)
def list_documents(
    farmer_id: int = _FARMER_ID,
    db: Session = Depends(get_db),
    actor: User = Depends(_require_read),
) -> DocumentListResponse:
    farmer = _farmer_or_404(db, farmer_id)
    ownership.assert_acts_for_farmer(db, actor, farmer.id)
    docs = (
        db.execute(
            select(Document)
            .where(Document.subject_type == "farmer", Document.subject_id == farmer.id)
            .order_by(Document.id)
        )
        .scalars()
        .all()
    )
    return DocumentListResponse(farmer_id=farmer.id, documents=[_out(d) for d in docs])


@router.get(
    "/documents/{document_id}/download-url",
    response_model=DownloadUrlResponse,
    responses=error_responses(401, 403, 404, 409, 422, 503),
)
def document_download_url(
    document_id: int = _DOCUMENT_ID,
    db: Session = Depends(get_db),
    actor: User = Depends(_require_read),
    store: ObjectStore = Depends(get_object_store),
) -> DownloadUrlResponse:
    doc = db.get(Document, document_id)
    if doc is None:
        raise APIError(404, "document_not_found", "Document does not exist")
    ownership.assert_acts_for_farmer(db, actor, doc.subject_id)
    if doc.scan_status is not ScanStatus.clean:
        raise APIError(409, "document_not_clean", "This document has not passed the virus scan")
    consent.require_consent(
        db, subject_type="farmer", subject_id=doc.subject_id, purpose=ConsentPurpose.document_upload
    )

    ttl = dt.timedelta(seconds=settings.signed_url_ttl_seconds)
    try:
        url = store.presign_get(
            doc.object_key,
            ttl=ttl,
            content_type=doc.content_type,
            filename=doc.original_filename,
        )
    except StorageUnavailable as exc:
        raise APIError(503, "storage_unavailable", "Document storage is unavailable") from exc
    return DownloadUrlResponse(url=url, expires_at=dt.datetime.now(dt.UTC) + ttl)
