"""GET /v2/batches/{id}/anchor-proof and the anchor-health surface (P2-G, 09 §10).

The endpoint is public: the consumer scanning a jar is not a user (04 §5.3).
Its job is to be honest about three states — a record can be in our log but not
yet anchored, anchored but not yet Bitcoin-confirmed, or fully confirmed — and
`pending` is a value rather than a missing field so the client renders the
third state instead of inferring it (03 §6).
"""

import base64

from sqlalchemy.orm import Session

from app.enums import Role
from app.models import Farmer, HoneyBatch
from app.services import anchoring, audit_log, merkle, ots
from tests.fakes import FakeCalendar


def _seed_batch(engine, *, batch_code: str = "B-TEST000001") -> int:
    with Session(engine) as s:
        # Phone is unique, so derive it from the batch code: a test that seeds
        # two batches must not collide on the farmer.
        phone = f"+2547{abs(hash(batch_code)) % 100_000_000:08d}"
        farmer = Farmer(first_name="Jane", last_name="Doe", phone=phone, enrolled_by=None)
        s.add(farmer)
        s.flush()
        batch = HoneyBatch(farmer_id=farmer.id, batch_code=batch_code)
        s.add(batch)
        s.flush()
        audit_log.append(
            s,
            actor_id=None,
            actor_role=Role.operator,
            subject_type="batch",
            subject_id=str(batch.id),
            action="batch.created",
            payload={"batch_code": batch_code},
        )
        s.commit()
        return batch.id


def _anchor_everything(engine, *, confirm_at_height: int | None = None) -> None:
    calendar = FakeCalendar(confirm_at_height=confirm_at_height)
    with Session(engine) as s:
        anchoring.stamp_with_session(s, calendars=[calendar])
        s.commit()
    if confirm_at_height is not None:
        with Session(engine) as s:
            anchoring.upgrade_pending_with_session(s, calendars=[calendar])
            s.commit()


def test_a_record_not_yet_anchored_reports_pending(client, migrated_engine):
    batch_id = _seed_batch(migrated_engine)

    r = client.get(f"/v2/batches/{batch_id}/anchor-proof")

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "pending"
    entry = body["entries"][0]
    # Pending is a value, not an absence — the client must not have to guess.
    assert entry["status"] == "pending"
    assert entry["merkle_root"] is None
    assert entry["ots_proof"] is None
    assert entry["row_hash"]  # the record itself is already in the log


def test_an_anchored_record_reports_anchored_with_a_verifiable_path(client, migrated_engine):
    batch_id = _seed_batch(migrated_engine)
    _anchor_everything(migrated_engine)

    body = client.get(f"/v2/batches/{batch_id}/anchor-proof").json()

    assert body["status"] == "anchored"
    entry = body["entries"][0]
    assert entry["status"] == "anchored"
    assert entry["anchored_at"] is not None
    assert entry["verified_at"] is None

    # The returned path must actually verify against the returned root.
    leaf = merkle.leaf_hash(bytes.fromhex(entry["row_hash"]))
    path = [
        merkle.ProofStep(bytes.fromhex(s["sibling"]), s["position"]) for s in entry["merkle_path"]
    ]
    assert merkle.verify_inclusion(leaf, path, bytes.fromhex(entry["merkle_root"])) is True

    # ...and the root must be what the .ots proof commits to, or the two halves
    # of the proof describe different things.
    proof = base64.b64decode(entry["ots_proof"])
    assert ots.message(proof) == bytes.fromhex(entry["merkle_root"])


def test_a_confirmed_record_reports_confirmed(client, migrated_engine):
    batch_id = _seed_batch(migrated_engine)
    _anchor_everything(migrated_engine, confirm_at_height=903_500)

    body = client.get(f"/v2/batches/{batch_id}/anchor-proof").json()

    assert body["status"] == "confirmed"
    entry = body["entries"][0]
    assert entry["status"] == "confirmed"
    assert entry["verified_at"] is not None
    assert ots.is_confirmed(base64.b64decode(entry["ots_proof"])) is True


def test_a_batch_with_anchored_and_pending_records_reports_partial(client, migrated_engine):
    batch_id = _seed_batch(migrated_engine)
    _anchor_everything(migrated_engine)

    # A later action on the same batch, after the anchor run.
    with Session(migrated_engine) as s:
        audit_log.append(
            s,
            actor_id=None,
            actor_role=Role.operator,
            subject_type="batch",
            subject_id=str(batch_id),
            action="batch.harvest_recorded",
            payload={"kg": 12},
        )
        s.commit()

    body = client.get(f"/v2/batches/{batch_id}/anchor-proof").json()

    assert body["status"] == "partial"
    assert [e["status"] for e in body["entries"]] == ["anchored", "pending"]


def test_entries_cover_every_audit_row_for_the_batch_in_order(client, migrated_engine):
    batch_id = _seed_batch(migrated_engine)
    with Session(migrated_engine) as s:
        for action in ("batch.harvest_recorded", "batch.processed"):
            audit_log.append(
                s,
                actor_id=None,
                actor_role=Role.operator,
                subject_type="batch",
                subject_id=str(batch_id),
                action=action,
                payload={},
            )
        s.commit()
    _anchor_everything(migrated_engine)

    body = client.get(f"/v2/batches/{batch_id}/anchor-proof").json()

    assert [e["action"] for e in body["entries"]] == [
        "batch.created",
        "batch.harvest_recorded",
        "batch.processed",
    ]
    assert [e["audit_id"] for e in body["entries"]] == sorted(
        e["audit_id"] for e in body["entries"]
    )
    # All three share one anchor, so every path must verify against that root.
    for entry in body["entries"]:
        leaf = merkle.leaf_hash(bytes.fromhex(entry["row_hash"]))
        path = [
            merkle.ProofStep(bytes.fromhex(s["sibling"]), s["position"])
            for s in entry["merkle_path"]
        ]
        assert merkle.verify_inclusion(leaf, path, bytes.fromhex(entry["merkle_root"]))


def test_another_batchs_rows_are_not_included(client, migrated_engine):
    mine = _seed_batch(migrated_engine, batch_code="B-MINE000001")
    _seed_batch(migrated_engine, batch_code="B-OTHER00001")
    _anchor_everything(migrated_engine)

    body = client.get(f"/v2/batches/{mine}/anchor-proof").json()

    assert len(body["entries"]) == 1
    assert body["batch_id"] == mine


def test_an_unknown_batch_is_a_clean_404(client):
    r = client.get("/v2/batches/999999/anchor-proof")

    assert r.status_code == 404
    assert r.json()["code"] == "batch_not_found"


def test_the_endpoint_needs_no_authentication(client, migrated_engine):
    # The consumer scanning a jar is not a user (04 §5.3).
    batch_id = _seed_batch(migrated_engine)
    assert client.get(f"/v2/batches/{batch_id}/anchor-proof").status_code == 200


# --- anchor health (05 §3.6: anchor lag is a metric worth having) ----------


def test_anchor_health_reports_the_unanchored_backlog(client, migrated_engine):
    anchoring.reset()
    _seed_batch(migrated_engine)

    body = client.get("/v2/audit/anchor-health").json()

    assert body["pending_rows"] == 1
    assert body["last_anchored_audit_id"] is None
    assert body["oldest_unanchored_at"] is not None


def test_anchor_health_reports_a_pending_anchor_then_a_confirmed_one(client, migrated_engine):
    anchoring.reset()
    _seed_batch(migrated_engine)
    _anchor_everything(migrated_engine)

    body = client.get("/v2/audit/anchor-health").json()
    assert body["pending_rows"] == 0
    assert body["pending_anchors"] == 1
    assert body["last_anchored_audit_id"] is not None

    _anchor_everything(migrated_engine, confirm_at_height=903_600)
    body = client.get("/v2/audit/anchor-health").json()
    assert body["pending_anchors"] == 0
