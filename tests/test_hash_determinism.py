"""Regression tests for the deterministic-hash invariant.

Ported from v1 ApiChain--Backend/backend/tests/test_hash_determinism.py,
retargeted from BlockchainService.compute_data_hash to the chain-neutral
app.services.canonical.compute_data_hash. The invariants are unchanged:

- Hash is invariant under dict key order (sort_keys=True doing its job).
- None-valued optional fields differ from missing fields.
- Datetimes serialize deterministically.
- Output is 32 bytes (keccak256).

New in v2: a golden parity vector captured from the live v1 implementation,
locking v2's output bit-for-bit to v1's proven discipline.
"""

from datetime import UTC, datetime

from app.services.canonical import canonical_dt, compute_data_hash


def test_key_order_does_not_affect_hash():
    h1 = compute_data_hash({"a": 1, "b": 2, "c": 3})
    h2 = compute_data_hash({"c": 3, "a": 1, "b": 2})
    assert h1 == h2, "Hash must be invariant under key reordering"


def test_nested_dict_key_order_does_not_affect_hash():
    h1 = compute_data_hash({"outer": {"x": 1, "y": 2}})
    h2 = compute_data_hash({"outer": {"y": 2, "x": 1}})
    assert h1 == h2


def test_missing_optional_differs_from_explicit_none():
    """If a payload omits a field, the hash MUST differ from one that sets it to None.
    Otherwise the schema's optional-field discipline is a no-op for traceability."""
    h_without = compute_data_hash({"a": 1})
    h_with_none = compute_data_hash({"a": 1, "b": None})
    assert h_without != h_with_none


def test_datetime_serializes_deterministically():
    """`default=str` is what allows datetime in payloads. Same datetime, same hash."""
    dt = datetime(2026, 5, 14, 12, 0, 0, tzinfo=UTC)
    h1 = compute_data_hash({"when": dt, "x": 1})
    h2 = compute_data_hash({"x": 1, "when": dt})
    assert h1 == h2


def test_hash_length_is_32_bytes():
    h = compute_data_hash({"anything": "here"})
    assert len(h) == 32, f"keccak256 must be 32 bytes, got {len(h)}"


def test_uses_sort_keys_in_serialization():
    """Lock in the implementation choice: the single serializer must produce
    sorted-key JSON. If someone changes this, two writers with identical
    semantic input produce different anchors. Since P1-C the serializer lives
    in `canonical_bytes` (which compute_data_hash and the audit chain share)."""
    import inspect

    from app.services.canonical import canonical_bytes

    src = inspect.getsource(canonical_bytes)
    assert "sort_keys=True" in src, "canonical_bytes must call json.dumps with sort_keys=True"


def test_keccak_parity_with_v1_golden_vector():
    """Locks v2's compute_data_hash to v1's exact output. Golden hex captured
    from ApiChain--Backend (Web3.keccak over sorted-key JSON) on 2026-07-17.
    If this breaks, the canonicalization or the hash primitive changed,
    which is a port failure, not a refactor."""
    payload = {"batch_id": "0xabc", "when": "2026-05-14T12:00:00", "yield_kg": 12.5, "notes": None}
    assert (
        compute_data_hash(payload).hex()
        == "837d2d4706afdcbcad237e38f36c74f3b87c0a30d0c65e8ecddbf6c5d25e455f"
    )


def test_canonical_dt_none_passthrough():
    assert canonical_dt(None) is None


def test_canonical_dt_tz_aware_and_naive_agree():
    """The v1 Sprint 6 watchlist invariant: a tz-aware datetime and its
    UTC-naive equivalent must canonicalize identically, because the DB
    round-trips TIMESTAMP WITHOUT TIME ZONE to naive."""
    aware = datetime(2026, 5, 14, 12, 0, 0, tzinfo=UTC)
    naive = datetime(2026, 5, 14, 12, 0, 0)
    assert canonical_dt(aware) == canonical_dt(naive) == "2026-05-14T12:00:00"
