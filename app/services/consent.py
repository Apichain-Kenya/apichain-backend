"""Consent capture and gate (P1-E, 04 §5.6, 08 D10).

Two distinct modes, deliberately not conflated:

- `capture_consent` — enrollment (Phase 1). The request carries the grant; this
  validates it is granted and writes the `consent_records` row in the caller's
  transaction, so a failed enrollment leaves no orphan consent. There is no
  prior row to look up.
- `require_consent` — upload (Phase 3). Looks up a pre-existing granted row and
  raises if absent (the document-upload gate). Defined now for symmetry; used
  when the media pipeline lands.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.errors import APIError
from app.models import ConsentPurpose, ConsentRecord, GrantedVia


def capture_consent(
    db: Session,
    *,
    subject_type: str,
    subject_id: int,
    purpose: ConsentPurpose,
    granted: bool,
    granted_via: GrantedVia,
    text_version: str,
) -> ConsentRecord:
    if not granted:
        raise APIError(
            422,
            "consent_required",
            f"Consent for '{purpose}' is required",
            {"purpose": str(purpose)},
        )
    row = ConsentRecord(
        subject_type=subject_type,
        subject_id=subject_id,
        consent_purpose=purpose,
        granted=True,
        granted_via=granted_via,
        text_version=text_version,
    )
    db.add(row)
    db.flush()
    return row


def require_consent(
    db: Session, *, subject_type: str, subject_id: int, purpose: ConsentPurpose
) -> ConsentRecord:
    row = db.execute(
        select(ConsentRecord)
        .where(
            ConsentRecord.subject_type == subject_type,
            ConsentRecord.subject_id == subject_id,
            ConsentRecord.consent_purpose == purpose,
            ConsentRecord.granted.is_(True),
        )
        .order_by(ConsentRecord.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    if row is None:
        raise APIError(
            422,
            "consent_required",
            f"No recorded consent for '{purpose}'",
            {"purpose": str(purpose)},
        )
    return row
