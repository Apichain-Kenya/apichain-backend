"""The two anonymous consumer views: anchor-proof (P2-G) and verify (P3-I).

Unauthenticated by design: the consumer scanning a jar is not a user
(04 §5.3). Both are keyed by `honey_batches.public_id`, the 128-bit random jar
QR identifier, never by the sequential `id`. A public route on `id` lets anyone
walk 1..N and scrape every batch, which is the enumeration a security review
caught in P3-I. v1's QR used an unguessable 64-hex id for the same reason.
"""

import base64
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Path
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.errors import APIError, error_responses
from app.models import HoneyBatch
from app.schemas.anchor import AnchorProofEntry, AnchorProofResponse, ProofStepOut
from app.schemas.batches import MetadataPublic
from app.schemas.verify import (
    BatchVerifyResponse,
    ConformanceOut,
    StageVerificationOut,
    StageVerifications,
)
from app.services import anchor_proof, verification

router = APIRouter(prefix="/public/batches", tags=["public"])

# `models.batch.new_public_id`: 32 lowercase hex characters. Anything else is
# invalid input (422), not a lookup that happens to miss.
_PUBLIC_ID = Path(pattern=r"^[0-9a-f]{32}$", min_length=32, max_length=32)


def _batch_or_404(db: Session, public_id: str) -> HoneyBatch:
    batch = db.execute(
        select(HoneyBatch).where(HoneyBatch.public_id == public_id)
    ).scalar_one_or_none()
    if batch is None:
        # Echo nothing back: the id is the caller's own input.
        raise APIError(404, "batch_not_found", "Batch does not exist")
    return batch


def _as_utc(value: datetime | None) -> datetime | None:
    """Columns are naive UTC (matching audit_log); the wire carries the zone."""
    return value.replace(tzinfo=UTC) if value is not None else None


@router.get(
    "/{public_id}/anchor-proof",
    response_model=AnchorProofResponse,
    responses=error_responses(404),
)
def batch_anchor_proof(
    public_id: str = _PUBLIC_ID,
    db: Session = Depends(get_db),
) -> AnchorProofResponse:
    """Everything needed to verify this batch's records offline.

    One entry per audit row, each carrying its own status, so a record that
    exists in our log but has no public anchor yet is reported as `pending`
    rather than as a missing field (03 §6).
    """
    batch = _batch_or_404(db, public_id)

    entries = anchor_proof.entries_for_subject(db, subject_type="batch", subject_id=str(batch.id))
    return AnchorProofResponse(
        batch_id=batch.id,
        public_id=batch.public_id,
        status=anchor_proof.rollup(entries),  # type: ignore[arg-type]
        entries=[
            AnchorProofEntry(
                audit_id=entry.audit_id,
                action=entry.action,
                row_hash=entry.row_hash.hex(),
                status=entry.status,  # type: ignore[arg-type]
                merkle_root=entry.anchor.merkle_root.hex() if entry.anchor else None,
                merkle_path=(
                    [ProofStepOut(sibling=s.sibling.hex(), position=s.position) for s in entry.path]
                    if entry.path is not None
                    else None
                ),
                anchor_target=entry.anchor.anchor_target if entry.anchor else None,
                ots_proof=(
                    base64.b64encode(entry.anchor.anchor_proof).decode() if entry.anchor else None
                ),
                anchored_at=_as_utc(entry.anchor.anchored_at) if entry.anchor else None,
                verified_at=_as_utc(entry.anchor.verified_at) if entry.anchor else None,
            )
            for entry in entries
        ],
    )


@router.get(
    "/{public_id}/verify",
    response_model=BatchVerifyResponse,
    responses=error_responses(404),
)
def verify_batch(
    public_id: str = _PUBLIC_ID,
    db: Session = Depends(get_db),
) -> BatchVerifyResponse:
    """The three-way match over every recorded stage of this batch.

    Being anonymous, it applies `verification.FIELD_POLICY` (hive coordinates
    at 2 dp, altitude at 100 m, free text and the analyst's name withheld) and
    publishes no hashes for a redacted block. Staff see exact values in an
    authenticated view, never here. Reports facts only: no score, no band —
    those mappings are the client's (03 §1, §6).
    """
    batch = _batch_or_404(db, public_id)

    result = verification.verify_batch(db, batch)
    conformance = None
    if result.conformance is not None:
        conformance = ConformanceOut.model_validate(
            {**result.conformance, "verdict": str(result.conformance["verdict"]).lower()}
        )
    return BatchVerifyResponse(
        batch_id=batch.id,
        public_id=batch.public_id,
        state=str(batch.state),
        metadata=(
            MetadataPublic.model_validate(result.metadata) if result.metadata is not None else None
        ),
        verification=StageVerifications(
            **{
                name: (StageVerificationOut(**vars(check)) if check is not None else None)
                for name, check in result.blocks.items()
            }
        ),
        conformance=conformance,
        anchor_status=result.anchor_status,  # type: ignore[arg-type]
    )
