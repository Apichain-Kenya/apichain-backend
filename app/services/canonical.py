"""Canonical payload hashing, chain-neutral.

Ported from v1 ApiChain--Backend: BlockchainService.compute_data_hash
(app/services/blockchain.py) and _canonical_dt (app/routers/batch.py).
See v1 for the Sprint 6/9 determinism history; the invariants are locked
by tests/test_hash_determinism.py including a v1 golden parity vector.
"""

import json
from datetime import UTC, datetime

from eth_hash.auto import keccak


def compute_data_hash(data: dict) -> bytes:
    """keccak256 over deterministic JSON (sorted keys). Returns 32 bytes."""
    payload = json.dumps(data, sort_keys=True, default=str)
    return keccak(payload.encode("utf-8"))


def canonical_dt(dt: datetime | None) -> str | None:
    """Serialize a datetime to a tz-stable ISO-8601 string (UTC-naive).

    DB columns are TIMESTAMP WITHOUT TIME ZONE; a tz-aware datetime
    round-trips to naive. Normalize both sides before hashing so the
    stored hash and the recomputed hash agree.
    """
    if dt is None:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(UTC).replace(tzinfo=None)
    return dt.isoformat()
