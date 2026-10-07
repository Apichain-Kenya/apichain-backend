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
anonymous jar-scan view, so every field of every stage payload is classified
in `FIELD_POLICY` as public, reduced or withheld, failing closed on anything
unlisted. Hive coordinates go to 2 dp, altitude to 100 m, and free text, the
analyst's name and the transport reference are withheld. A block whose public
form differs from its exact pre-image publishes **no hashes**: the hash
commits to the exact values, and a 2 dp cell contains only ~10^8 six-dp
coordinate candidates, so the hash next to the reduced values would let anyone
brute-force the hive's location back out.
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

Rule = Literal["public", "reduce_2dp", "round_100", "withhold"]

# Every field of every public payload, classified. Fail-closed: a key a
# builder emits that is not listed here is withheld, and a test asserts each
# block's payload keys equal its policy keys, so a new column is a visible
# decision rather than a silent publication. Classified per field rather than
# by PII tag because free text and references carry personal data whatever the
# column is tagged (Ian, 2026-10-07).
FIELD_POLICY: Mapping[str, Mapping[str, Rule]] = {
    "apiary": {
        "batch_id": "public",
        # Internal FK; links batches from one apiary to each other.
        "apiary_id": "withhold",
        # Exact hive location: theft risk and sensitive data under the KDPA.
        # 2 dp is ~1.1 km, locality precision (03 §8.1, D11).
        "latitude": "reduce_2dp",
        "longitude": "reduce_2dp",
        # Centimetre altitude inside a 1.1 km cell narrows hilly terrain to a
        # contour line; 100 m keeps the fact without the fix.
        "altitude": "round_100",
        "vegetation_type": "public",
        "hive_count": "public",
    },
    "metadata": {
        "batch_id": "public",
        "honey_type": "public",
        "expected_yield_kg": "public",
        "harvest_window_start": "public",
        "harvest_window_end": "public",
        "apiary_management_method": "public",
        "recorded_at": "public",
    },
    "harvest": {
        "batch_id": "public",
        "harvest_date": "public",
        "quantity_kg": "public",
        "hive_ids": "public",
        # The hive location again; same treatment as the apiary.
        "gps_lat": "reduce_2dp",
        "gps_lon": "reduce_2dp",
        "notes": "withhold",
    },
    "process": {
        "batch_id": "public",
        "extraction_method": "public",
        "moisture_content": "public",
        "handling_notes": "withhold",
    },
    "lab": {
        "batch_id": "public",
        "moisture_pct": "public",
        "fructose_glucose_g_100g": "public",
        "sucrose_g_100g": "public",
        "hmf_mg_kg": "public",
        "diastase_schade": "public",
        "free_acidity_meq_kg": "public",
        "pollen_density": "public",
        # The laboratory and certificate number stay public so a consumer can
        # check the result with the lab; the named analyst does not.
        "laboratory_name": "public",
        "analyst_name": "withhold",
        "certificate_number": "public",
        "notes": "withhold",
        "tested_at": "public",
        # Merged into the lab pre-image by P3-G, not emitted by the builder.
        "conformance": "public",
    },
    "packaging": {
        "batch_id": "public",
        "unit_count": "public",
        "jar_ids": "public",
        "notes": "withhold",
    },
    "distribution": {
        "batch_id": "public",
        "retailer_name": "public",
        # Typically a vehicle plate or a driver.
        "transport_reference": "withhold",
        "handover_notes": "withhold",
    },
}

_TWO_PLACES = Decimal("0.01")
_HUNDRED = Decimal(100)


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


def _reduce_2dp(value: Any) -> str | None:
    if value is None:
        return None
    return str(Decimal(str(value)).quantize(_TWO_PLACES))


def _round_100(value: Any) -> str | None:
    if value is None:
        return None
    # Not quantize(Decimal("1E2")), which renders as "1.8E+3".
    return f"{(Decimal(str(value)) / _HUNDRED).quantize(Decimal(1)) * _HUNDRED:f}"


_APPLY: Mapping[Rule, Callable[[Any], Any]] = {
    "public": lambda value: value,
    "reduce_2dp": _reduce_2dp,
    "round_100": _round_100,
    "withhold": lambda _value: None,
}


def _public_form(name: str, payload: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """The payload as the anonymous view may show it, and which fields that
    changed. Only fields whose value actually changed are listed, so a block
    whose withheld fields happen to be null stays exact and reproducible."""
    policy = FIELD_POLICY[name]
    public = {key: _APPLY[policy.get(key, "withhold")](value) for key, value in payload.items()}
    changed = [key for key in payload if public[key] != payload[key]]
    return public, changed


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

        if payload is not None:
            public_payload, redacted = _public_form(name, payload)
        else:
            # Deleted row: nothing to compare against, so fall back to the
            # static policy. Comparing None to None would call the block exact
            # and publish a recorded hash over values that are never shown.
            public_payload = None
            redacted = [k for k, rule in FIELD_POLICY[name].items() if rule != "public"]
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
