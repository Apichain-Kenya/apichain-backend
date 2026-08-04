#!/usr/bin/env python
"""Verify an anchor-proof bundle offline (P2-H, 09 §11).

No database, no server, no network. Point it at a bundle saved from
`GET /v2/batches/{id}/anchor-proof` and it re-derives everything itself:

    python scripts/verify_anchor.py --bundle proof.json
    python scripts/verify_anchor.py --bundle proof.json --audit-id 41
    python scripts/verify_anchor.py --bundle proof.json --block-merkle-root <hex>

**What "offline" means here, precisely** — because this is the claim the whole
anchoring design rests on, and overstating it would be worse than not making
it:

  1. Recomputing the leaf from `row_hash`               — no network, no trust.
  2. Folding the Merkle path to a root                  — no network, no trust.
  3. Checking that root is what the .ots proof commits to — no network, no trust.

Those three are the substance: they prove this record is in the tree whose root
was published, and that the published root is the one in front of you. Step 4
reports what the proof itself asserts (a pending calendar, or a Bitcoin block
height). Only step 5 — confirming that block really contains the root — needs a
Bitcoin block header, which you supply with `--block-merkle-root` from any
source you trust: your own node, a block explorer, a friend. There is no
Bitcoin node here and none in CI.

Exit codes: 0 every checked entry verified; 1 something did not verify;
2 nothing to check yet (every record still anchor-pending).
"""

import argparse
import base64
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from opentimestamps.core.notary import (  # noqa: E402
    BitcoinBlockHeaderAttestation,
    PendingAttestation,
)
from opentimestamps.core.serialize import BytesDeserializationContext  # noqa: E402
from opentimestamps.core.timestamp import DetachedTimestampFile  # noqa: E402

# The one shared import: the Merkle construction itself, so this script and the
# server can never disagree about how a tree is built.
from app.services.merkle import ProofStep, leaf_hash, verify_inclusion  # noqa: E402

OK = "  ok   "
BAD = " FAIL  "
INFO = " info  "


def _check_entry(entry: dict, block_merkle_root: str | None) -> bool | None:
    """True/False if the entry was checked, None if it is not anchored yet."""
    audit_id = entry.get("audit_id")
    if entry.get("status") == "pending" or not entry.get("merkle_root"):
        print(f"{INFO} audit {audit_id}: recorded in the log, public anchor still pending")
        return None

    label = f"audit {audit_id} ({entry.get('action')})"
    root = bytes.fromhex(entry["merkle_root"])

    # 1 + 2: leaf, then fold the path to a root.
    leaf = leaf_hash(bytes.fromhex(entry["row_hash"]))
    path = [ProofStep(bytes.fromhex(s["sibling"]), s["position"]) for s in entry["merkle_path"]]
    if not verify_inclusion(leaf, path, root):
        print(f"{BAD} {label}: record is NOT in the tree with root {root.hex()}")
        return False
    print(f"{OK} {label}: included in Merkle root {root.hex()}")

    # 3: the published proof must commit to that same root.
    try:
        detached = DetachedTimestampFile.deserialize(
            BytesDeserializationContext(base64.b64decode(entry["ots_proof"]))
        )
    except Exception as exc:
        print(f"{BAD} {label}: .ots proof could not be parsed ({exc})")
        return False
    if detached.timestamp.msg != root:
        print(f"{BAD} {label}: .ots proof commits to {detached.timestamp.msg.hex()}, not this root")
        return False
    print(f"{OK} {label}: .ots proof commits to that exact root")

    # 4: report what the proof asserts about the public chain.
    heights = [
        a.height
        for _, a in detached.timestamp.all_attestations()
        if isinstance(a, BitcoinBlockHeaderAttestation)
    ]
    pending = [
        a.uri for _, a in detached.timestamp.all_attestations() if isinstance(a, PendingAttestation)
    ]
    if heights:
        # A confirmed proof usually still carries the calendar's original
        # pending attestation; once Bitcoin has attested, that is history, not
        # a wait, so it is not reported as one.
        print(f"{OK} {label}: proof asserts Bitcoin block(s) {heights}")
    elif pending:
        print(f"{INFO} {label}: awaiting Bitcoin via {', '.join(pending)}")
    else:
        print(f"{BAD} {label}: proof carries no attestation at all")
        return False

    # 5: the only step that needs anything from outside this file.
    if block_merkle_root is not None:
        if block_merkle_root.lower() == root.hex():
            print(f"{OK} {label}: matches the supplied Bitcoin block Merkle root")
        else:
            print(f"{BAD} {label}: does NOT match the supplied Bitcoin block Merkle root")
            return False
    elif heights:
        print(f"{INFO} {label}: pass --block-merkle-root to check block {heights[0]} yourself")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--bundle", required=True, type=Path, help="saved anchor-proof JSON")
    parser.add_argument("--audit-id", type=int, help="verify only this audit row")
    parser.add_argument(
        "--block-merkle-root",
        help="Merkle root of the Bitcoin block the proof names, from a source you trust",
    )
    args = parser.parse_args(argv)

    bundle = json.loads(args.bundle.read_text(encoding="utf-8"))
    entries = bundle.get("entries", [])
    if args.audit_id is not None:
        entries = [e for e in entries if e.get("audit_id") == args.audit_id]
        if not entries:
            print(f"{BAD} no entry for audit id {args.audit_id} in this bundle")
            return 1

    code, batch_id, status = (
        bundle.get("batch_code"),
        bundle.get("batch_id"),
        bundle.get("status"),
    )
    print(f"batch {code} (id {batch_id}), status: {status}")
    results = [_check_entry(entry, args.block_merkle_root) for entry in entries]
    checked = [r for r in results if r is not None]

    if not checked:
        print("\nNothing to verify yet: every record is still anchor-pending.")
        return 2
    if all(checked):
        print(
            f"\nVerified {len(checked)} record(s) against the published anchor. No server needed."
        )
        return 0
    print(f"\n{checked.count(False)} of {len(checked)} record(s) FAILED verification.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
