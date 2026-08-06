"""S3 lab-verify: the panel, the verdict, and what gets anchored (P3-G, 10 §9).

This is the transition that differs. After the `lab_results` insert the handler
runs `codex_scoring.evaluate` — pure, so it cannot fail on I/O — writes the
`codex_conformance` row, and folds the full verdict into the audit payload, so
what is anchored is the judgement and not only the measurements.

**A failing verdict does not block the transition.** `LAB_VERIFIED` means "a
lab result has been recorded", not "the honey passed". Conflating the two would
make a failing result unrecordable, which is how bad results go missing. The
verdict is anchored and surfaced; the state machine stays orthogonal.
"""

from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.enums import BatchState, ConformanceVerdict, Role
from app.models import AuditLog, CodexConformance, HoneyBatch, LabResult
from app.services import codex_scoring, stage_payloads
from tests.helpers import METADATA, auth, seed_apiary, seed_farmer, seed_user
from tests.test_transitions_endpoints import HARVEST, PROCESS

CLEAN_PANEL = {
    "moisture_pct": "18.40",
    "fructose_glucose_g_100g": "72.10",
    "sucrose_g_100g": "2.30",
    "hmf_mg_kg": "21.00",
    "diastase_schade": "14.20",
    "free_acidity_meq_kg": "28.00",
    "pollen_density": "64.50",
    "laboratory_name": "KEBS Nairobi",
    "analyst_name": "J. Wanjiru",
    "certificate_number": "KEBS-2026-0042",
    "tested_at": "2026-04-02T11:00:00Z",
}


def _processed_batch(client, engine, phone: str) -> int:
    farmer_id = seed_farmer(engine, phone)
    apiary_id = seed_apiary(engine, farmer_id)
    operator = seed_user(engine, Role.operator, f"labop{phone[-6:]}")
    headers = auth(operator, Role.operator)
    created = client.post(
        "/v2/batches",
        json={"farmer_id": farmer_id, "apiary_id": apiary_id, "metadata": METADATA},
        headers=headers,
    )
    batch_id = created.json()["id"]
    client.post(f"/v2/batches/{batch_id}/harvest", json=HARVEST, headers=headers)
    client.post(f"/v2/batches/{batch_id}/process", json=PROCESS, headers=headers)
    return batch_id


def _lab(engine, name: str) -> dict[str, str]:
    return auth(seed_user(engine, Role.lab_officer, name), Role.lab_officer)


def test_a_clean_panel_advances_the_state_and_records_a_pass(client, migrated_engine):
    batch_id = _processed_batch(client, migrated_engine, "+254700040001")

    r = client.post(
        f"/v2/batches/{batch_id}/lab-verify",
        json=CLEAN_PANEL,
        headers=_lab(migrated_engine, "lab-1"),
    )

    assert r.status_code == 201, r.text
    assert r.json()["state"] == "LAB_VERIFIED"
    with Session(migrated_engine) as s:
        assert s.get(HoneyBatch, batch_id).state is BatchState.LAB_VERIFIED
        conformance = s.execute(
            select(CodexConformance).where(CodexConformance.batch_id == batch_id)
        ).scalar_one()
        assert conformance.verdict is ConformanceVerdict.PASS
        assert conformance.rule_set_version == "codex-kenya-v1"


def test_a_failing_panel_still_records_the_result(client, migrated_engine):
    """A lab result that fails must be recordable. Blocking the transition is
    how a bad result quietly never gets entered."""
    batch_id = _processed_batch(client, migrated_engine, "+254700040002")

    r = client.post(
        f"/v2/batches/{batch_id}/lab-verify",
        json={**CLEAN_PANEL, "sucrose_g_100g": "9.00"},
        headers=_lab(migrated_engine, "lab-2"),
    )

    assert r.status_code == 201, r.text
    assert r.json()["state"] == "LAB_VERIFIED"
    with Session(migrated_engine) as s:
        conformance = s.execute(
            select(CodexConformance).where(CodexConformance.batch_id == batch_id)
        ).scalar_one()
        assert conformance.verdict is ConformanceVerdict.FAIL
        assert conformance.sucrose_passed is False
        assert conformance.moisture_passed is True


def test_an_incomplete_panel_is_incomplete_not_pass(client, migrated_engine):
    batch_id = _processed_batch(client, migrated_engine, "+254700040003")
    partial = {k: v for k, v in CLEAN_PANEL.items() if k != "diastase_schade"}

    r = client.post(
        f"/v2/batches/{batch_id}/lab-verify",
        json=partial,
        headers=_lab(migrated_engine, "lab-3"),
    )

    assert r.status_code == 201, r.text
    with Session(migrated_engine) as s:
        conformance = s.execute(
            select(CodexConformance).where(CodexConformance.batch_id == batch_id)
        ).scalar_one()
        assert conformance.verdict is ConformanceVerdict.INCOMPLETE
        assert conformance.diastase_passed is None, "unmeasured must be NULL, not False"


def test_the_verdict_is_anchored_alongside_the_measurements(client, migrated_engine):
    """What is committed to the chain must include the judgement, not only the
    numbers — otherwise the verdict is re-derivable but never witnessed."""
    batch_id = _processed_batch(client, migrated_engine, "+254700040004")

    client.post(
        f"/v2/batches/{batch_id}/lab-verify",
        json=CLEAN_PANEL,
        headers=_lab(migrated_engine, "lab-4"),
    )

    with Session(migrated_engine) as s:
        row = s.execute(
            select(AuditLog).where(AuditLog.action == "batch.lab_verified")
        ).scalar_one()

        assert row.payload["conformance"]["verdict"] == "PASS"
        assert row.payload["conformance"]["rule_set_version"] == "codex-kenya-v1"
        assert row.payload["moisture_pct"] == "18.40"


def test_the_anchored_payload_carries_the_rule_set_version(client, migrated_engine):
    """A verdict must stay interpretable after the limits change: the version
    names the frozen rule set it was evaluated against."""
    batch_id = _processed_batch(client, migrated_engine, "+254700040005")

    client.post(
        f"/v2/batches/{batch_id}/lab-verify",
        json=CLEAN_PANEL,
        headers=_lab(migrated_engine, "lab-5"),
    )

    with Session(migrated_engine) as s:
        row = s.execute(
            select(AuditLog).where(AuditLog.action == "batch.lab_verified")
        ).scalar_one()
        conformance = row.payload["conformance"]

        assert conformance["rule_set_version"] in codex_scoring.RULES
        parameters = {p["parameter"]: p for p in conformance["parameters"]}
        assert parameters["hmf"]["limit"] == "80.00"
        assert parameters["hmf"]["basis"] == "codex-annex"


def test_the_anchored_payload_has_no_blended_score(client, migrated_engine):
    """02 §4: one number hid which question failed. It must not reappear on
    the wire or in the anchored record."""
    batch_id = _processed_batch(client, migrated_engine, "+254700040006")

    client.post(
        f"/v2/batches/{batch_id}/lab-verify",
        json=CLEAN_PANEL,
        headers=_lab(migrated_engine, "lab-6"),
    )

    with Session(migrated_engine) as s:
        row = s.execute(
            select(AuditLog).where(AuditLog.action == "batch.lab_verified")
        ).scalar_one()
        flat = str(row.payload)

        for banned in ("authenticity_score", "confidence", "sucrose_level", "predicted_"):
            assert banned not in flat


def test_the_stored_panel_matches_the_canonical_builder(client, migrated_engine):
    batch_id = _processed_batch(client, migrated_engine, "+254700040007")

    client.post(
        f"/v2/batches/{batch_id}/lab-verify",
        json=CLEAN_PANEL,
        headers=_lab(migrated_engine, "lab-7"),
    )

    with Session(migrated_engine) as s:
        record = s.execute(select(LabResult).where(LabResult.batch_id == batch_id)).scalar_one()
        payload = stage_payloads.lab_result(record)

        assert payload["sucrose_g_100g"] == "2.30"
        assert payload["tested_at"] == "2026-04-02T11:00:00"
        assert record.pollen_density == Decimal("64.50")


def test_pollen_density_is_stored_but_not_scored(client, migrated_engine):
    """Not a Codex parameter. 02 demotes pollen to a corroboration signal until
    melissopalynology lands, at which point it belongs to origin_verification."""
    batch_id = _processed_batch(client, migrated_engine, "+254700040008")

    client.post(
        f"/v2/batches/{batch_id}/lab-verify",
        json=CLEAN_PANEL,
        headers=_lab(migrated_engine, "lab-8"),
    )

    with Session(migrated_engine) as s:
        row = s.execute(
            select(AuditLog).where(AuditLog.action == "batch.lab_verified")
        ).scalar_one()
        scored = {p["parameter"] for p in row.payload["conformance"]["parameters"]}

        assert "pollen" not in scored
        assert row.payload["pollen_density"] == "64.50"


def test_an_operator_may_not_submit_a_lab_result(client, migrated_engine):
    batch_id = _processed_batch(client, migrated_engine, "+254700040009")
    operator = seed_user(migrated_engine, Role.operator, "op-lab-denied")

    r = client.post(
        f"/v2/batches/{batch_id}/lab-verify",
        json=CLEAN_PANEL,
        headers=auth(operator, Role.operator),
    )

    assert r.status_code == 403


def test_lab_verifying_before_processing_is_refused_and_writes_nothing(client, migrated_engine):
    farmer_id = seed_farmer(migrated_engine, "+254700040010")
    apiary_id = seed_apiary(migrated_engine, farmer_id)
    operator = seed_user(migrated_engine, Role.operator, "op-early-lab")
    created = client.post(
        "/v2/batches",
        json={"farmer_id": farmer_id, "apiary_id": apiary_id, "metadata": METADATA},
        headers=auth(operator, Role.operator),
    )
    batch_id = created.json()["id"]

    r = client.post(
        f"/v2/batches/{batch_id}/lab-verify",
        json=CLEAN_PANEL,
        headers=_lab(migrated_engine, "lab-early"),
    )

    assert r.status_code == 409
    assert r.json()["code"] == "invalid_transition"
    with Session(migrated_engine) as s:
        assert s.execute(select(func.count()).select_from(LabResult)).scalar_one() == 0
        assert s.execute(select(func.count()).select_from(CodexConformance)).scalar_one() == 0


def test_a_swapped_sugar_pair_is_refused_at_the_boundary(client, migrated_engine):
    """Sucrose <=5 and fructose+glucose >=60 are the two fields a lab form is
    most likely to transpose, and a swap passes range validation. It is
    chemically impossible, so it is 422 rather than a confident FAIL."""
    batch_id = _processed_batch(client, migrated_engine, "+254700040011")

    r = client.post(
        f"/v2/batches/{batch_id}/lab-verify",
        json={**CLEAN_PANEL, "sucrose_g_100g": "72.10", "fructose_glucose_g_100g": "2.30"},
        headers=_lab(migrated_engine, "lab-swap"),
    )

    assert r.status_code == 422


def test_a_conformance_row_records_one_flag_per_parameter(client, migrated_engine):
    batch_id = _processed_batch(client, migrated_engine, "+254700040012")

    client.post(
        f"/v2/batches/{batch_id}/lab-verify",
        json=CLEAN_PANEL,
        headers=_lab(migrated_engine, "lab-flags"),
    )

    with Session(migrated_engine) as s:
        row = s.execute(
            select(CodexConformance).where(CodexConformance.batch_id == batch_id)
        ).scalar_one()

        assert (
            row.moisture_passed,
            row.fructose_glucose_passed,
            row.sucrose_passed,
            row.hmf_passed,
            row.diastase_passed,
            row.free_acidity_passed,
        ) == (True, True, True, True, True, True)


def test_hmf_between_the_general_and_tropical_limits_passes(client, migrated_engine):
    """Kenya is tropical: Codex Annex 1.3 allows 80 mg/kg. A rule set fixed at
    the general 40 would flag legitimate honey — v1's exact failure mode."""
    batch_id = _processed_batch(client, migrated_engine, "+254700040013")

    client.post(
        f"/v2/batches/{batch_id}/lab-verify",
        json={**CLEAN_PANEL, "hmf_mg_kg": "55.00"},
        headers=_lab(migrated_engine, "lab-hmf"),
    )

    with Session(migrated_engine) as s:
        row = s.execute(
            select(CodexConformance).where(CodexConformance.batch_id == batch_id)
        ).scalar_one()
        assert row.verdict is ConformanceVerdict.PASS
        assert row.hmf_passed is True
