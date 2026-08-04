"""Phase 2 acceptance: a proof verifies offline, with no server (P2-H, 09 §11).

The script runs as a real subprocess against a bundle saved from the endpoint —
not as an imported function — because "verifies offline" is a claim about
something a third party can run, not about a code path we can call.
"""

import json
import subprocess
import sys
from pathlib import Path

from sqlalchemy.orm import Session

from app.enums import Role
from app.models import Farmer, HoneyBatch
from app.services import anchoring, audit_log
from tests.fakes import FakeCalendar

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "verify_anchor.py"


def _run(bundle: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--bundle", str(bundle), *args],
        capture_output=True,
        text=True,
        cwd=SCRIPT.parent.parent,
    )


def _seed_and_anchor(engine, *, confirm_at_height: int | None = None, anchor: bool = True) -> int:
    with Session(engine) as s:
        farmer = Farmer(first_name="Ada", last_name="Bee", phone="+254700000901", enrolled_by=None)
        s.add(farmer)
        s.flush()
        batch = HoneyBatch(farmer_id=farmer.id, batch_code="B-VERIFY0001")
        s.add(batch)
        s.flush()
        for action in ("batch.created", "batch.harvest_recorded"):
            audit_log.append(
                s,
                actor_id=None,
                actor_role=Role.operator,
                subject_type="batch",
                subject_id=str(batch.id),
                action=action,
                payload={"a": action},
            )
        s.commit()
        batch_id = batch.id

    if anchor:
        calendar = FakeCalendar(confirm_at_height=confirm_at_height)
        with Session(engine) as s:
            anchoring.stamp_with_session(s, calendars=[calendar])
            s.commit()
        if confirm_at_height is not None:
            with Session(engine) as s:
                anchoring.upgrade_pending_with_session(s, calendars=[calendar])
                s.commit()
    return batch_id


def _bundle(client, tmp_path: Path, batch_id: int) -> Path:
    body = client.get(f"/v2/batches/{batch_id}/anchor-proof").json()
    path = tmp_path / "proof.json"
    path.write_text(json.dumps(body), encoding="utf-8")
    return path


def test_a_saved_proof_verifies_with_no_server_running(client, migrated_engine, tmp_path):
    batch_id = _seed_and_anchor(migrated_engine)
    bundle = _bundle(client, tmp_path, batch_id)

    result = _run(bundle)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Verified 2 record(s)" in result.stdout


def test_a_confirmed_proof_reports_its_bitcoin_block(client, migrated_engine, tmp_path):
    batch_id = _seed_and_anchor(migrated_engine, confirm_at_height=904_222)
    bundle = _bundle(client, tmp_path, batch_id)

    result = _run(bundle)

    assert result.returncode == 0, result.stdout
    assert "904222" in result.stdout


def test_a_tampered_row_hash_fails_verification(client, migrated_engine, tmp_path):
    batch_id = _seed_and_anchor(migrated_engine)
    bundle = _bundle(client, tmp_path, batch_id)

    body = json.loads(bundle.read_text(encoding="utf-8"))
    original = body["entries"][0]["row_hash"]
    flipped = f"{(int(original[:2], 16) ^ 0xFF):02x}" + original[2:]
    body["entries"][0]["row_hash"] = flipped
    bundle.write_text(json.dumps(body), encoding="utf-8")

    result = _run(bundle)

    assert result.returncode == 1
    assert "NOT in the tree" in result.stdout


def test_a_swapped_proof_step_fails_verification(client, migrated_engine, tmp_path):
    batch_id = _seed_and_anchor(migrated_engine)
    bundle = _bundle(client, tmp_path, batch_id)

    body = json.loads(bundle.read_text(encoding="utf-8"))
    step = body["entries"][0]["merkle_path"][0]
    step["position"] = "right" if step["position"] == "left" else "left"
    bundle.write_text(json.dumps(body), encoding="utf-8")

    assert _run(bundle).returncode == 1


def test_a_root_swapped_for_another_valid_looking_one_fails(client, migrated_engine, tmp_path):
    batch_id = _seed_and_anchor(migrated_engine)
    bundle = _bundle(client, tmp_path, batch_id)

    body = json.loads(bundle.read_text(encoding="utf-8"))
    body["entries"][0]["merkle_root"] = "11" * 32
    bundle.write_text(json.dumps(body), encoding="utf-8")

    result = _run(bundle)
    assert result.returncode == 1


def test_an_unanchored_bundle_reports_nothing_to_verify(client, migrated_engine, tmp_path):
    batch_id = _seed_and_anchor(migrated_engine, anchor=False)
    bundle = _bundle(client, tmp_path, batch_id)

    result = _run(bundle)

    assert result.returncode == 2
    assert "anchor-pending" in result.stdout


def test_a_single_audit_row_can_be_verified_on_its_own(client, migrated_engine, tmp_path):
    batch_id = _seed_and_anchor(migrated_engine)
    bundle = _bundle(client, tmp_path, batch_id)
    audit_id = json.loads(bundle.read_text(encoding="utf-8"))["entries"][1]["audit_id"]

    result = _run(bundle, "--audit-id", str(audit_id))

    assert result.returncode == 0, result.stdout
    assert "Verified 1 record(s)" in result.stdout


def test_the_verifier_needs_neither_the_web_framework_nor_the_database():
    # "Offline" has to mean a third party can run this. If it ever grows a
    # FastAPI or SQLAlchemy import, that claim quietly stops being true.
    source = SCRIPT.read_text(encoding="utf-8")
    for forbidden in ("fastapi", "sqlalchemy", "app.database", "app.models", "requests"):
        assert forbidden not in source, f"the offline verifier must not depend on {forbidden}"


# --- the --block-merkle-root comparison (regression) -----------------------
#
# An OTS attestation does not sit on our Merkle root: the calendar applies more
# operations, and the Bitcoin attestation lands on the derived commitment at
# the end of that path. That derived value is what equals the block header's
# merkle root (the library's own verify_against_blockheader compares exactly
# that). Comparing the supplied block root against OUR root instead rejected
# every genuine block root, and "passed" only when fed our own root — which
# proves nothing about Bitcoin at all.


def _attested_commitment(bundle: Path) -> tuple[str, int]:
    """The (commitment, height) the proof says Bitcoin attests to."""
    import base64

    from opentimestamps.core.notary import BitcoinBlockHeaderAttestation
    from opentimestamps.core.serialize import BytesDeserializationContext
    from opentimestamps.core.timestamp import DetachedTimestampFile

    entry = json.loads(bundle.read_text(encoding="utf-8"))["entries"][0]
    detached = DetachedTimestampFile.deserialize(
        BytesDeserializationContext(base64.b64decode(entry["ots_proof"]))
    )
    for msg, att in detached.timestamp.all_attestations():
        if isinstance(att, BitcoinBlockHeaderAttestation):
            return msg.hex(), att.height
    raise AssertionError("proof carries no Bitcoin attestation")


def test_the_real_attested_commitment_verifies(client, migrated_engine, tmp_path):
    batch_id = _seed_and_anchor(migrated_engine, confirm_at_height=904_800)
    bundle = _bundle(client, tmp_path, batch_id)
    commitment, height = _attested_commitment(bundle)

    result = _run(bundle, "--block-merkle-root", commitment)

    assert result.returncode == 0, result.stdout
    assert f"Bitcoin block {height}" in result.stdout


def test_this_batchs_merkle_root_is_not_accepted_as_a_block_root(client, migrated_engine, tmp_path):
    # The exact false positive the old comparison allowed.
    batch_id = _seed_and_anchor(migrated_engine, confirm_at_height=904_800)
    bundle = _bundle(client, tmp_path, batch_id)
    our_root = json.loads(bundle.read_text(encoding="utf-8"))["entries"][0]["merkle_root"]

    result = _run(bundle, "--block-merkle-root", our_root)

    assert result.returncode == 1, result.stdout
    assert "matches no attestation" in result.stdout


def test_a_wrong_block_root_is_rejected(client, migrated_engine, tmp_path):
    batch_id = _seed_and_anchor(migrated_engine, confirm_at_height=904_800)
    bundle = _bundle(client, tmp_path, batch_id)

    assert _run(bundle, "--block-merkle-root", "ab" * 32).returncode == 1


def test_the_commitment_to_look_up_is_printed(client, migrated_engine, tmp_path):
    # Without this the user has no way to know what to check in the block.
    batch_id = _seed_and_anchor(migrated_engine, confirm_at_height=904_800)
    bundle = _bundle(client, tmp_path, batch_id)
    commitment, _ = _attested_commitment(bundle)

    assert commitment in _run(bundle).stdout


def test_supplying_a_block_root_for_a_pending_proof_fails_cleanly(
    client, migrated_engine, tmp_path
):
    batch_id = _seed_and_anchor(migrated_engine)  # anchored, not yet confirmed
    bundle = _bundle(client, tmp_path, batch_id)

    result = _run(bundle, "--block-merkle-root", "ab" * 32)

    assert result.returncode == 1
    assert "names no Bitcoin block yet" in result.stdout
