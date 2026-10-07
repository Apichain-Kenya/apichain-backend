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
every payload field is classified public, reduced or withheld
(`verification.FIELD_POLICY`, fail-closed): hive coordinates go to 2 dp
(~1.1 km), altitude to 100 m, and free text, the analyst's name and the
transport reference are withheld. A redacted block carries **no hashes**: the
hash commits to the exact values, and a 2 dp cell holds only ~10^8 six-dp
candidates, so publishing the hash would let anyone brute-force the hive's
location back out of it in minutes.
"""

import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from app.enums import Role
from app.models import (
    ApiaryRecord,
    AuditLog,
    BatchMetadata,
    HarvestRecord,
    LabResult,
)
from app.services import stage_payloads, verification
from app.services.canonical import compute_data_hash
from tests.helpers import METADATA, auth, seed_apiary, seed_farmer, seed_user
from tests.test_anchor_proof_endpoint import _anchor_everything
from tests.test_lab_verify import CLEAN_PANEL, _processed_batch
from tests.test_package_distribute import DISTRIBUTE, PACKAGE, _distributed_batch
from tests.test_transitions_endpoints import HARVEST, PROCESS, _batch

BLOCKS = ("apiary", "metadata", "harvest", "process", "lab", "packaging", "distribution")
EXACT_COORDS = ("1.286389", "36.817223")


def _get(client, batch_id: int, **kwargs):
    return client.get(f"/v2/batches/{batch_id}/verify", **kwargs)


def _verify(client, batch_id: int) -> dict:
    r = _get(client, batch_id)
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
    """For a block whose public form equals its pre-image, a consumer can hash
    the payload shown and get the recorded hash themselves."""
    batch_id = _distributed_batch(client, migrated_engine, "+254700060002")

    block = _verify(client, batch_id)["verification"]["metadata"]

    assert block["payload_precision"] == "exact"
    assert block["redacted_fields"] == []
    assert compute_data_hash(block["payload"]).hex() == block["recorded_hash"]
    assert block["recomputed_hash"] == block["recorded_hash"]


def test_a_block_whose_withheld_fields_are_null_stays_exact(client, migrated_engine):
    """Withholding a null changes nothing, so there is nothing to hide and the
    block stays reproducible."""
    batch_id = _batch(client, migrated_engine, "+254700060019")
    headers = auth(seed_user(migrated_engine, Role.operator, "op-null-notes"), Role.operator)
    client.post(f"/v2/batches/{batch_id}/harvest", json=HARVEST, headers=headers)
    client.post(
        f"/v2/batches/{batch_id}/process",
        json={"extraction_method": "centrifugal", "moisture_content": "18.20"},
        headers=headers,
    )

    block = _verify(client, batch_id)["verification"]["process"]

    assert block["payload_precision"] == "exact"
    assert compute_data_hash(block["payload"]).hex() == block["recorded_hash"]


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


def test_the_metadata_summary_carries_no_free_text(client, migrated_engine):
    batch_id = _batch(client, migrated_engine, "+254700060005")

    body = _verify(client, batch_id)

    assert body["metadata"]["honey_type"] == "acacia"
    assert "notes" not in body["metadata"]
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
            update(BatchMetadata)
            .where(BatchMetadata.batch_id == batch_id)
            .values(honey_type="wildflower")
        )
        s.commit()

    body = _verify(client, batch_id)

    block = body["verification"]["metadata"]
    assert block["match"] is False
    assert block["recomputed_hash"] != block["recorded_hash"]
    for name in set(BLOCKS) - {"metadata"}:
        assert body["verification"][name]["match"] is True, name
    # The audit log itself is untouched, so its anchor proof still holds.
    assert block["anchor_status"] == "anchored"


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
        s.execute(delete(BatchMetadata).where(BatchMetadata.batch_id == batch_id))
        s.commit()

    block = _verify(client, batch_id)["verification"]["metadata"]

    assert block is not None
    assert block["match"] is False
    assert block["payload"] is None
    assert block["recomputed_hash"] is None
    assert block["recorded_hash"] is not None


def test_a_deleted_redacted_row_still_publishes_no_hash(client, migrated_engine):
    """With the row gone there is no public form to compare, so the static
    policy decides. Otherwise the recorded hash over the exact coordinates
    would be published beside nothing at all."""
    batch_id = _distributed_batch(client, migrated_engine, "+254700060020")
    with Session(migrated_engine) as s:
        s.execute(delete(ApiaryRecord).where(ApiaryRecord.batch_id == batch_id))
        s.commit()

    block = _verify(client, batch_id)["verification"]["apiary"]

    assert block["match"] is False
    assert block["payload_precision"] == "reduced"
    assert block["recorded_hash"] is None


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

    r = _get(client, batch_id)

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
    assert {lat, lon} <= set(payload_block["redacted_fields"])


def test_the_apiary_block_coarsens_altitude_and_withholds_the_apiary_id(client, migrated_engine):
    batch_id = _distributed_batch(client, migrated_engine, "+254700060023")

    block = _verify(client, batch_id)["verification"]["apiary"]

    assert block["payload"]["altitude"] == "1800"  # seeded at 1795.00
    assert block["payload"]["apiary_id"] is None
    assert block["payload"]["vegetation_type"] == "acacia_woodland"


def test_the_analyst_name_is_withheld(client, migrated_engine):
    batch_id = _distributed_batch(client, migrated_engine, "+254700060013")

    r = _get(client, batch_id)
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

    r = _get(client, batch_id)
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


def test_no_free_text_reaches_the_anonymous_view(client, migrated_engine):
    """Body-level rather than per-field: a sentinel in every free-text field,
    and none of them may appear anywhere in the response."""
    sentinels = {
        "metadata_notes": "SENTINEL-META",
        "harvest_notes": "SENTINEL-HARVEST",
        "handling_notes": "SENTINEL-HANDLING",
        "lab_notes": "SENTINEL-LAB",
        "analyst_name": "SENTINEL-ANALYST",
        "packaging_notes": "SENTINEL-PACK",
        "transport_reference": "SENTINEL-TRANSPORT",
        "handover_notes": "SENTINEL-HANDOVER",
    }
    farmer_id = seed_farmer(migrated_engine, "+254700060025")
    apiary_id = seed_apiary(migrated_engine, farmer_id)
    op = auth(seed_user(migrated_engine, Role.operator, "op-sentinel"), Role.operator)
    lab = auth(seed_user(migrated_engine, Role.lab_officer, "lab-sentinel"), Role.lab_officer)
    created = client.post(
        "/v2/batches",
        json={
            "farmer_id": farmer_id,
            "apiary_id": apiary_id,
            "metadata": {**METADATA, "notes": sentinels["metadata_notes"]},
        },
        headers=op,
    )
    assert created.status_code == 201, created.text
    batch_id = created.json()["id"]
    steps = [
        ("harvest", {**HARVEST, "notes": sentinels["harvest_notes"]}, op),
        ("process", {**PROCESS, "handling_notes": sentinels["handling_notes"]}, op),
        (
            "lab-verify",
            {
                **CLEAN_PANEL,
                "notes": sentinels["lab_notes"],
                "analyst_name": sentinels["analyst_name"],
            },
            lab,
        ),
        ("package", {**PACKAGE, "notes": sentinels["packaging_notes"]}, op),
        (
            "distribute",
            {
                **DISTRIBUTE,
                "transport_reference": sentinels["transport_reference"],
                "handover_notes": sentinels["handover_notes"],
            },
            op,
        ),
    ]
    for stage, body, headers in steps:
        r = client.post(f"/v2/batches/{batch_id}/{stage}", json=body, headers=headers)
        assert r.status_code == 201, (stage, r.text)

    r = _get(client, batch_id)

    assert r.status_code == 200
    for field, sentinel in sentinels.items():
        assert sentinel not in r.text, field
    assert all(b["match"] for b in r.json()["verification"].values())


def test_every_payload_field_is_classified(client, migrated_engine):
    """Fail-closed is the runtime guard; this makes an unclassified field a
    test failure rather than a silently withheld one."""
    batch_id = _distributed_batch(client, migrated_engine, "+254700060024")

    blocks = _verify(client, batch_id)["verification"]

    for name in BLOCKS:
        assert set(blocks[name]["payload"]) == set(verification.FIELD_POLICY[name]), name


def test_no_pii_tagged_column_is_classified_public():
    for name, model in verification.STAGE_MODELS.items():
        tagged = {c.name for c in model.__table__.columns if c.info.get("pii")}
        public = {k for k, rule in verification.FIELD_POLICY[name].items() if rule == "public"}
        assert not tagged & public, (name, tagged & public)


def test_the_block_registry_covers_every_payload_builder():
    assert set(verification.STAGE_MODELS) == set(stage_payloads.BUILDERS)
    assert set(verification.AUDIT_ACTIONS) == set(stage_payloads.BUILDERS)
    assert set(verification.FIELD_POLICY) == set(stage_payloads.BUILDERS)


# --- the endpoint contract --------------------------------------------------


def test_verify_is_public_by_design(client, migrated_engine):
    """The consumer scanning a jar is not a user (04 §5.3)."""
    batch_id = _batch(client, migrated_engine, "+254700060015")

    assert _get(client, batch_id).status_code == 200


def test_verify_ignores_a_bearer_token(client, migrated_engine):
    batch_id = _batch(client, migrated_engine, "+254700060016")
    operator = seed_user(migrated_engine, Role.operator, "op-verify")

    r = _get(client, batch_id, headers=auth(operator, Role.operator))

    assert r.status_code == 200
    assert "36.817223" not in r.text  # staff see exact values elsewhere, not here


def test_an_unknown_batch_is_404(client, migrated_engine):
    r = client.get("/v2/batches/999999/verify")

    assert r.status_code == 404
    assert r.json()["code"] == "batch_not_found"


def test_an_out_of_range_batch_id_is_422(client, migrated_engine):
    assert client.get("/v2/batches/2147483648/verify").status_code == 422
