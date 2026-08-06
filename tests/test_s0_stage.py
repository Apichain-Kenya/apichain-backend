"""S0: apiaries, and the two stage records a batch is born with (P3-E, 10 §8).

`CREATED` was a header-only state through Phases 1 and 2 — a batch row and one
audit line. `04` §5.2 carries two S0 pre-images forward from v1: where the
honey was produced (`apiary_records`) and what the farmer declared about it
(`batch_metadata`). With both, every state in the lifecycle has a canonical
payload behind it and `/verify` has no gap to explain.

Creating a batch therefore writes **three** audit rows, not one. They are three
distinct facts with three distinct payloads: merging them would make a tampered
declaration indistinguishable from a tampered location.
"""

import datetime as dt
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.enums import BatchState, Role
from app.models import ApiaryLocation, ApiaryRecord, AuditLog, BatchMetadata, HoneyBatch
from app.services import stage_payloads
from app.services.canonical import compute_data_hash
from tests.helpers import METADATA, auth, seed_apiary, seed_farmer, seed_user


def test_an_apiary_can_be_seeded_and_writes_an_audit_row(client, migrated_engine):
    officer = seed_user(migrated_engine, Role.field_officer, "officer-s0")
    farmer_id = seed_farmer(migrated_engine, "+254700020001")

    r = client.post(
        "/v2/apiaries",
        json={
            "farmer_id": farmer_id,
            "name": "Ridge apiary",
            "latitude": "-1.286389",
            "longitude": "36.817223",
            "altitude": "1795.00",
            "vegetation_type": "acacia_woodland",
            "hive_count": 12,
        },
        headers=auth(officer, Role.field_officer),
    )

    assert r.status_code == 201, r.text
    body = r.json()
    assert body["farmer_id"] == farmer_id
    assert body["name"] == "Ridge apiary"

    with Session(migrated_engine) as s:
        assert s.get(ApiaryLocation, body["id"]) is not None
        actions = s.execute(select(AuditLog.action)).scalars().all()
        assert actions == ["apiary.created"]


def test_a_farmer_may_not_seed_an_apiary_for_someone_else(client, migrated_engine):
    """The ownership check v1 omitted on farm-details, which is still a live
    IDOR in the deployed v1 system."""
    mine = seed_farmer(migrated_engine, "+254700020002")
    theirs = seed_farmer(migrated_engine, "+254700020003")
    farmer_user = seed_user(migrated_engine, Role.farmer, "farmer-s0", farmer_id=mine)

    r = client.post(
        "/v2/apiaries",
        json={"farmer_id": theirs, "name": "Not mine", "latitude": "0", "longitude": "0"},
        headers=auth(farmer_user, Role.farmer),
    )

    assert r.status_code == 403
    assert r.json()["code"] == "forbidden"
    with Session(migrated_engine) as s:
        assert s.execute(select(func.count()).select_from(ApiaryLocation)).scalar_one() == 0


def test_an_operator_may_not_seed_an_apiary(client, migrated_engine):
    operator = seed_user(migrated_engine, Role.operator, "op-s0")
    farmer_id = seed_farmer(migrated_engine, "+254700020004")

    r = client.post(
        "/v2/apiaries",
        json={"farmer_id": farmer_id, "name": "X", "latitude": "0", "longitude": "0"},
        headers=auth(operator, Role.operator),
    )

    assert r.status_code == 403


def test_an_out_of_range_coordinate_is_invalid_input(client, migrated_engine):
    officer = seed_user(migrated_engine, Role.field_officer, "officer-coord")
    farmer_id = seed_farmer(migrated_engine, "+254700020005")

    r = client.post(
        "/v2/apiaries",
        json={"farmer_id": farmer_id, "name": "X", "latitude": "91.0", "longitude": "0"},
        headers=auth(officer, Role.field_officer),
    )

    assert r.status_code == 422


def test_creating_a_batch_writes_both_s0_stage_records(client, migrated_engine):
    farmer_id = seed_farmer(migrated_engine, "+254700020010")
    apiary_id = seed_apiary(migrated_engine, farmer_id)
    operator = seed_user(migrated_engine, Role.operator, "op-create")

    r = client.post(
        "/v2/batches",
        json={"farmer_id": farmer_id, "apiary_id": apiary_id, "metadata": METADATA},
        headers=auth(operator, Role.operator),
    )

    assert r.status_code == 201, r.text
    batch_id = r.json()["id"]

    with Session(migrated_engine) as s:
        apiary_record = s.execute(
            select(ApiaryRecord).where(ApiaryRecord.batch_id == batch_id)
        ).scalar_one()
        metadata = s.execute(
            select(BatchMetadata).where(BatchMetadata.batch_id == batch_id)
        ).scalar_one()

        assert apiary_record.apiary_id == apiary_id
        assert metadata.honey_type == METADATA["honey_type"]
        assert s.get(HoneyBatch, batch_id).state is BatchState.CREATED


def test_creating_a_batch_writes_three_distinct_audit_rows(client, migrated_engine):
    """One per fact. Merging them would make a tampered declaration
    indistinguishable from a tampered location."""
    farmer_id = seed_farmer(migrated_engine, "+254700020011")
    apiary_id = seed_apiary(migrated_engine, farmer_id)
    operator = seed_user(migrated_engine, Role.operator, "op-audit")

    r = client.post(
        "/v2/batches",
        json={"farmer_id": farmer_id, "apiary_id": apiary_id, "metadata": METADATA},
        headers=auth(operator, Role.operator),
    )
    assert r.status_code == 201, r.text

    with Session(migrated_engine) as s:
        rows = s.execute(select(AuditLog).order_by(AuditLog.id)).scalars().all()

        assert [row.action for row in rows] == [
            "batch.created",
            "batch.apiary_recorded",
            "batch.metadata_recorded",
        ]
        assert all(row.actor_role == Role.operator for row in rows)


def test_the_s0_audit_payload_hashes_match_the_canonical_builders(client, migrated_engine):
    """What was recorded at write time must be what /verify recomputes."""
    farmer_id = seed_farmer(migrated_engine, "+254700020012")
    apiary_id = seed_apiary(migrated_engine, farmer_id)
    operator = seed_user(migrated_engine, Role.operator, "op-hash")

    r = client.post(
        "/v2/batches",
        json={"farmer_id": farmer_id, "apiary_id": apiary_id, "metadata": METADATA},
        headers=auth(operator, Role.operator),
    )
    batch_id = r.json()["id"]

    with Session(migrated_engine) as s:
        recorded = {
            row.action: row.payload_hash for row in s.execute(select(AuditLog)).scalars().all()
        }
        apiary_record = s.execute(
            select(ApiaryRecord).where(ApiaryRecord.batch_id == batch_id)
        ).scalar_one()
        metadata = s.execute(
            select(BatchMetadata).where(BatchMetadata.batch_id == batch_id)
        ).scalar_one()

        assert recorded["batch.apiary_recorded"] == compute_data_hash(
            stage_payloads.apiary_record(apiary_record)
        )
        assert recorded["batch.metadata_recorded"] == compute_data_hash(
            stage_payloads.batch_metadata(metadata)
        )


def test_the_apiary_record_snapshots_rather_than_referencing(client, migrated_engine):
    farmer_id = seed_farmer(migrated_engine, "+254700020013")
    apiary_id = seed_apiary(migrated_engine, farmer_id)
    operator = seed_user(migrated_engine, Role.operator, "op-snap")

    r = client.post(
        "/v2/batches",
        json={"farmer_id": farmer_id, "apiary_id": apiary_id, "metadata": METADATA},
        headers=auth(operator, Role.operator),
    )
    batch_id = r.json()["id"]

    with Session(migrated_engine) as s:
        apiary = s.get(ApiaryLocation, apiary_id)
        apiary.latitude = Decimal("9.999999")
        s.commit()

    with Session(migrated_engine) as s:
        record = s.execute(
            select(ApiaryRecord).where(ApiaryRecord.batch_id == batch_id)
        ).scalar_one()
        assert record.latitude != Decimal("9.999999")


def test_a_batch_for_an_unknown_apiary_writes_nothing(client, migrated_engine):
    farmer_id = seed_farmer(migrated_engine, "+254700020014")
    operator = seed_user(migrated_engine, Role.operator, "op-noapiary")

    r = client.post(
        "/v2/batches",
        json={"farmer_id": farmer_id, "apiary_id": 999_999, "metadata": METADATA},
        headers=auth(operator, Role.operator),
    )

    assert r.status_code == 404
    assert r.json()["code"] == "apiary_not_found"
    with Session(migrated_engine) as s:
        assert s.execute(select(func.count()).select_from(HoneyBatch)).scalar_one() == 0
        assert s.execute(select(func.count()).select_from(AuditLog)).scalar_one() == 0


def test_an_apiary_belonging_to_another_farmer_is_refused(client, migrated_engine):
    """The batch's provenance claim must not point at someone else's hives."""
    farmer_id = seed_farmer(migrated_engine, "+254700020015")
    other_id = seed_farmer(migrated_engine, "+254700020016")
    other_apiary = seed_apiary(migrated_engine, other_id)
    operator = seed_user(migrated_engine, Role.operator, "op-mismatch")

    r = client.post(
        "/v2/batches",
        json={"farmer_id": farmer_id, "apiary_id": other_apiary, "metadata": METADATA},
        headers=auth(operator, Role.operator),
    )

    assert r.status_code == 409
    assert r.json()["code"] == "apiary_farmer_mismatch"


def test_the_old_farmer_id_only_request_shape_is_now_invalid(client, migrated_engine):
    """The break is intentional: 04 §5.4 and v1's Sprint 9 lesson both refuse a
    dual typed/untyped path, so there is no grace period."""
    farmer_id = seed_farmer(migrated_engine, "+254700020017")
    operator = seed_user(migrated_engine, Role.operator, "op-old")

    r = client.post(
        "/v2/batches", json={"farmer_id": farmer_id}, headers=auth(operator, Role.operator)
    )

    assert r.status_code == 422


def test_a_harvest_window_that_ends_before_it_starts_is_refused(client, migrated_engine):
    farmer_id = seed_farmer(migrated_engine, "+254700020018")
    apiary_id = seed_apiary(migrated_engine, farmer_id)
    operator = seed_user(migrated_engine, Role.operator, "op-window")

    bad = {**METADATA, "harvest_window_start": "2026-04-01", "harvest_window_end": "2026-03-01"}
    r = client.post(
        "/v2/batches",
        json={"farmer_id": farmer_id, "apiary_id": apiary_id, "metadata": bad},
        headers=auth(operator, Role.operator),
    )

    assert r.status_code == 422


def test_an_unknown_honey_type_is_refused_at_the_boundary(client, migrated_engine):
    farmer_id = seed_farmer(migrated_engine, "+254700020019")
    apiary_id = seed_apiary(migrated_engine, farmer_id)
    operator = seed_user(migrated_engine, Role.operator, "op-type")

    bad = {**METADATA, "honey_type": "unobtainium"}
    r = client.post(
        "/v2/batches",
        json={"farmer_id": farmer_id, "apiary_id": apiary_id, "metadata": bad},
        headers=auth(operator, Role.operator),
    )

    assert r.status_code == 422


def test_metadata_notes_persist_but_do_not_change_the_anchored_hash(client, migrated_engine):
    farmer_id = seed_farmer(migrated_engine, "+254700020020")
    apiary_id = seed_apiary(migrated_engine, farmer_id)
    operator = seed_user(migrated_engine, Role.operator, "op-notes")

    r = client.post(
        "/v2/batches",
        json={
            "farmer_id": farmer_id,
            "apiary_id": apiary_id,
            "metadata": {**METADATA, "notes": "left of the river"},
        },
        headers=auth(operator, Role.operator),
    )
    batch_id = r.json()["id"]

    with Session(migrated_engine) as s:
        metadata = s.execute(
            select(BatchMetadata).where(BatchMetadata.batch_id == batch_id)
        ).scalar_one()

        assert metadata.notes == "left of the river"
        assert "notes" not in stage_payloads.batch_metadata(metadata)


def test_a_replayed_creation_does_not_write_a_second_set_of_records(client, migrated_engine):
    farmer_id = seed_farmer(migrated_engine, "+254700020021")
    apiary_id = seed_apiary(migrated_engine, farmer_id)
    operator = seed_user(migrated_engine, Role.operator, "op-replay")
    headers = {**auth(operator, Role.operator), "Idempotency-Key": "s0-replay"}
    body = {"farmer_id": farmer_id, "apiary_id": apiary_id, "metadata": METADATA}

    first = client.post("/v2/batches", json=body, headers=headers)
    second = client.post("/v2/batches", json=body, headers=headers)

    assert first.json() == second.json()
    with Session(migrated_engine) as s:
        assert s.execute(select(func.count()).select_from(HoneyBatch)).scalar_one() == 1
        assert s.execute(select(func.count()).select_from(ApiaryRecord)).scalar_one() == 1
        assert s.execute(select(func.count()).select_from(AuditLog)).scalar_one() == 3


def test_the_declared_harvest_window_is_hashed_as_plain_dates(client, migrated_engine):
    """Dates carry no timezone; hashing them as `YYYY-MM-DD` keeps the payload
    independent of how the client serialized them."""
    farmer_id = seed_farmer(migrated_engine, "+254700020022")
    apiary_id = seed_apiary(migrated_engine, farmer_id)
    operator = seed_user(migrated_engine, Role.operator, "op-dates")

    r = client.post(
        "/v2/batches",
        json={"farmer_id": farmer_id, "apiary_id": apiary_id, "metadata": METADATA},
        headers=auth(operator, Role.operator),
    )
    batch_id = r.json()["id"]

    with Session(migrated_engine) as s:
        metadata = s.execute(
            select(BatchMetadata).where(BatchMetadata.batch_id == batch_id)
        ).scalar_one()
        payload = stage_payloads.batch_metadata(metadata)

        assert payload["harvest_window_start"] == METADATA["harvest_window_start"]
        assert metadata.harvest_window_start == dt.date.fromisoformat(
            METADATA["harvest_window_start"]
        )
