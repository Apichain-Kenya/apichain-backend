"""Phase 3a acceptance: one batch, all six states, end to end (P3-J, 10 §11.1).

This file is the phase's acceptance evidence. It drives a batch from creation
to `DISTRIBUTED` through the real HTTP endpoints, each step by the role that
owns it, then checks every layer that the walk should have touched:

1. **The audit log** holds exactly eight rows for the batch, in order:
   `batch.created` plus one row per stage record (apiary and metadata at S0,
   then harvest, process, lab, packaging, distribution). The plan said seven;
   it counted stage records and forgot the header row.
2. **The hash chain** still verifies end to end.
3. **The anchoring worker** (stamp job, fake calendar) covers every one of
   those rows.
4. **`anchor-proof`** reports every entry `anchored`.
5. **`/verify`** reports all seven stage blocks matching and anchored, and the
   witnessed Codex verdict.

Nothing here is mocked except the OpenTimestamps calendar, which CI must
never reach.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.enums import BatchState, Role
from app.models import AuditLog, HoneyBatch
from app.services import audit_log
from tests.helpers import METADATA, auth, public_path, seed_apiary, seed_farmer, seed_user
from tests.test_anchor_proof_endpoint import _anchor_everything
from tests.test_lab_verify import CLEAN_PANEL
from tests.test_package_distribute import DISTRIBUTE, PACKAGE
from tests.test_transitions_endpoints import HARVEST, PROCESS

EXPECTED_ACTIONS = [
    "batch.created",
    "batch.apiary_recorded",
    "batch.metadata_recorded",
    "batch.harvest_recorded",
    "batch.process_recorded",
    "batch.lab_verified",
    "batch.packaged",
    "batch.distributed",
]
BLOCKS = ("apiary", "metadata", "harvest", "process", "lab", "packaging", "distribution")


def test_a_batch_walks_all_six_states_and_every_record_is_anchored(client, migrated_engine):
    # --- the walk: each step by the role that owns it ----------------------
    farmer_id = seed_farmer(migrated_engine, "+254700070001")
    apiary_id = seed_apiary(migrated_engine, farmer_id)
    farmer = auth(
        seed_user(migrated_engine, Role.farmer, "accept-farmer", farmer_id=farmer_id),
        Role.farmer,
    )
    operator = auth(seed_user(migrated_engine, Role.operator, "accept-op"), Role.operator)
    lab = auth(seed_user(migrated_engine, Role.lab_officer, "accept-lab"), Role.lab_officer)

    created = client.post(
        "/v2/batches",
        json={"farmer_id": farmer_id, "apiary_id": apiary_id, "metadata": METADATA},
        headers=farmer,
    )
    assert created.status_code == 201, created.text
    batch_id = created.json()["id"]
    assert created.json()["state"] == "CREATED"

    steps = [
        ("harvest", HARVEST, farmer, "HARVESTED"),
        ("process", PROCESS, operator, "PROCESSED"),
        ("lab-verify", CLEAN_PANEL, lab, "LAB_VERIFIED"),
        ("package", PACKAGE, operator, "PACKAGED"),
        ("distribute", DISTRIBUTE, operator, "DISTRIBUTED"),
    ]
    for stage, body, headers, expected_state in steps:
        r = client.post(f"/v2/batches/{batch_id}/{stage}", json=body, headers=headers)
        assert r.status_code == 201, (stage, r.text)
        assert r.json()["state"] == expected_state, stage

    # --- 1. eight audit rows, in order, attributed to who acted ------------
    with Session(migrated_engine) as s:
        assert s.get(HoneyBatch, batch_id).state is BatchState.DISTRIBUTED
        rows = (
            s.execute(
                select(AuditLog)
                .where(AuditLog.subject_type == "batch", AuditLog.subject_id == str(batch_id))
                .order_by(AuditLog.id)
            )
            .scalars()
            .all()
        )
        assert [r.action for r in rows] == EXPECTED_ACTIONS
        roles = {r.action: r.actor_role for r in rows}
        assert roles["batch.created"] == Role.farmer
        assert roles["batch.harvest_recorded"] == Role.farmer
        assert roles["batch.process_recorded"] == Role.operator
        assert roles["batch.lab_verified"] == Role.lab_officer
        assert roles["batch.distributed"] == Role.operator
        batch_audit_ids = {r.id for r in rows}

        # --- 2. the chain verifies -----------------------------------------
        assert audit_log.verify_chain(s).ok

    # --- 3. the stamp job covers every row --------------------------------
    _anchor_everything(migrated_engine)

    # --- 4. anchor-proof: every entry anchored ----------------------------
    proof = client.get(public_path(migrated_engine, batch_id, "anchor-proof"))
    assert proof.status_code == 200, proof.text
    entries = proof.json()["entries"]
    assert {e["audit_id"] for e in entries} == batch_audit_ids
    assert [e["action"] for e in entries] == EXPECTED_ACTIONS
    assert {e["status"] for e in entries} == {"anchored"}
    assert all(e["merkle_path"] is not None and e["ots_proof"] for e in entries)
    assert proof.json()["status"] == "anchored"

    # --- 5. /verify: seven blocks match and are anchored ------------------
    verify = client.get(public_path(migrated_engine, batch_id, "verify"))
    assert verify.status_code == 200, verify.text
    body = verify.json()
    assert body["state"] == "DISTRIBUTED"
    assert body["anchor_status"] == "anchored"
    for name in BLOCKS:
        block = body["verification"][name]
        assert block["match"] is True, name
        assert block["anchor_status"] == "anchored", name
        assert block["audit_id"] in batch_audit_ids, name
    assert body["conformance"]["verdict"] == "pass"
    assert body["conformance"]["rule_set_version"] == "codex-kenya-v1"
