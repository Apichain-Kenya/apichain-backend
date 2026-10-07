"""S4 package and S5 distribute, the last two transitions (P3-H, 10 §9).

Same handler order as harvest/process (see `test_transitions_endpoints.py`),
so the same two properties are asserted on every refusal: nothing is written
anywhere, and no audit id is consumed.

The one thing specific to this WP is the terminal state. `DISTRIBUTED` has no
successor, and every transition endpoint — including another `distribute` —
must refuse it. `transitions.py` already enforces that; the endpoint test says
so on purpose, so a future handler that skips `assert_transition` fails here.
"""

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.enums import BatchState, Role
from app.models import AuditLog, DistributionRecord, HoneyBatch, PackagingRecord
from app.services import stage_payloads
from app.services.canonical import compute_data_hash
from tests.helpers import auth, seed_user
from tests.test_lab_verify import CLEAN_PANEL, _processed_batch
from tests.test_transitions_endpoints import HARVEST, PROCESS

PACKAGE = {
    "unit_count": 120,
    "jar_ids": ["J-0001", "J-0002"],
    "notes": "500 g glass jars",
}
DISTRIBUTE = {
    "retailer_name": "Naivas Westlands",
    "transport_reference": "KDA 123X / 2026-04-10",
    "handover_notes": "received chilled",
}


def _audit_count(engine) -> int:
    with Session(engine) as s:
        return s.execute(select(func.count()).select_from(AuditLog)).scalar_one()


def _operator(engine, name: str) -> dict[str, str]:
    return auth(seed_user(engine, Role.operator, name), Role.operator)


def _lab_verified_batch(client, engine, phone: str) -> int:
    batch_id = _processed_batch(client, engine, phone)
    lab = auth(seed_user(engine, Role.lab_officer, f"lab{phone[-6:]}"), Role.lab_officer)
    r = client.post(f"/v2/batches/{batch_id}/lab-verify", json=CLEAN_PANEL, headers=lab)
    assert r.status_code == 201, r.text
    return batch_id


def _packaged_batch(client, engine, phone: str) -> int:
    batch_id = _lab_verified_batch(client, engine, phone)
    r = client.post(
        f"/v2/batches/{batch_id}/package",
        json=PACKAGE,
        headers=_operator(engine, f"pk{phone[-6:]}"),
    )
    assert r.status_code == 201, r.text
    return batch_id


def _distributed_batch(client, engine, phone: str) -> int:
    batch_id = _packaged_batch(client, engine, phone)
    r = client.post(
        f"/v2/batches/{batch_id}/distribute",
        json=DISTRIBUTE,
        headers=_operator(engine, f"ds{phone[-6:]}"),
    )
    assert r.status_code == 201, r.text
    return batch_id


# --- S4 package ------------------------------------------------------------


def test_package_advances_the_state_and_anchors_the_record(client, migrated_engine):
    batch_id = _lab_verified_batch(client, migrated_engine, "+254700050001")
    before = _audit_count(migrated_engine)

    r = client.post(
        f"/v2/batches/{batch_id}/package",
        json=PACKAGE,
        headers=_operator(migrated_engine, "op-pack"),
    )

    assert r.status_code == 201, r.text
    assert r.json()["state"] == "PACKAGED"
    with Session(migrated_engine) as s:
        assert s.get(HoneyBatch, batch_id).state is BatchState.PACKAGED
        record = s.execute(
            select(PackagingRecord).where(PackagingRecord.batch_id == batch_id)
        ).scalar_one()
        assert record.unit_count == 120
        assert list(record.jar_ids) == ["J-0001", "J-0002"]

        rows = s.execute(select(AuditLog).order_by(AuditLog.id)).scalars().all()
        assert len(rows) == before + 1
        row = rows[-1]
        assert row.action == "batch.packaged"
        assert row.subject_type == "batch"
        assert row.subject_id == str(batch_id)
        assert row.payload_hash == compute_data_hash(stage_payloads.packaging_record(record))


def test_packaging_before_lab_verification_is_refused_and_writes_nothing(client, migrated_engine):
    batch_id = _processed_batch(client, migrated_engine, "+254700050002")
    before = _audit_count(migrated_engine)

    r = client.post(
        f"/v2/batches/{batch_id}/package",
        json=PACKAGE,
        headers=_operator(migrated_engine, "op-pack-early"),
    )

    assert r.status_code == 409
    assert r.json()["code"] == "invalid_transition"
    assert r.json()["details"] == {"current_state": "PROCESSED", "attempted_state": "PACKAGED"}
    with Session(migrated_engine) as s:
        assert s.get(HoneyBatch, batch_id).state is BatchState.PROCESSED
        assert s.execute(select(func.count()).select_from(PackagingRecord)).scalar_one() == 0
    assert _audit_count(migrated_engine) == before, "a refused request consumed an audit id"


def test_packaging_twice_is_refused_and_writes_nothing(client, migrated_engine):
    """The double-submit. The state check fires before the UNIQUE(batch_id)
    backstop, so the second attempt is `invalid_transition`."""
    batch_id = _packaged_batch(client, migrated_engine, "+254700050003")
    before = _audit_count(migrated_engine)

    r = client.post(
        f"/v2/batches/{batch_id}/package",
        json=PACKAGE,
        headers=_operator(migrated_engine, "op-pack-twice"),
    )

    assert r.status_code == 409
    assert r.json()["code"] == "invalid_transition"
    with Session(migrated_engine) as s:
        assert s.execute(select(func.count()).select_from(PackagingRecord)).scalar_one() == 1
    assert _audit_count(migrated_engine) == before


def test_a_replayed_package_returns_the_stored_response(client, migrated_engine):
    batch_id = _lab_verified_batch(client, migrated_engine, "+254700050004")
    headers = {**_operator(migrated_engine, "op-pack-idem"), "Idempotency-Key": "package-1"}
    before = _audit_count(migrated_engine)

    first = client.post(f"/v2/batches/{batch_id}/package", json=PACKAGE, headers=headers)
    second = client.post(f"/v2/batches/{batch_id}/package", json=PACKAGE, headers=headers)

    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json() == second.json()
    assert _audit_count(migrated_engine) == before + 1


@pytest.mark.parametrize(
    ("role", "phone"),
    [
        (Role.farmer, "+254700050101"),
        (Role.lab_officer, "+254700050102"),
        (Role.field_officer, "+254700050103"),
    ],
)
def test_only_staff_operators_may_package(client, migrated_engine, role, phone):
    batch_id = _lab_verified_batch(client, migrated_engine, phone)
    actor = seed_user(migrated_engine, role, f"pack-{role.value}")
    before = _audit_count(migrated_engine)

    r = client.post(f"/v2/batches/{batch_id}/package", json=PACKAGE, headers=auth(actor, role))

    assert r.status_code == 403
    assert _audit_count(migrated_engine) == before


def test_an_admin_may_package(client, migrated_engine):
    batch_id = _lab_verified_batch(client, migrated_engine, "+254700050005")
    admin = seed_user(migrated_engine, Role.admin, "admin-pack")

    r = client.post(
        f"/v2/batches/{batch_id}/package", json=PACKAGE, headers=auth(admin, Role.admin)
    )

    assert r.status_code == 201, r.text


@pytest.mark.parametrize(
    ("patch", "phone"),
    [
        ({"notes": "bad\x00note"}, "+254700050201"),
        ({"jar_ids": ["J-1", "J\x002"]}, "+254700050202"),
        ({"unit_count": -1}, "+254700050203"),
    ],
)
def test_bad_packaging_input_is_rejected_at_the_boundary(client, migrated_engine, patch, phone):
    batch_id = _lab_verified_batch(client, migrated_engine, phone)
    before = _audit_count(migrated_engine)

    r = client.post(
        f"/v2/batches/{batch_id}/package",
        json={**PACKAGE, **patch},
        headers=_operator(migrated_engine, f"op-pack-bad-{next(iter(patch))}"),
    )

    assert r.status_code == 422
    assert _audit_count(migrated_engine) == before


# --- S5 distribute ---------------------------------------------------------


def test_distribute_advances_the_state_and_anchors_the_record(client, migrated_engine):
    batch_id = _packaged_batch(client, migrated_engine, "+254700050006")
    before = _audit_count(migrated_engine)

    r = client.post(
        f"/v2/batches/{batch_id}/distribute",
        json=DISTRIBUTE,
        headers=_operator(migrated_engine, "op-dist"),
    )

    assert r.status_code == 201, r.text
    assert r.json()["state"] == "DISTRIBUTED"
    with Session(migrated_engine) as s:
        assert s.get(HoneyBatch, batch_id).state is BatchState.DISTRIBUTED
        record = s.execute(
            select(DistributionRecord).where(DistributionRecord.batch_id == batch_id)
        ).scalar_one()
        assert record.retailer_name == "Naivas Westlands"

        rows = s.execute(select(AuditLog).order_by(AuditLog.id)).scalars().all()
        assert len(rows) == before + 1
        row = rows[-1]
        assert row.action == "batch.distributed"
        assert row.subject_id == str(batch_id)
        assert row.payload_hash == compute_data_hash(stage_payloads.distribution_record(record))


def test_distributing_before_packaging_is_refused_and_writes_nothing(client, migrated_engine):
    batch_id = _lab_verified_batch(client, migrated_engine, "+254700050007")
    before = _audit_count(migrated_engine)

    r = client.post(
        f"/v2/batches/{batch_id}/distribute",
        json=DISTRIBUTE,
        headers=_operator(migrated_engine, "op-dist-early"),
    )

    assert r.status_code == 409
    assert r.json()["details"] == {
        "current_state": "LAB_VERIFIED",
        "attempted_state": "DISTRIBUTED",
    }
    with Session(migrated_engine) as s:
        assert s.execute(select(func.count()).select_from(DistributionRecord)).scalar_one() == 0
    assert _audit_count(migrated_engine) == before


def test_a_replayed_distribute_returns_the_stored_response(client, migrated_engine):
    batch_id = _packaged_batch(client, migrated_engine, "+254700050008")
    headers = {**_operator(migrated_engine, "op-dist-idem"), "Idempotency-Key": "distribute-1"}
    before = _audit_count(migrated_engine)

    first = client.post(f"/v2/batches/{batch_id}/distribute", json=DISTRIBUTE, headers=headers)
    second = client.post(f"/v2/batches/{batch_id}/distribute", json=DISTRIBUTE, headers=headers)

    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json() == second.json()
    assert _audit_count(migrated_engine) == before + 1


@pytest.mark.parametrize(
    ("role", "phone"),
    [
        (Role.farmer, "+254700050301"),
        (Role.lab_officer, "+254700050302"),
        (Role.field_officer, "+254700050303"),
    ],
)
def test_only_staff_operators_may_distribute(client, migrated_engine, role, phone):
    batch_id = _packaged_batch(client, migrated_engine, phone)
    actor = seed_user(migrated_engine, role, f"dist-{role.value}")
    before = _audit_count(migrated_engine)

    r = client.post(
        f"/v2/batches/{batch_id}/distribute", json=DISTRIBUTE, headers=auth(actor, role)
    )

    assert r.status_code == 403
    assert _audit_count(migrated_engine) == before


@pytest.mark.parametrize(
    ("field", "phone"),
    [
        ("retailer_name", "+254700050401"),
        ("transport_reference", "+254700050402"),
        ("handover_notes", "+254700050403"),
    ],
)
def test_a_nul_byte_in_distribution_text_is_rejected(client, migrated_engine, field, phone):
    batch_id = _packaged_batch(client, migrated_engine, phone)
    before = _audit_count(migrated_engine)

    r = client.post(
        f"/v2/batches/{batch_id}/distribute",
        json={**DISTRIBUTE, field: "bad\x00text"},
        headers=_operator(migrated_engine, f"op-dist-nul-{field}"),
    )

    assert r.status_code == 422
    assert _audit_count(migrated_engine) == before


def test_retailer_name_is_required(client, migrated_engine):
    batch_id = _packaged_batch(client, migrated_engine, "+254700050009")

    r = client.post(
        f"/v2/batches/{batch_id}/distribute",
        json={k: v for k, v in DISTRIBUTE.items() if k != "retailer_name"},
        headers=_operator(migrated_engine, "op-dist-noretailer"),
    )

    assert r.status_code == 422


# --- shared ----------------------------------------------------------------


@pytest.mark.parametrize(("stage", "body"), [("package", PACKAGE), ("distribute", DISTRIBUTE)])
def test_an_unknown_batch_is_404(client, migrated_engine, stage, body):
    r = client.post(
        f"/v2/batches/999999/{stage}",
        json=body,
        headers=_operator(migrated_engine, f"op-404-{stage}"),
    )

    assert r.status_code == 404
    assert r.json()["code"] == "batch_not_found"


@pytest.mark.parametrize(("stage", "body"), [("package", PACKAGE), ("distribute", DISTRIBUTE)])
def test_an_out_of_range_batch_id_is_invalid_input(client, migrated_engine, stage, body):
    r = client.post(
        f"/v2/batches/2147483648/{stage}",
        json=body,
        headers=_operator(migrated_engine, f"op-range-{stage}"),
    )

    assert r.status_code == 422


@pytest.mark.parametrize(
    ("stage", "body", "phone"),
    [("package", PACKAGE, "+254700050601"), ("distribute", DISTRIBUTE, "+254700050602")],
)
def test_an_unauthenticated_transition_is_401(client, migrated_engine, stage, body, phone):
    batch_id = _lab_verified_batch(client, migrated_engine, phone)

    r = client.post(f"/v2/batches/{batch_id}/{stage}", json=body)

    assert r.status_code == 401


# --- the terminal state ----------------------------------------------------


def test_distributed_is_terminal_at_every_endpoint(client, migrated_engine):
    """No successor for DISTRIBUTED, and no endpoint may invent one — including
    a second `distribute`. Every refusal names the state the batch is in and
    consumes no audit id."""
    batch_id = _distributed_batch(client, migrated_engine, "+254700050010")
    admin = auth(seed_user(migrated_engine, Role.admin, "admin-terminal"), Role.admin)
    before = _audit_count(migrated_engine)

    attempts = [
        ("harvest", HARVEST, "HARVESTED"),
        ("process", PROCESS, "PROCESSED"),
        ("lab-verify", CLEAN_PANEL, "LAB_VERIFIED"),
        ("package", PACKAGE, "PACKAGED"),
        ("distribute", DISTRIBUTE, "DISTRIBUTED"),
    ]
    for stage, body, attempted in attempts:
        r = client.post(f"/v2/batches/{batch_id}/{stage}", json=body, headers=admin)
        assert r.status_code == 409, (stage, r.text)
        assert r.json()["code"] == "invalid_transition"
        assert r.json()["details"] == {
            "current_state": "DISTRIBUTED",
            "attempted_state": attempted,
        }

    with Session(migrated_engine) as s:
        assert s.get(HoneyBatch, batch_id).state is BatchState.DISTRIBUTED
    assert _audit_count(migrated_engine) == before, "a refused request consumed an audit id"


def test_the_audit_chain_verifies_after_the_full_lifecycle(client, migrated_engine):
    from app.services import audit_log

    _distributed_batch(client, migrated_engine, "+254700050011")

    with Session(migrated_engine) as s:
        assert audit_log.verify_chain(s).ok
