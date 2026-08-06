"""The seven canonical stage payloads (P3-D, 10 §7).

Each builder turns a stage row into the exact dict that gets hashed into the
audit chain and, through Phase 2, anchored to Bitcoin. These are ports of v1's
`_*_canonical_payload` helpers in `routers/batch.py`, which is where v1's real
work lived.

Determinism is the whole point. A payload that hashes one way on write and
another way after a database round-trip breaks verification silently — v1's
Sprint 6 watchlist records exactly that failure mode with `DateTime` columns.
So every test here round-trips through the DB before hashing, rather than
hashing an in-memory object the way a unit test naturally would.

Two conventions the tests pin:

- **Server bookkeeping timestamps stay out.** `recorded_at`/`evaluated_at` are
  when *we* wrote the row; the audit row's own `created_at` already records and
  anchors that. Actor-asserted dates (`harvest_date`, `tested_at`, the harvest
  window) are facts about the honey and are hashed. `batch_metadata.recorded_at`
  is the one exception, carried from v1 for parity.
- **Numbers are fixed-precision strings**, never floats, so the hash cannot
  depend on repr.
"""

import datetime as dt
from decimal import Decimal

import pytest

from app.enums import BatchState, Role
from app.models import (
    ApiaryLocation,
    ApiaryRecord,
    BatchMetadata,
    DistributionRecord,
    Farmer,
    HarvestRecord,
    HoneyBatch,
    LabResult,
    PackagingRecord,
    ProcessRecord,
    User,
)
from app.services import stage_payloads
from app.services.canonical import compute_data_hash


@pytest.fixture
def batch(db):
    user = User(username="op-payload", password_hash="x", role=Role.operator)
    db.add(user)
    db.flush()
    farmer = Farmer(first_name="A", last_name="B", phone="+254700009999", user_id=user.id)
    db.add(farmer)
    db.flush()
    row = HoneyBatch(farmer_id=farmer.id, batch_code="B-PAYLOAD01", state=BatchState.CREATED)
    apiary = ApiaryLocation(
        farmer_id=farmer.id,
        name="Ridge",
        latitude=Decimal("-1.286389"),
        longitude=Decimal("36.817223"),
    )
    db.add_all([row, apiary])
    db.flush()
    row.apiary_id_for_test = apiary.id  # type: ignore[attr-defined]
    return row


def _roundtrip(db, row):
    """Persist, expire, reload — so the payload is built from what the DB
    actually returns, not from the Python objects we happened to construct."""
    db.add(row)
    db.flush()
    db.expire(row)
    db.refresh(row)
    return row


def test_apiary_payload_matches_the_v1_field_set(db, batch):
    row = _roundtrip(
        db,
        ApiaryRecord(
            batch_id=batch.id,
            apiary_id=batch.apiary_id_for_test,
            latitude=Decimal("-1.286389"),
            longitude=Decimal("36.817223"),
            altitude=Decimal("1795.00"),
            vegetation_type="acacia_woodland",
            hive_count=12,
        ),
    )

    assert stage_payloads.apiary_record(row) == {
        "batch_id": batch.id,
        "apiary_id": batch.apiary_id_for_test,
        "latitude": "-1.286389",
        "longitude": "36.817223",
        "altitude": "1795.00",
        "vegetation_type": "acacia_woodland",
        "hive_count": 12,
    }


def test_metadata_payload_lowercases_enums_and_fixes_numeric_precision(db, batch):
    row = _roundtrip(
        db,
        BatchMetadata(
            batch_id=batch.id,
            honey_type="Acacia",
            expected_yield_kg=Decimal("50"),
            harvest_window_start=dt.date(2026, 3, 1),
            harvest_window_end=dt.date(2026, 4, 1),
            apiary_management_method="Langstroth",
            notes="a typo to be fixed later",
        ),
    )

    payload = stage_payloads.batch_metadata(row)

    assert payload["honey_type"] == "acacia"
    assert payload["apiary_management_method"] == "langstroth"
    assert payload["expected_yield_kg"] == "50.00"
    assert payload["harvest_window_start"] == "2026-03-01"
    assert payload["harvest_window_end"] == "2026-04-01"


def test_metadata_notes_are_stored_but_never_hashed(db, batch):
    """v1 Sprint 8's rule: correcting a typo must not invalidate anchored
    history. `notes` is displayed by /verify and excluded from the hash."""
    row = _roundtrip(
        db,
        BatchMetadata(
            batch_id=batch.id,
            honey_type="acacia",
            expected_yield_kg=Decimal("50.00"),
            harvest_window_start=dt.date(2026, 3, 1),
            harvest_window_end=dt.date(2026, 4, 1),
            apiary_management_method="langstroth",
            notes="before",
        ),
    )
    before = compute_data_hash(stage_payloads.batch_metadata(row))

    row.notes = "after"
    db.flush()
    db.refresh(row)

    assert "notes" not in stage_payloads.batch_metadata(row)
    assert compute_data_hash(stage_payloads.batch_metadata(row)) == before


def test_harvest_payload_matches_the_v1_field_set(db, batch):
    row = _roundtrip(
        db,
        HarvestRecord(
            batch_id=batch.id,
            harvest_date=dt.datetime(2026, 3, 15, 9, 30),
            quantity_kg=Decimal("42.5"),
            hive_ids=["H1", "H2"],
            gps_lat=Decimal("-1.286389"),
            gps_lon=Decimal("36.817223"),
            notes="morning harvest",
        ),
    )

    assert stage_payloads.harvest_record(row) == {
        "batch_id": batch.id,
        "harvest_date": "2026-03-15T09:30:00",
        "quantity_kg": "42.50",
        "hive_ids": ["H1", "H2"],
        "gps_lat": "-1.286389",
        "gps_lon": "36.817223",
        "notes": "morning harvest",
    }


def test_process_payload_matches_the_v1_field_set(db, batch):
    row = _roundtrip(
        db,
        ProcessRecord(
            batch_id=batch.id,
            extraction_method="centrifugal",
            moisture_content=Decimal("18.2"),
            handling_notes="strained twice",
        ),
    )

    assert stage_payloads.process_record(row) == {
        "batch_id": batch.id,
        "extraction_method": "centrifugal",
        "moisture_content": "18.20",
        "handling_notes": "strained twice",
    }


def test_lab_payload_names_every_measurement_with_its_unit(db, batch):
    row = _roundtrip(
        db,
        LabResult(
            batch_id=batch.id,
            moisture_pct=Decimal("18.4"),
            fructose_glucose_g_100g=Decimal("72.1"),
            sucrose_g_100g=Decimal("2.3"),
            hmf_mg_kg=Decimal("21"),
            diastase_schade=Decimal("14.2"),
            free_acidity_meq_kg=Decimal("28"),
            pollen_density=Decimal("64.5"),
            laboratory_name="KEBS Nairobi",
            analyst_name="J. Wanjiru",
            certificate_number="KEBS-2026-0042",
            notes="clear sample",
            tested_at=dt.datetime(2026, 4, 2, 11, 0),
        ),
    )

    assert stage_payloads.lab_result(row) == {
        "batch_id": batch.id,
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
        "notes": "clear sample",
        "tested_at": "2026-04-02T11:00:00",
    }


def test_lab_payload_carries_no_retired_v1_field(db, batch):
    row = _roundtrip(db, LabResult(batch_id=batch.id, moisture_pct=Decimal("18.4")))

    payload = stage_payloads.lab_result(row)

    assert "sucrose_level" not in payload
    assert not {k for k in payload if k.startswith("predicted_")}
    assert "authenticity_score" not in payload


def test_packaging_payload_matches_the_v1_field_set(db, batch):
    row = _roundtrip(
        db,
        PackagingRecord(batch_id=batch.id, unit_count=120, jar_ids=["J1", "J2"], notes="500g jars"),
    )

    assert stage_payloads.packaging_record(row) == {
        "batch_id": batch.id,
        "unit_count": 120,
        "jar_ids": ["J1", "J2"],
        "notes": "500g jars",
    }


def test_distribution_payload_matches_the_v1_field_set(db, batch):
    row = _roundtrip(
        db,
        DistributionRecord(
            batch_id=batch.id,
            retailer_name="Karen Provision",
            transport_reference="TRK-9",
            handover_notes="cool box",
        ),
    )

    assert stage_payloads.distribution_record(row) == {
        "batch_id": batch.id,
        "retailer_name": "Karen Provision",
        "transport_reference": "TRK-9",
        "handover_notes": "cool box",
    }


def test_no_payload_carries_a_server_bookkeeping_timestamp(db, batch):
    """`recorded_at` is when we wrote the row; the audit row's created_at
    already records and anchors that. batch_metadata keeps it for v1 parity."""
    rows = {
        stage_payloads.apiary_record: ApiaryRecord(
            batch_id=batch.id,
            apiary_id=batch.apiary_id_for_test,
            latitude=Decimal("-1.28"),
            longitude=Decimal("36.81"),
        ),
        stage_payloads.harvest_record: HarvestRecord(
            batch_id=batch.id,
            harvest_date=dt.datetime(2026, 3, 15, 9, 30),
            quantity_kg=Decimal("42.50"),
            hive_ids=[],
        ),
        stage_payloads.process_record: ProcessRecord(
            batch_id=batch.id, extraction_method="centrifugal"
        ),
        stage_payloads.lab_result: LabResult(batch_id=batch.id),
        stage_payloads.packaging_record: PackagingRecord(
            batch_id=batch.id, unit_count=1, jar_ids=[]
        ),
        stage_payloads.distribution_record: DistributionRecord(
            batch_id=batch.id, retailer_name="R"
        ),
    }
    for builder, row in rows.items():
        _roundtrip(db, row)
        assert "recorded_at" not in builder(row), builder.__name__


def test_a_tz_aware_and_a_naive_datetime_hash_identically(db, batch):
    """The Sprint 6 watchlist failure: a DateTime that bypasses canonical_dt
    hashes one way on write and another after the psycopg round-trip."""
    aware = _roundtrip(
        db,
        HarvestRecord(
            batch_id=batch.id,
            harvest_date=dt.datetime(2026, 3, 15, 9, 30, tzinfo=dt.UTC),
            quantity_kg=Decimal("42.50"),
            hive_ids=[],
        ),
    )
    from_aware = compute_data_hash(stage_payloads.harvest_record(aware))

    aware.harvest_date = dt.datetime(2026, 3, 15, 9, 30)
    db.flush()
    db.refresh(aware)

    assert compute_data_hash(stage_payloads.harvest_record(aware)) == from_aware


def test_an_absent_optional_value_is_null_not_a_missing_key(db, batch):
    """A dropped key changes the hash. Absent optionals must serialize as None."""
    row = _roundtrip(
        db,
        ApiaryRecord(
            batch_id=batch.id,
            apiary_id=batch.apiary_id_for_test,
            latitude=Decimal("-1.28"),
            longitude=Decimal("36.81"),
        ),
    )

    payload = stage_payloads.apiary_record(row)

    assert payload["altitude"] is None
    assert payload["vegetation_type"] is None
    assert payload["hive_count"] is None


def test_every_builder_is_registered_for_its_stage():
    """The /verify endpoint walks this registry; a stage missing from it would
    silently drop out of the three-way match."""
    assert set(stage_payloads.BUILDERS) == {
        "apiary",
        "metadata",
        "harvest",
        "process",
        "lab",
        "packaging",
        "distribution",
    }
