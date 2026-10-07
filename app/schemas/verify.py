"""`GET /v2/batches/{id}/verify` response schemas (P3-I, 10 §10).

Vocabulary, fixed here so neither side invents a third one: conformance is
`pass` / `fail` / `incomplete` (per parameter, `pass` / `fail` /
`not_measured`), and anchoring reuses Phase 2's `pending` / `anchored` /
`confirmed`. The consumer-safe `authenticity_band` is a client-side mapping
(Phase 4, `03` §1, §6), and there is no blended score anywhere (`02` §4).
"""

from typing import Any, Literal

from pydantic import BaseModel

from app.schemas.anchor import BatchAnchorStatus, EntryStatus
from app.schemas.batches import MetadataPublic


class StageVerificationOut(BaseModel):
    """One stage of the three-way match.

    `payload` is the canonical pre-image as the stage row stands now. When
    `payload_precision` is `exact`, hashing it reproduces `recomputed_hash`.
    When it is `reduced`, the fields in `redacted_fields` were coarsened or
    withheld for privacy and **both hashes are omitted**, because they commit
    to the exact values; `match` is then the server's own comparison.
    """

    audit_id: int | None
    payload: dict[str, Any] | None
    recomputed_hash: str | None  # hex
    recorded_hash: str | None  # hex
    match: bool
    anchor_status: EntryStatus | None
    payload_precision: Literal["exact", "reduced"]
    redacted_fields: list[str]


class StageVerifications(BaseModel):
    """`null` means the batch has not reached that stage yet."""

    apiary: StageVerificationOut | None
    metadata: StageVerificationOut | None
    harvest: StageVerificationOut | None
    process: StageVerificationOut | None
    lab: StageVerificationOut | None
    packaging: StageVerificationOut | None
    distribution: StageVerificationOut | None


class ConformanceParameterOut(BaseModel):
    parameter: str
    measured: str | None
    unit: str
    comparator: Literal["<=", ">="]
    limit: str
    basis: str
    status: Literal["pass", "fail", "not_measured"]


class ConformanceOut(BaseModel):
    """The Codex/KEBS verdict re-derived from the lab row now, under the rule
    set recorded with it. Every limit and its basis travel with the result, so
    a reader never needs to know which rules were in force."""

    rule_set_version: str
    verdict: Literal["pass", "fail", "incomplete"]
    parameters: list[ConformanceParameterOut]


class BatchVerifyResponse(BaseModel):
    batch_id: int
    batch_code: str
    state: str
    # The farmer's declaration in display form (no free text).
    metadata: MetadataPublic | None
    verification: StageVerifications
    conformance: ConformanceOut | None
    # Rollup over every audit row of the batch, `batch.created` included.
    anchor_status: BatchAnchorStatus
