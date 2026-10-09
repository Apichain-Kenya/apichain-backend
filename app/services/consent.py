"""Consent capture and gate (P1-E, 04 §5.6, 08 D10; completed in P3b-B, 11 §5).

The ledger is a history, not a set: every grant and every withdrawal is a row,
and **the newest row for `(subject, purpose)` decides**. Before 3b the gate
picked the newest *granted* row, which filtered a withdrawal out before
"newest" was decided, so a farmer who withdrew still passed. `current` is the
one place the rule lives; every reader goes through it.

Three writers/readers, deliberately not conflated:

- `capture_consent` — enrollment. The request must carry a grant (422
  otherwise) and the row is written in the caller's transaction, so a failed
  enrollment leaves no orphan consent.
- `record_consent` — the consent endpoint. Records a choice, grant or
  withdrawal.
- `require_consent` / `has_consent` — the gate, raising and non-raising. Called
  at document upload, signed-URL issuance and communications send (11 D2), and
  **never in a lifecycle transition handler**.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.errors import APIError
from app.models import ConsentPurpose, ConsentRecord, GrantedVia


def record_consent(
    db: Session,
    *,
    subject_type: str,
    subject_id: int,
    purpose: ConsentPurpose,
    granted: bool,
    granted_via: GrantedVia,
    text_version: str,
) -> ConsentRecord:
    row = ConsentRecord(
        subject_type=subject_type,
        subject_id=subject_id,
        consent_purpose=purpose,
        granted=granted,
        granted_via=granted_via,
        text_version=text_version,
    )
    db.add(row)
    db.flush()
    return row


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
    return record_consent(
        db,
        subject_type=subject_type,
        subject_id=subject_id,
        purpose=purpose,
        granted=True,
        granted_via=granted_via,
        text_version=text_version,
    )


def current(
    db: Session,
    *,
    subject_type: str,
    subject_id: int,
    purpose: ConsentPurpose,
    granted_via: GrantedVia | None = None,
) -> ConsentRecord | None:
    """The newest row for this subject and purpose, grant or withdrawal.

    `granted_via` narrows it to one capturer's latest choice (the consent
    endpoint asks for the farmer's own). Ordered by `id`, not `granted_at`:
    two rows written in one transaction share `now()`, and the sequence still
    orders them.
    """
    query = select(ConsentRecord).where(
        ConsentRecord.subject_type == subject_type,
        ConsentRecord.subject_id == subject_id,
        ConsentRecord.consent_purpose == purpose,
    )
    if granted_via is not None:
        query = query.where(ConsentRecord.granted_via == granted_via)
    return db.execute(query.order_by(ConsentRecord.id.desc()).limit(1)).scalar_one_or_none()


def has_consent(
    db: Session, *, subject_type: str, subject_id: int, purpose: ConsentPurpose
) -> bool:
    row = current(db, subject_type=subject_type, subject_id=subject_id, purpose=purpose)
    return row is not None and row.granted


def require_consent(
    db: Session, *, subject_type: str, subject_id: int, purpose: ConsentPurpose
) -> ConsentRecord:
    row = current(db, subject_type=subject_type, subject_id=subject_id, purpose=purpose)
    if row is None or not row.granted:
        raise APIError(
            422,
            "consent_required",
            f"No recorded consent for '{purpose}'",
            {"purpose": str(purpose)},
        )
    return row
