"""`documents`: content-addressable media metadata (04 §5.2, §5.6; 11 §6).

The bytes live in object storage under `object_key = "sha256/<hex>"`; this row
is what the system knows about them. Ported from v1's `models/document.py`,
which stored a local path named `{farmer_id}_{original_filename}` and nothing
else: no hash, no type, no size, no scan, no consent (04 §1.4).

**Unique per (subject, content_hash), not per hash (11 D3).** `04` §5.2 makes
`content_hash` globally unique. That refuses the second farmer to upload a
common file (a blank form, a shared title deed), and the "already uploaded"
answer `03` §8.2 wants would tell farmer B that someone already holds the file
whose hash B computed. Storage still deduplicates fully: identical bytes share
one `object_key`, so the dedup is in the bucket, not the ledger.

**`content_type` is what the bytes sniffed as**, never the client's
declaration. `scan_status` and `scan_engine` exist so a later rescan has a
home; in 3b only `clean` rows are ever written, because the upload scans
synchronously and stores nothing that fails (11 D5). `scan_engine='fake'`
marks a document scanned by the dev fake, permanently (11 D4).

`uploaded_by` is not a foreign key, for the same reason `audit_log.actor_id`
is not: the record must outlive a user deletion.
"""

import datetime as dt

from sqlalchemy import Enum, Integer, LargeBinary, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.enums import ScanStatus
from app.models.types import UtcDateTime


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


class Document(Base):
    __tablename__ = "documents"
    __table_args__ = (
        UniqueConstraint(
            "subject_type", "subject_id", "content_hash", name="uq_documents_subject_content"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # 'farmer' only in 3b (11 D10); a column so batch documents need no migration.
    subject_type: Mapped[str] = mapped_column(String)
    subject_id: Mapped[int] = mapped_column(Integer)
    # App-layer vocabulary (a Literal at the API), like audit_log.action: it
    # grows without an ALTER TYPE.
    doc_type: Mapped[str] = mapped_column(String)
    content_hash: Mapped[bytes] = mapped_column(LargeBinary)  # sha256, 32 bytes
    content_type: Mapped[str] = mapped_column(String)  # sniffed
    size_bytes: Mapped[int] = mapped_column(Integer)
    original_filename: Mapped[str] = mapped_column(String, info={"pii": "identity"})
    object_key: Mapped[str] = mapped_column(String)
    scan_status: Mapped[ScanStatus] = mapped_column(Enum(ScanStatus, name="scan_status"))
    scan_engine: Mapped[str] = mapped_column(String)
    scanned_at: Mapped[dt.datetime] = mapped_column(UtcDateTime)
    uploaded_by: Mapped[int] = mapped_column(Integer)
    # Python-side default rather than server `now()`: a server default on a
    # naive column is rendered in the session's TimeZone, which is exactly the
    # drift UtcDateTime exists to stop.
    uploaded_at: Mapped[dt.datetime] = mapped_column(UtcDateTime, default=_utcnow)
