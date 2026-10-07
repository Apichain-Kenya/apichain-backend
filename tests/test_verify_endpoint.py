"""GET /v2/batches/{id}/verify — the consumer's three-way match (P3-I, 10 §10).

For each of the seven stage blocks:

1. **recomputed** — `compute_data_hash(builder(row))` over the stage row now;
2. **recorded** — `payload_hash` on the audit row appended with that record;
3. **witnessed** — that audit row's inclusion in a public Merkle anchor.

(1) vs (2) catches a DB edit after the fact; (2) vs (3) catches an edit to the
audit log itself. The server performs both comparisons. A consumer can check
(3) offline via `anchor-proof`, but cannot link a block's payload hash to its
anchored leaf themselves, because `row_hash` also commits to the actor, IP and
user agent, which are not public.

**Privacy (10 D11, extended by Ian on 2026-10-07).** `/verify` is anonymous, so
every PII-tagged column in a stage payload has an explicit public policy:
apiary and harvest coordinates are reduced to 2 dp (~1.1 km) and the lab's
`analyst_name` is withheld. A redacted block carries **no hashes**: the hash
commits to the exact values, and a 2 dp cell holds only ~10^8 six-dp
candidates, so publishing the hash would let anyone brute-force the hive's
location back out of it in minutes.
"""

import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from app.enums import Role
from app.models import (
    AuditLog,
    HarvestRecord,
    LabResult,
    ProcessRecord,
)
from app.services import stage_payloads, verification
from app.services.canonical import compute_data_hash
from tests.helpers import auth, seed_user
from tests.test_anchor_proof_endpoint import _anchor_everything
from tests.test_lab_verify import CLEAN_PANEL, _processed_batch
from tests.test_package_distribute import _distributed_batch
from tests.test_transitions_endpoints import _batch

BLOCKS = ("apiary", "metadata", "harvest", "process", "lab", "packaging", "distribution")
EXACT_COORDS = ("1.286389", "36.817223")


def _verify(client, batch_id: int) -> dict:
    r = client.get(f"/v2/batches/{batch_id}/verify")
    assert r.status_code == 200, r.text
    return r.json()


# --- the happy path ---------------------------------------------------------


def test_a_distributed_batch_matches_on_all_seven_blocks(client, migrated_engine):
    batch_id = _distributed_batch(client, migrated_engine, "+254700060001")

    body = _verify(client, batch_id)

    assert body["batch_id"] == batch_id
    assert body["state"] == "DISTRIBUTED"
    assert set(body["verification"]) == set(BLOCKS)
    for name in BLOCKS:
        block = body["verification"][name]
        assert block is not None, name
        assert block["match"] is True, name
        assert block["audit_id"] is not None, name
        assert block["anchor_status"] == "pending", name


def test_an_exact_block_is_independently_reproducible(client, migrated_engine):
    """For an unredacted block, the payload shown is the exact pre-image: a
    consumer can hash it and get the recorded hash themselves."""
    batch_id = _distributed_batch(client, migrated_engine, "+254700060002")

    body = _verify(client, batch_id)

    for name in ("metadata", "process", "packaging", "distribution"):
        block = body["verification"][name]
        assert block["payload_precision"] == "exact", name
        assert block["redacted_fields"] == [], name
        assert compute_data_hash(block["payload"]).hex() == block["recorded_hash"], name
        assert block["recomputed_hash"] == block["recorded_hash"], name


def test_anchor_status_follows_the_public_anchor(client, migrated_engine):
    batch_id = _distributed_batch(client, migrated_engine, "+254700060003")
    _anchor_everything(migrated_engine)

    body = _verify(client, batch_id)

    assert body["anchor_status"] == "anchored"
    assert {body["verification"][n]["anchor_status"] for n in BLOCKS} == {"anchored"}


def test_the_conformance_block_reports_facts_not_a_score(client, migrated_engine):
    batch_id = _distributed_batch(client, migrated_engine, "+254700060004")

    conformance = _verify(client, batch_id)["conformance"]

    assert conformance["verdict"] == "pass"
    assert conformance["rule_set_version"] == "codex-kenya-v1"
    assert [p["parameter"] for p in conformance["parameters"]] == [
        "moisture",
        "fructose_glucose",
        "sucrose",
        "hmf",
        "diastase",
        "free_acidity",
    ]
    for p in conformance["parameters"]:
        assert set(p) == {"parameter", "measured", "unit", "comparator", "limit", "basis", "status"}
    # 02 §4: no blended number anywhere.
    assert "score" not in str(conformance).lower()


def test_metadata_notes_are_shown_though_unhashed(client, migrated_engine):
    batch_id = _batch(client, migrated_engine, "+254700060005")

    body = _verify(client, batch_id)

    assert body["metadata"]["honey_type"] == "acacia"
    assert "notes" in body["metadata"]
    assert "notes" not in body["verification"]["metadata"]["payload"]


# --- mid-lifecycle ----------------------------------------------------------


def test_a_batch_mid_lifecycle_reports_later_stages_as_null(client, migrated_engine):
    batch_id = _processed_batch(client, migrated_engine, "+254700060006")

    body = _verify(client, batch_id)

    assert body["state"] == "PROCESSED"
    for name in ("apiary", "metadata", "harvest", "process"):
        assert body["verification"][name]["match"] is True, name
    for name in ("lab", "packaging", "distribution"):
        assert body["verification"][name] is None, name
    assert body["conformance"] is None


# --- tampering --------------------------------------------------------------


def test_editing_one_stage_row_breaks_exactly_that_block(client, migrated_engine):
    batch_id = _distributed_batch(client, migrated_engine, "+254700060007")
    _anchor_everything(migrated_engine)
    with Session(migrated_engine) as s:
        s.execute(
            update(ProcessRecord)
            .where(ProcessRecord.batch_id == batch_id)
            .values(extraction_method="pressed")
        )
        s.commit()

    body = _verify(client, batch_id)

    assert body["verification"]["process"]["match"] is False
    assert (
        body["verification"]["process"]["recomputed_hash"]
        != body["verification"]["process"]["recorded_hash"]
    )
    for name in set(BLOCKS) - {"process"}:
        assert body["verification"][name]["match"] is True, name
    # The audit log itself is untouched, so its anchor proof still holds.
    assert body["verification"]["process"]["anchor_status"] == "anchored"


def test_editing_a_redacted_block_is_still_detected(client, migrated_engine):
    batch_id = _distributed_batch(client, migrated_engine, "+254700060008")
    with Session(migrated_engine) as s:
        s.execute(
            update(HarvestRecord).where(HarvestRecord.batch_id == batch_id).values(quantity_kg=99)
        )
        s.commit()

    block = _verify(client, batch_id)["verification"]["harvest"]

    assert block["match"] is False
    assert block["recorded_hash"] is None and block["recomputed_hash"] is None


def test_editing_a_lab_measurement_breaks_the_lab_block(client, migrated_engine):
    """The lab payload embeds the verdict, so the recomputation re-runs the
    scorer under the *recorded* rule set, and a changed measurement breaks it.
    The verdict shown is still the witnessed one, not one re-derived from the
    edited row."""
    batch_id = _distributed_batch(client, migrated_engine, "+254700060009")
    with Session(migrated_engine) as s:
        s.execute(update(LabResult).where(LabResult.batch_id == batch_id).values(hmf_mg_kg=95))
        s.commit()

    body = _verify(client, batch_id)

    assert body["verification"]["lab"]["match"] is False
    assert body["conformance"]["verdict"] == "pass"


def test_doctoring_a_failing_panel_cannot_show_a_pass(client, migrated_engine):
    """The case /verify exists for: someone with DB access edits a failing
    panel into a passing one. The lab block must break, and the verdict shown
    must stay the anchored `fail`."""
    batch_id = _processed_batch(client, migrated_engine, "+254700060017")
    lab = auth(seed_user(migrated_engine, Role.lab_officer, "lab-doctor"), Role.lab_officer)
    r = client.post(
        f"/v2/batches/{batch_id}/lab-verify",
        json={**CLEAN_PANEL, "hmf_mg_kg": "95.00"},
        headers=lab,
    )
    assert r.status_code == 201, r.text
    with Session(migrated_engine) as s:
        s.execute(update(LabResult).where(LabResult.batch_id == batch_id).values(hmf_mg_kg=20))
        s.commit()

    body = _verify(client, batch_id)

    assert body["verification"]["lab"]["match"] is False
    assert body["conformance"]["verdict"] == "fail"


def test_a_tampered_audit_payload_withholds_the_verdict(client, migrated_engine):
    """The verdict is read from the audit row's JSONB, so that copy is checked
    against the row's own payload_hash first. An edited copy shows nothing."""
    batch_id = _distributed_batch(client, migrated_engine, "+254700060018")
    with Session(migrated_engine) as s:
        row = s.execute(
            select(AuditLog).where(
                AuditLog.subject_id == str(batch_id), AuditLog.action == "batch.lab_verified"
            )
        ).scalar_one()
        doctored = dict(row.payload)
        doctored["conformance"] = {**doctored["conformance"], "verdict": "FAIL"}
        s.execute(update(AuditLog).where(AuditLog.id == row.id).values(payload=doctored))
        s.commit()

    assert _verify(client, batch_id)["conformance"] is None


def test_a_deleted_stage_row_is_a_mismatch_not_an_unreached_stage(client, migrated_engine):
    """The audit log says the stage happened. A missing row is tampering, and
    reporting `null` would make it look like the stage was never reached."""
    batch_id = _distributed_batch(client, migrated_engine, "+254700060010")
    with Session(migrated_engine) as s:
        s.execute(delete(ProcessRecord).where(ProcessRecord.batch_id == batch_id))
        s.commit()

    block = _verify(client, batch_id)["verification"]["process"]

    assert block is not None
    assert block["match"] is False
    assert block["payload"] is None
    assert block["recomputed_hash"] is None
    assert block["recorded_hash"] is not None


def test_a_stage_row_without_its_audit_row_is_a_mismatch(client, migrated_engine):
    batch_id = _distributed_batch(client, migrated_engine, "+254700060011")
    with Session(migrated_engine) as s:
        # Detach the audit row from this batch rather than deleting it, so the
        # chain itself stays intact and only the link is missing.
        s.execute(
            update(AuditLog)
            .where(AuditLog.subject_id == str(batch_id), AuditLog.action == "batch.packaged")
            .values(subject_id="0")
        )
        s.commit()

    block = _verify(client, batch_id)["verification"]["packaging"]

    assert block["match"] is False
    assert block["audit_id"] is None
    assert block["recorded_hash"] is None
    assert block["anchor_status"] is None


# --- privacy (D11, extended 2026-10-07) -------------------------------------


def test_exact_coordinates_appear_nowhere_in_the_response(client, migrated_engine):
    batch_id = _distributed_batch(client, migrated_engine, "+254700060012")

    r = client.get(f"/v2/batches/{batch_id}/verify")

    for exact in EXACT_COORDS:
        assert exact not in r.text, exact


@pytest.mark.parametrize(
    ("block", "lat", "lon", "phone"),
    [
        ("apiary", "latitude", "longitude", "+254700060021"),
        ("harvest", "gps_lat", "gps_lon", "+254700060022"),
    ],
)
def test_coordinates_are_reduced_to_two_places(client, migrated_engine, block, lat, lon, phone):
    batch_id = _distributed_batch(client, migrated_engine, phone)

    payload_block = _verify(client, batch_id)["verification"][block]

    assert payload_block["payload_precision"] == "reduced"
    assert payload_block["payload"][lat] == "-1.29"
    assert payload_block["payload"][lon] == "36.82"
    assert set(payload_block["redacted_fields"]) == {lat, lon}


def test_the_analyst_name_is_withheld(client, migrated_engine):
    batch_id = _distributed_batch(client, migrated_engine, "+254700060013")

    r = client.get(f"/v2/batches/{batch_id}/verify")
    block = r.json()["verification"]["lab"]

    assert "J. Wanjiru" not in r.text
    assert block["payload"]["analyst_name"] is None
    assert block["redacted_fields"] == ["analyst_name"]
    assert block["payload"]["laboratory_name"] == "KEBS Nairobi"
    assert block["payload"]["certificate_number"] == "KEBS-2026-0042"


def test_a_redacted_block_publishes_no_hash(client, migrated_engine):
    """The hash commits to the exact values. Publishing it next to values
    reduced to 2 dp would let anyone recover the exact ones by brute force."""
    batch_id = _distributed_batch(client, migrated_engine, "+254700060014")

    r = client.get(f"/v2/batches/{batch_id}/verify")
    body = r.json()

    with Session(migrated_engine) as s:
        for name, action in (
            ("apiary", "batch.apiary_recorded"),
            ("harvest", "batch.harvest_recorded"),
            ("lab", "batch.lab_verified"),
        ):
            block = body["verification"][name]
            assert block["recorded_hash"] is None, name
            assert block["recomputed_hash"] is None, name
            assert block["match"] is True, name
            recorded = s.execute(
                select(AuditLog.payload_hash).where(
                    AuditLog.subject_id == str(batch_id), AuditLog.action == action
                )
            ).scalar_one()
            assert recorded.hex() not in r.text, name


def test_every_pii_column_in_a_stage_payload_has_a_public_policy():
    """A newly tagged column must not reach the anonymous view by default."""
    for name, model in verification.STAGE_MODELS.items():
        tagged = {c.name for c in model.__table__.columns if c.info.get("pii")}
        policy = set(verification.PUBLIC_REDACTIONS.get(name, {}))
        assert tagged <= policy, (name, tagged - policy)


def test_the_block_registry_covers_every_payload_builder():
    assert set(verification.STAGE_MODELS) == set(stage_payloads.BUILDERS)
    assert set(verification.AUDIT_ACTIONS) == set(stage_payloads.BUILDERS)


# --- the endpoint contract --------------------------------------------------


def test_verify_is_public_by_design(client, migrated_engine):
    """The consumer scanning a jar is not a user (04 §5.3)."""
    batch_id = _batch(client, migrated_engine, "+254700060015")

    assert client.get(f"/v2/batches/{batch_id}/verify").status_code == 200


def test_verify_ignores_a_bearer_token(client, migrated_engine):
    batch_id = _batch(client, migrated_engine, "+254700060016")
    operator = seed_user(migrated_engine, Role.operator, "op-verify")

    r = client.get(f"/v2/batches/{batch_id}/verify", headers=auth(operator, Role.operator))

    assert r.status_code == 200
    assert "36.817223" not in r.text  # staff see exact values elsewhere, not here


def test_an_unknown_batch_is_404(client, migrated_engine):
    r = client.get("/v2/batches/999999/verify")

    assert r.status_code == 404
    assert r.json()["code"] == "batch_not_found"


def test_an_out_of_range_batch_id_is_422(client, migrated_engine):
    assert client.get("/v2/batches/2147483648/verify").status_code == 422
