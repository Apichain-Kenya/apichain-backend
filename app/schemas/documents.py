"""Document response schemas (P3b-E). Requests are multipart form fields,
declared on the route; there is no JSON request body to model."""

from datetime import datetime

from pydantic import BaseModel

from app.enums import ScanStatus


class DocumentResponse(BaseModel):
    id: int
    farmer_id: int
    doc_type: str
    # What the bytes sniffed as, never what the client declared.
    content_type: str
    size_bytes: int
    original_filename: str
    content_hash: str  # sha256, lowercase hex
    scan_status: ScanStatus
    uploaded_at: datetime


class DocumentListResponse(BaseModel):
    farmer_id: int
    documents: list[DocumentResponse]


class DownloadUrlResponse(BaseModel):
    """A short-lived signed URL (04 §5.6). Request a fresh one per view; it
    is never stored and never appears on any public response."""

    url: str
    expires_at: datetime
