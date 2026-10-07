"""S1 harvest and S2 process, the first two transitions (P3-F, 10 §9).

All five transitions share one handler order, so what is tested here is mostly
the shape rather than the stage:

    idempotency.begin -> resolve/authorize -> validate -> SELECT ... FOR UPDATE
    -> legal-transition check -> insert stage row -> flush (409 on conflict)
    -> advance state -> audit_log.append -> finish -> commit

The two properties worth stating plainly, because both are invisible in a
passing happy path:

- **Nothing fallible runs after `append()`.** A rollback past that point burns
  an audit id permanently, since Postgres sequences are not transactional, and
  Phase 2 can only skip the gap it leaves. So every refusal below asserts that
  no audit row was written at all — a refused request must not consume an id.
- **A refused transition writes nothing anywhere.** No stage row, no state
  change, no audit row.
"""

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.enums import BatchState, Role
from app.models import AuditLog, HarvestRecord, HoneyBatch, ProcessRecord
from app.services import stage_payloads
from app.services.canonical import compute_data_hash
from tests.helpers import METADATA, auth, seed_apiary, seed_farmer, seed_user

HARVEST = {
    "harvest_date": "2026-03-15T09:30:00Z",
    "quantity_kg": "42.50",
    "hive_ids": ["H1", "H2"],
    "gps_lat": "-1.286389",
    "gps_lon": "36.817223",
    "notes": "morning harvest",
}
PROCESS = {
    "extraction_method": "centrifugal",
    "moisture_content": "18.20",
    "handling_notes": "strained twice",
}


def _batch(client, engine, phone: str) -> int:
    farmer_id = seed_farmer(engine, phone)
    apiary_id = seed_apiary(engine, farmer_id)
    operator = seed_user(engine, Role.operator, f"op{phone[-6:]}")
    r = client.post(
        "/v2/batches",
        json={"farmer_id": farmer_id, "apiary_id": apiary_id, "metadata": METADATA},
        headers=auth(operator, Role.operator),
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _audit_count(engine) -> int:
    with Session(engine) as s:
        return s.execute(select(func.count()).select_from(AuditLog)).scalar_one()


def test_harvest_advances_the_state_and_records_the_stage(client, migrated_engine):
    batch_id = _batch(client, migrated_engine, "+254700030001")
    operator = seed_user(migrated_engine, Role.operator, "op-harvest")

    r = client.post(
        f"/v2/batches/{batch_id}/harvest",
        json=HARVEST,
        headers=auth(operator, Role.operator),
    )

    assert r.status_code == 201, r.text
    assert r.json()["state"] == "HARVESTED"
    with Session(migrated_engine) as s:
        assert s.get(HoneyBatch, batch_id).state is BatchState.HARVESTED
        record = s.execute(
            select(HarvestRecord).where(HarvestRecord.batch_id == batch_id)
        ).scalar_one()
        assert [str(h) for h in record.hive_ids] == ["H1", "H2"]


def test_harvest_writes_exactly_one_audit_row_whose_hash_matches(client, migrated_engine):
    batch_id = _batch(client, migrated_engine, "+254700030002")
    operator = seed_user(migrated_engine, Role.operator, "op-harvest-hash")
    before = _audit_count(migrated_engine)

    client.post(
        f"/v2/batches/{batch_id}/harvest",
        json=HARVEST,
        headers=auth(operator, Role.operator),
    )

    with Session(migrated_engine) as s:
        rows = s.execute(select(AuditLog).order_by(AuditLog.id)).scalars().all()
        assert len(rows) == before + 1
        row = rows[-1]
        assert row.action == "batch.harvest_recorded"
        assert row.subject_type == "batch"
        assert row.subject_id == str(batch_id)

        record = s.execute(
            select(HarvestRecord).where(HarvestRecord.batch_id == batch_id)
        ).scalar_one()
        assert row.payload_hash == compute_data_hash(stage_payloads.harvest_record(record))


def test_a_tz_aware_harvest_date_is_stored_as_utc(client, migrated_engine):
    """The wire carries an offset; the column is naive. Without normalizing on
    the way in, two clients reporting the same instant store different ones."""
    batch_id = _batch(client, migrated_engine, "+254700030003")
    operator = seed_user(migrated_engine, Role.operator, "op-tz")

    client.post(
        f"/v2/batches/{batch_id}/harvest",
        json={**HARVEST, "harvest_date": "2026-03-15T12:30:00+03:00"},
        headers=auth(operator, Role.operator),
    )

    with Session(migrated_engine) as s:
        record = s.execute(
            select(HarvestRecord).where(HarvestRecord.batch_id == batch_id)
        ).scalar_one()
        assert stage_payloads.harvest_record(record)["harvest_date"] == "2026-03-15T09:30:00"


def test_process_follows_harvest(client, migrated_engine):
    batch_id = _batch(client, migrated_engine, "+254700030004")
    operator = seed_user(migrated_engine, Role.operator, "op-process")
    headers = auth(operator, Role.operator)

    client.post(f"/v2/batches/{batch_id}/harvest", json=HARVEST, headers=headers)
    r = client.post(f"/v2/batches/{batch_id}/process", json=PROCESS, headers=headers)

    assert r.status_code == 201, r.text
    assert r.json()["state"] == "PROCESSED"
    with Session(migrated_engine) as s:
        assert s.get(HoneyBatch, batch_id).state is BatchState.PROCESSED
        assert (
            s.execute(select(ProcessRecord).where(ProcessRecord.batch_id == batch_id)).scalar_one()
            is not None
        )


def test_processing_before_harvesting_is_refused_and_writes_nothing(client, migrated_engine):
    batch_id = _batch(client, migrated_engine, "+254700030005")
    operator = seed_user(migrated_engine, Role.operator, "op-skip")
    before = _audit_count(migrated_engine)

    r = client.post(
        f"/v2/batches/{batch_id}/process",
        json=PROCESS,
        headers=auth(operator, Role.operator),
    )

    assert r.status_code == 409
    body = r.json()
    assert body["code"] == "invalid_transition"
    assert body["details"] == {"current_state": "CREATED", "attempted_state": "PROCESSED"}
    with Session(migrated_engine) as s:
        assert s.get(HoneyBatch, batch_id).state is BatchState.CREATED
        assert s.execute(select(func.count()).select_from(ProcessRecord)).scalar_one() == 0
    assert _audit_count(migrated_engine) == before, "a refused request consumed an audit id"


def test_harvesting_twice_is_refused_and_writes_nothing(client, migrated_engine):
    """The double-submit. Refused before append(), so no audit id is burned."""
    batch_id = _batch(client, migrated_engine, "+254700030006")
    operator = seed_user(migrated_engine, Role.operator, "op-twice")
    headers = auth(operator, Role.operator)

    first = client.post(f"/v2/batches/{batch_id}/harvest", json=HARVEST, headers=headers)
    assert first.status_code == 201
    after_first = _audit_count(migrated_engine)

    second = client.post(f"/v2/batches/{batch_id}/harvest", json=HARVEST, headers=headers)

    assert second.status_code == 409
    assert second.json()["code"] == "invalid_transition"
    with Session(migrated_engine) as s:
        assert s.execute(select(func.count()).select_from(HarvestRecord)).scalar_one() == 1
    assert _audit_count(migrated_engine) == after_first


def test_a_replayed_harvest_returns_the_stored_response(client, migrated_engine):
    batch_id = _batch(client, migrated_engine, "+254700030007")
    operator = seed_user(migrated_engine, Role.operator, "op-idem")
    headers = {**auth(operator, Role.operator), "Idempotency-Key": "harvest-1"}
    before = _audit_count(migrated_engine)

    first = client.post(f"/v2/batches/{batch_id}/harvest", json=HARVEST, headers=headers)
    second = client.post(f"/v2/batches/{batch_id}/harvest", json=HARVEST, headers=headers)

    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json() == second.json()
    assert _audit_count(migrated_engine) == before + 1


def test_a_lab_officer_may_not_record_a_harvest(client, migrated_engine):
    batch_id = _batch(client, migrated_engine, "+254700030008")
    lab = seed_user(migrated_engine, Role.lab_officer, "lab-harvest")
    before = _audit_count(migrated_engine)

    r = client.post(
        f"/v2/batches/{batch_id}/harvest",
        json=HARVEST,
        headers=auth(lab, Role.lab_officer),
    )

    assert r.status_code == 403
    assert r.json()["code"] == "forbidden"
    assert _audit_count(migrated_engine) == before


def test_a_farmer_may_record_their_harvest(client, migrated_engine):
    """04 §5.3: a farmer self-registers their own harvest; the other four
    transitions are staff actions."""
    farmer_id = seed_farmer(migrated_engine, "+254700030009")
    apiary_id = seed_apiary(migrated_engine, farmer_id)
    farmer_user = seed_user(migrated_engine, Role.farmer, "farmer-h", farmer_id=farmer_id)
    headers = auth(farmer_user, Role.farmer)

    created = client.post(
        "/v2/batches",
        json={"farmer_id": farmer_id, "apiary_id": apiary_id, "metadata": METADATA},
        headers=headers,
    )
    r = client.post(f"/v2/batches/{created.json()['id']}/harvest", json=HARVEST, headers=headers)

    assert r.status_code == 201, r.text


def test_a_farmer_may_not_record_processing(client, migrated_engine):
    batch_id = _batch(client, migrated_engine, "+254700030010")
    farmer_user = seed_user(migrated_engine, Role.farmer, "farmer-p")
    operator = seed_user(migrated_engine, Role.operator, "op-for-farmer")

    client.post(
        f"/v2/batches/{batch_id}/harvest", json=HARVEST, headers=auth(operator, Role.operator)
    )
    r = client.post(
        f"/v2/batches/{batch_id}/process",
        json=PROCESS,
        headers=auth(farmer_user, Role.farmer),
    )

    assert r.status_code == 403


def test_a_transition_on_an_unknown_batch_is_404(client, migrated_engine):
    operator = seed_user(migrated_engine, Role.operator, "op-404")

    r = client.post(
        "/v2/batches/999999/harvest", json=HARVEST, headers=auth(operator, Role.operator)
    )

    assert r.status_code == 404
    assert r.json()["code"] == "batch_not_found"


def test_an_out_of_range_batch_id_is_invalid_input_not_a_driver_error(client, migrated_engine):
    """honey_batches.id is int32; an id past that reaches the driver and comes
    back as a 400 the schema never promised."""
    operator = seed_user(migrated_engine, Role.operator, "op-bounds")

    r = client.post(
        "/v2/batches/2147483648/harvest",
        json=HARVEST,
        headers=auth(operator, Role.operator),
    )

    assert r.status_code == 422


def test_a_nul_byte_in_notes_is_rejected_at_the_boundary(client, migrated_engine):
    batch_id = _batch(client, migrated_engine, "+254700030011")
    operator = seed_user(migrated_engine, Role.operator, "op-nul")

    r = client.post(
        f"/v2/batches/{batch_id}/harvest",
        json={**HARVEST, "notes": "bad\x00note"},
        headers=auth(operator, Role.operator),
    )

    assert r.status_code == 422


def test_a_negative_quantity_is_refused(client, migrated_engine):
    batch_id = _batch(client, migrated_engine, "+254700030012")
    operator = seed_user(migrated_engine, Role.operator, "op-neg")

    r = client.post(
        f"/v2/batches/{batch_id}/harvest",
        json={**HARVEST, "quantity_kg": "-1.00"},
        headers=auth(operator, Role.operator),
    )

    assert r.status_code == 422


def test_an_unauthenticated_transition_is_401(client, migrated_engine):
    batch_id = _batch(client, migrated_engine, "+254700030013")

    r = client.post(f"/v2/batches/{batch_id}/harvest", json=HARVEST)

    assert r.status_code == 401


def test_the_audit_chain_still_verifies_after_two_transitions(client, migrated_engine):
    from app.services import audit_log

    batch_id = _batch(client, migrated_engine, "+254700030014")
    operator = seed_user(migrated_engine, Role.operator, "op-chain")
    headers = auth(operator, Role.operator)

    client.post(f"/v2/batches/{batch_id}/harvest", json=HARVEST, headers=headers)
    client.post(f"/v2/batches/{batch_id}/process", json=PROCESS, headers=headers)

    with Session(migrated_engine) as s:
        assert audit_log.verify_chain(s).ok
