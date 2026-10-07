"""The consumer's three-way match over a batch's seven stage records (P3-I, 10 §10).

For each stage block:

1. **recomputed** — `compute_data_hash(builder(row))` over the stage row as it
   stands now;
2. **recorded** — `payload_hash` on the audit row appended in the same
   transaction as that record, found by `(subject_type='batch', subject_id,
   action)` (D6: there is deliberately no `*_proof_hash` column and no FK);
3. **witnessed** — that audit row's inclusion in a public Merkle anchor, which
   Phase 2's `anchor_proof.entries_for_subject` already derives.

(1) vs (2) catches a DB edit after the fact; (2) vs (3) catches an edit to the
audit log itself. **Both comparisons are made by the server.** A consumer can
check (3) offline from `anchor-proof`, but cannot link a block's payload hash
to its anchored leaf, because `row_hash` also commits to the actor, IP and
user agent, none of which are public. For an unredacted block the consumer can
still hash the shown payload and get the recorded hash themselves.

**Block existence follows the audit log, not the stage table.** A stage with
an audit row but no stage row was deleted, which is a mismatch, not an
unreached stage; a stage row with no audit row was never anchored, also a
mismatch. A block is `None` only when neither exists, i.e. the batch has
honestly not got that far.

**Privacy (03 §8.1, 10 D11; extended by Ian on 2026-10-07).** `/verify` is the
anonymous jar-scan view, so every PII-tagged column in a stage payload has an
explicit public policy in `PUBLIC_REDACTIONS`, and a test pins that the policy
covers every tag, so a newly tagged column cannot leak by default. A redacted
block publishes **no hashes**: the hash commits to the exact values, and a
2 dp cell contains only ~10^8 six-dp coordinate candidates, so the hash next to
the reduced values would let anyone brute-force the hive's location back out.
The server still compares, and reports the result as `match`.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    ApiaryRecord,
    AuditLog,
    BatchMetadata,
    CodexConformance,
    DistributionRecord,
    HarvestRecord,
    HoneyBatch,
    LabResult,
    PackagingRecord,
    ProcessRecord,
)
from app.services import anchor_proof, codex_scoring, stage_payloads
from app.services.canonical import compute_data_hash

# Block name -> the stage table holding its pre-image. Keys equal
# `stage_payloads.BUILDERS`; a test pins that.
STAGE_MODELS: Mapping[str, type[Any]] = {
    "apiary": ApiaryRecord,
    "metadata": BatchMetadata,
    "harvest": HarvestRecord,
    "process": ProcessRecord,
    "lab": LabResult,
    "packaging": PackagingRecord,
    "distribution": DistributionRecord,
}

# Block name -> the audit action appended with it. These strings are a
# contract between the write handlers and this lookup.
AUDIT_ACTIONS: Mapping[str, str] = {
    "apiary": "batch.apiary_recorded",
    "metadata": "batch.metadata_recorded",
    "harvest": "batch.harvest_recorded",
    "process": "batch.process_recorded",
    "lab": "batch.lab_verified",
    "packaging": "batch.packaged",
    "distribution": "batch.distributed",
}

Redaction = Literal["reduce_2dp", "withhold"]

# What the anonymous view does with each PII-tagged payload field.
PUBLIC_REDACTIONS: Mapping[str, Mapping[str, Redaction]] = {
    # Exact hive location: theft risk and sensitive data under the KDPA.
    # 2 dp is ~1.1 km, locality precision (03 §8.1).
    "apiary": {"latitude": "reduce_2dp", "longitude": "reduce_2dp"},
    # Harvest GPS is the hive location again; same treatment.
    "harvest": {"gps_lat": "reduce_2dp", "gps_lon": "reduce_2dp"},
    # A named person. The laboratory and certificate number stay public, so a
    # consumer can still check the result with the lab.
    "lab": {"analyst_name": "withhold"},
}

_TWO_PLACES = Decimal("0.01")


@dataclass(frozen=True)
class StageCheck:
    """One block of the three-way match, already in its public form."""

    audit_id: int | None
    payload: dict[str, Any] | None
    recomputed_hash: str | None
    recorded_hash: str | None
    match: bool
    anchor_status: str | None
    payload_precision: Literal["exact", "reduced"]
    redacted_fields: list[str]


@dataclass(frozen=True)
class BatchVerification:
    blocks: dict[str, StageCheck | None]
    # The witnessed verdict, in `codex_scoring.as_payload` shape (see
    # `_witnessed_conformance`), or None if there is none to trust.
    conformance: dict[str, Any] | None
    metadata: BatchMetadata | None
    anchor_status: str


def _reduce(value: Any) -> str | None:
    if value is None:
        return None
    return str(Decimal(str(value)).quantize(_TWO_PLACES))


_APPLY: Mapping[Redaction, Callable[[Any], Any]] = {
    "reduce_2dp": _reduce,
    "withhold": lambda _value: None,
}


def _redact(name: str, payload: dict[str, Any] | None) -> tuple[dict[str, Any] | None, list[str]]:
    policy = PUBLIC_REDACTIONS.get(name, {})
    if not policy:
        return payload, []
    if payload is None:
        return None, list(policy)
    public = dict(payload)
    for field, rule in policy.items():
        public[field] = _APPLY[rule](public.get(field))
    return public, list(policy)


def _lab_payload(row: LabResult, conformance: CodexConformance | None) -> dict[str, Any] | None:
    """The lab pre-image embeds the verdict (P3-G), so recomputing it re-runs
    the scorer — under the rule set *recorded* on the row, never the current
    default, or every old verdict would mismatch the day the default moved."""
    if conformance is None or conformance.rule_set_version not in codex_scoring.RULES:
        # Cannot reconstruct what was hashed: report a mismatch, not a 500.
        return None
    report = codex_scoring.evaluate(
        codex_scoring.LabMeasurements.from_row(row),
        rule_set_version=conformance.rule_set_version,
    )
    return {**stage_payloads.lab_result(row), "conformance": codex_scoring.as_payload(report)}


def _witnessed_conformance(
    audit_rows: list[tuple[int, bytes, dict[str, Any]]],
) -> dict[str, Any] | None:
    """The verdict as anchored, not as re-derivable from the lab row now.

    Re-deriving it from the current row would let a DB edit that turns a
    failing panel into a passing one display `pass` beside a lab block that
    reports `match: false` — and a client rendering the verdict on its own
    would show the doctored result. So the verdict comes from the audit row's
    payload, and only after that JSONB copy is checked against the row's own
    `payload_hash`, which the chain and the anchor cover. The sub-dict carries
    no PII (rule set, verdict, parameters), so it is safe to publish whole.
    """
    if len(audit_rows) != 1:
        return None
    _, payload_hash, payload = audit_rows[0]
    if compute_data_hash(payload) != payload_hash:
        return None
    conformance = payload.get("conformance")
    return conformance if isinstance(conformance, dict) else None


def verify_batch(db: Session, batch: HoneyBatch) -> BatchVerification:
    subject_id = str(batch.id)
    entries = anchor_proof.entries_for_subject(db, subject_type="batch", subject_id=subject_id)
    status_of = {entry.audit_id: entry.status for entry in entries}

    recorded: dict[str, list[tuple[int, bytes, dict[str, Any]]]] = {}
    for audit_id, action, payload_hash, payload_json in db.execute(
        select(AuditLog.id, AuditLog.action, AuditLog.payload_hash, AuditLog.payload)
        .where(AuditLog.subject_type == "batch")
        .where(AuditLog.subject_id == subject_id)
        .order_by(AuditLog.id.asc())
    ).all():
        recorded.setdefault(action, []).append((audit_id, payload_hash, payload_json))

    conformance_row = db.execute(
        select(CodexConformance).where(CodexConformance.batch_id == batch.id)
    ).scalar_one_or_none()

    blocks: dict[str, StageCheck | None] = {}
    metadata_row: BatchMetadata | None = None
    for name, model in STAGE_MODELS.items():
        row = db.execute(select(model).where(model.batch_id == batch.id)).scalar_one_or_none()
        audit_rows = recorded.get(AUDIT_ACTIONS[name], [])
        if row is None and not audit_rows:
            blocks[name] = None  # honestly not reached yet
            continue

        payload: dict[str, Any] | None = None
        if row is not None:
            if name == "lab":
                payload = _lab_payload(row, conformance_row)
            else:
                payload = stage_payloads.BUILDERS[name](row)
            if name == "metadata":
                metadata_row = row

        recomputed = compute_data_hash(payload) if payload is not None else None
        audit_id, recorded_hash = audit_rows[0][:2] if audit_rows else (None, None)
        # More than one audit row for a once-only stage is itself an anomaly.
        match = (
            recomputed is not None
            and recorded_hash is not None
            and len(audit_rows) == 1
            and recomputed == recorded_hash
        )

        public_payload, redacted = _redact(name, payload)
        reduced = bool(redacted)
        blocks[name] = StageCheck(
            audit_id=audit_id,
            payload=public_payload,
            recomputed_hash=None if reduced or recomputed is None else recomputed.hex(),
            recorded_hash=None if reduced or recorded_hash is None else recorded_hash.hex(),
            match=match,
            anchor_status=status_of.get(audit_id) if audit_id is not None else None,
            payload_precision="reduced" if reduced else "exact",
            redacted_fields=redacted,
        )

    return BatchVerification(
        blocks=blocks,
        conformance=_witnessed_conformance(recorded.get(AUDIT_ACTIONS["lab"], [])),
        metadata=metadata_row,
        anchor_status=anchor_proof.rollup(entries),
    )
