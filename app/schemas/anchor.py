"""Anchor-proof and anchor-health response schemas (P2-G, 04 §5.4).

Wire encoding, fixed once here so the endpoint, the offline verifier script,
and the tests cannot drift apart (09 D6): every `bytea` is lowercase hex,
except the `.ots` proof, which is base64.
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel

from app.enums import AnchorTarget

EntryStatus = Literal["pending", "anchored", "confirmed"]
BatchAnchorStatus = Literal["pending", "partial", "anchored", "confirmed"]


class ProofStepOut(BaseModel):
    """One sibling on the path to the root; `position` is the sibling's side."""

    sibling: str  # hex
    position: Literal["left", "right"]


class AnchorProofEntry(BaseModel):
    """One audit row of this batch, and the proof for it if one exists yet."""

    audit_id: int
    action: str
    row_hash: str  # hex
    status: EntryStatus
    merkle_root: str | None = None  # hex
    merkle_path: list[ProofStepOut] | None = None
    anchor_target: AnchorTarget | None = None
    ots_proof: str | None = None  # base64
    anchored_at: datetime | None = None
    verified_at: datetime | None = None


class AnchorProofResponse(BaseModel):
    batch_id: int
    batch_code: str
    # The weakest entry's state, so a client can render one badge without
    # walking the list: `partial` means some records are anchored and some are
    # still only in our log.
    status: BatchAnchorStatus
    entries: list[AnchorProofEntry]


class AnchorHealthResponse(BaseModel):
    """Anchor lag, for the uptime check and the Phase 5 alarms (05 §3.6)."""

    anchor_ok: bool
    last_run_at: datetime | None = None
    last_anchor_id: int | None = None
    last_anchored_audit_id: int | None = None
    pending_rows: int
    oldest_unanchored_at: datetime | None = None
    pending_anchors: int
    last_error: str | None = None
