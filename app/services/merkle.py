"""The Merkle layer over `audit_log` rows (P2-B, 09 §5).

Pure: no DB, no network, no clock. The anchoring worker (P2-E) builds a root
over a contiguous run of audit rows; the anchor-proof endpoint (P2-G) derives
inclusion paths on demand; `scripts/verify_anchor.py` and any third party
re-verify them offline.

**This layer is sha256, while the audit chain is keccak256, and that is
deliberate (09 D2).** OpenTimestamps declares the anchored root with `OpSHA256`
— it asserts "this root is a sha256 digest". A sha256 root makes that assertion
true, so a stock `ots verify` and any third-party tool can reproduce the proof;
a keccak root would make it false and confine verification to our own code,
which would defeat the point of anchoring. `leaf = sha256(0x00 ‖ row_hash)`
means the sha256 tree still commits to the keccak chain. Do not "unify" the two
hash functions for consistency — it would invalidate every proof ever issued.

Construction, pinned byte-exactly and locked by the golden vector in
`tests/test_merkle.py`:

- leaf:     sha256(0x00 ‖ row_hash)
- internal: sha256(0x01 ‖ left ‖ right)
- odd node: promoted unchanged to the next level, never duplicated. Bitcoin's
  duplicate-last convention is where CVE-2012-2459 (distinct trees sharing one
  root) comes from; there is no reason to inherit it. A promoted node
  contributes no step to any proof path.
- leaves are in ascending `audit_log.id` order, never sorted, never deduped.
"""

import hashlib
from dataclasses import dataclass
from typing import Literal

_LEAF_PREFIX = b"\x00"
_INTERNAL_PREFIX = b"\x01"

Position = Literal["left", "right"]


@dataclass(frozen=True)
class ProofStep:
    """One sibling on the path from a leaf to the root.

    `position` is the side **the sibling** sits on, so a verifier never has to
    know the leaf's index or the tree's width.
    """

    sibling: bytes
    position: Position


def leaf_hash(row_hash: bytes) -> bytes:
    """The leaf committing to one `audit_log.row_hash` (raw 32 bytes)."""
    return hashlib.sha256(_LEAF_PREFIX + row_hash).digest()


def _internal(left: bytes, right: bytes) -> bytes:
    return hashlib.sha256(_INTERNAL_PREFIX + left + right).digest()


def _next_level(level: list[bytes]) -> list[bytes]:
    parents = [_internal(level[i], level[i + 1]) for i in range(0, len(level) - 1, 2)]
    if len(level) % 2:
        parents.append(level[-1])  # promote, do not duplicate
    return parents


def build_root(leaves: list[bytes]) -> bytes:
    """Root over `leaves` (already `leaf_hash`ed, in audit-id order)."""
    if not leaves:
        raise ValueError("cannot build a Merkle root over zero leaves")
    level = list(leaves)
    while len(level) > 1:
        level = _next_level(level)
    return level[0]


def inclusion_proof(leaves: list[bytes], index: int) -> list[ProofStep]:
    """The sibling path proving `leaves[index]` is in the tree."""
    if not leaves:
        raise ValueError("cannot prove inclusion in an empty tree")
    if not 0 <= index < len(leaves):
        raise IndexError(f"leaf index {index} out of range for {len(leaves)} leaves")

    path: list[ProofStep] = []
    level = list(leaves)
    idx = index
    while len(level) > 1:
        if idx == len(level) - 1 and len(level) % 2:
            # Promoted: it has no sibling at this level, so no step is emitted
            # and it lands after the pairs that were combined.
            idx = len(level) // 2
        else:
            sibling = idx ^ 1
            path.append(ProofStep(level[sibling], "left" if sibling < idx else "right"))
            idx //= 2
        level = _next_level(level)
    return path


def verify_inclusion(leaf: bytes, path: list[ProofStep], root: bytes) -> bool:
    """Fold `leaf` up through `path` and compare with `root`.

    The whole offline verification story reduces to this function plus
    `leaf_hash` — no DB, no network, no third party.
    """
    current = leaf
    for step in path:
        if step.position == "left":
            current = _internal(step.sibling, current)
        elif step.position == "right":
            current = _internal(current, step.sibling)
        else:
            raise ValueError(f"unknown proof-step position: {step.position!r}")
    return current == root
