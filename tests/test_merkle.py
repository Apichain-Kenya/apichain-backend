"""The pure Merkle layer (P2-B, 09 §5).

No DB, no network, no clock — everything here is a pure function, which is why
it is specified byte-exactly and locked by a golden vector. A builder and an
independent third-party verifier disagree the moment any of leaf preimage,
domain separation, odd-node handling, or path encoding is left implicit.
"""

import hashlib

import pytest

from app.services.merkle import (
    ProofStep,
    build_root,
    inclusion_proof,
    leaf_hash,
    verify_inclusion,
)


def _row_hash(n: int) -> bytes:
    """Stand-in for an `audit_log.row_hash` (32 raw bytes)."""
    return hashlib.sha256(f"row-{n}".encode()).digest()


def _rows(count: int) -> list[bytes]:
    return [_row_hash(i) for i in range(count)]


def _leaves(count: int) -> list[bytes]:
    return [leaf_hash(r) for r in _rows(count)]


def _internal(left: bytes, right: bytes) -> bytes:
    return hashlib.sha256(b"\x01" + left + right).digest()


# --- construction, pinned byte-exactly (09 §5.1) ---------------------------


def test_leaf_is_sha256_of_the_row_hash_with_a_zero_prefix():
    row = _row_hash(0)
    assert leaf_hash(row) == hashlib.sha256(b"\x00" + row).digest()


def test_leaf_and_internal_domains_are_separated():
    # A leaf and an internal node must be unforgeably distinct, or an internal
    # node can be passed off as a record (the classic second-preimage attack).
    a, b = _leaves(2)
    assert leaf_hash(a + b) != _internal(a, b)
    assert leaf_hash(a) != hashlib.sha256(a).digest()


def test_single_leaf_tree_is_its_own_root():
    leaves = _leaves(1)
    assert build_root(leaves) == leaves[0]
    assert inclusion_proof(leaves, 0) == []
    assert verify_inclusion(leaves[0], [], leaves[0]) is True


def test_two_leaves_combine_left_to_right():
    leaves = _leaves(2)
    assert build_root(leaves) == _internal(leaves[0], leaves[1])


def test_odd_node_is_promoted_not_duplicated():
    # Duplicating the last node is Bitcoin's convention and the source of the
    # CVE-2012-2459 root-collision class. We promote instead (09 D7).
    leaves = _leaves(3)
    promoted = _internal(_internal(leaves[0], leaves[1]), leaves[2])
    duplicated = _internal(_internal(leaves[0], leaves[1]), _internal(leaves[2], leaves[2]))
    assert build_root(leaves) == promoted
    assert build_root(leaves) != duplicated


def test_promotion_does_not_contribute_a_path_step():
    leaves = _leaves(3)
    # index 2 is promoted at the first level, so its path is one step, not two.
    assert len(inclusion_proof(leaves, 2)) == 1


def test_leaf_order_is_preserved_never_sorted():
    leaves = _leaves(4)
    assert build_root(leaves) != build_root(list(reversed(leaves)))


# --- proofs ----------------------------------------------------------------


@pytest.mark.parametrize("count", [1, 2, 3, 4, 5, 8, 9, 17])
def test_every_index_produces_a_verifying_proof(count):
    leaves = _leaves(count)
    root = build_root(leaves)
    for i, leaf in enumerate(leaves):
        assert verify_inclusion(leaf, inclusion_proof(leaves, i), root) is True


def test_a_tampered_leaf_does_not_verify():
    leaves = _leaves(5)
    root = build_root(leaves)
    path = inclusion_proof(leaves, 2)
    assert verify_inclusion(leaf_hash(_row_hash(99)), path, root) is False


def test_a_tampered_sibling_does_not_verify():
    leaves = _leaves(5)
    root = build_root(leaves)
    path = inclusion_proof(leaves, 2)
    forged = [ProofStep(bytes(32), path[0].position), *path[1:]]
    assert verify_inclusion(leaves[2], forged, root) is False


def test_a_flipped_position_does_not_verify():
    leaves = _leaves(4)
    root = build_root(leaves)
    path = inclusion_proof(leaves, 1)
    flipped = [ProofStep(s.sibling, "right" if s.position == "left" else "left") for s in path]
    assert verify_inclusion(leaves[1], flipped, root) is False


def test_a_truncated_path_does_not_verify():
    leaves = _leaves(8)
    root = build_root(leaves)
    path = inclusion_proof(leaves, 3)
    assert verify_inclusion(leaves[3], path[:-1], root) is False


def test_an_unknown_position_is_rejected_rather_than_guessed():
    leaves = _leaves(2)
    with pytest.raises(ValueError):
        verify_inclusion(leaves[0], [ProofStep(leaves[1], "sideways")], bytes(32))


# --- guards ----------------------------------------------------------------


def test_empty_input_raises():
    # The worker skips a run with no new rows, so this is a programming-error
    # guard, not a control path (09 §5.1).
    with pytest.raises(ValueError):
        build_root([])
    with pytest.raises(ValueError):
        inclusion_proof([], 0)


@pytest.mark.parametrize("index", [-1, 4])
def test_out_of_range_index_raises(index):
    with pytest.raises(IndexError):
        inclusion_proof(_leaves(4), index)


# --- golden vector ---------------------------------------------------------

# NEVER CHANGE THESE VALUES. They pin the anchor format exactly as
# tests/test_hash_determinism.py pins the canonical-payload format. Every proof
# ever issued to a consumer verifies against this construction; if this test
# fails, the format moved and every previously issued proof just became
# unverifiable. Fix the code, not the vector.
GOLDEN_ROW_HASHES = [
    "f1b3a2ba5e2f7d0e1b2a1a70cbf9f9a1c1e4b3b3c5f6a7e8d9c0b1a2f3e4d5c6",
    "0e1d2c3b4a5968778695a4b3c2d1e0f00f1e2d3c4b5a69788796a5b4c3d2e1f0",
    "aabbccddeeff00112233445566778899aabbccddeeff00112233445566778899",
    "1111111111111111111111111111111111111111111111111111111111111111",
    "deadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
]
GOLDEN_ROOT = "e656416c575fa1ee5b32893fa5c69cc1881f1a004c11d4c91738ac9b0a7b0ec1"
GOLDEN_PROOF_INDEX = 4
GOLDEN_PROOF = [
    ("a8fee386b00c02fbda65476577dca1f66f31a2ee201fb3a2290d46b2f885a746", "left"),
]


def test_golden_vector_root():
    leaves = [leaf_hash(bytes.fromhex(h)) for h in GOLDEN_ROW_HASHES]
    assert build_root(leaves).hex() == GOLDEN_ROOT


def test_golden_vector_proof():
    leaves = [leaf_hash(bytes.fromhex(h)) for h in GOLDEN_ROW_HASHES]
    path = inclusion_proof(leaves, GOLDEN_PROOF_INDEX)
    assert [(s.sibling.hex(), s.position) for s in path] == GOLDEN_PROOF
    assert verify_inclusion(leaves[GOLDEN_PROOF_INDEX], path, bytes.fromhex(GOLDEN_ROOT))
