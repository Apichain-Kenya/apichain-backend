"""The per-stage record tables and their invariants (P3-C, 10 §6).

`04` §5.2 carries v1's `*_records` tables forward: one normalized row per
lifecycle stage, holding the canonical payload that gets hashed into the audit
chain. They look repetitive from a distance but share only three columns — the
batch link, the timestamp, and nothing else. Their payload columns are v1's
Sprint 5-8 work and are the reason `/verify` can say which stage was tampered
with rather than only that something was.

The invariant that matters most here is `UNIQUE(batch_id)`. It is the
database-level backstop that makes a stage unrepeatable: even if a handler's
state check were bypassed, a second harvest for one batch cannot be written.
The transition handlers rely on it to turn a double-submit into a 409 *before*
`audit_log.append()` runs, which is what keeps a burned audit id off the table.
"""

import datetime as dt
from decimal import Decimal

import pytest
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

from app.database import Base
from app.enums import BatchState, ConformanceVerdict, Role
from app.models import (
    ApiaryLocation,
    ApiaryRecord,
    BatchMetadata,
    CodexConformance,
    DistributionRecord,
    Farmer,
    HarvestRecord,
    HoneyBatch,
    LabResult,
    PackagingRecord,
    ProcessRecord,
    User,
)

_STAGE_TABLES = [
    "apiary_locations",
    "apiary_records",
    "batch_metadata",
    "harvest_records",
    "process_records",
    "lab_results",
    "packaging_records",
    "distribution_records",
    "codex_conformance",
]


def _seed_batch(db) -> tuple[int, int]:
    """A farmer, an apiary and a batch to hang stage records off."""
    user = User(username="op-stage", password_hash="x", role=Role.operator)
    db.add(user)
    db.flush()
    farmer = Farmer(first_name="A", last_name="B", phone=f"+2547{user.id:08d}", user_id=user.id)
    db.add(farmer)
    db.flush()
    apiary = ApiaryLocation(
        farmer_id=farmer.id,
        name="Ridge apiary",
        latitude=Decimal("-1.286389"),
        longitude=Decimal("36.817223"),
    )
    batch = HoneyBatch(farmer_id=farmer.id, batch_code="B-STAGE0001", state=BatchState.CREATED)
    db.add_all([apiary, batch])
    db.flush()
    return batch.id, apiary.id


def test_every_stage_table_is_created_by_the_migration(migrated_engine):
    present = set(inspect(migrated_engine).get_table_names())

    assert set(_STAGE_TABLES) <= present, f"missing: {sorted(set(_STAGE_TABLES) - present)}"


def test_the_truncate_list_covers_every_mapped_table():
    """A table missing from the list leaks rows, and only in a full run."""
    from tests.conftest import _ALL_TABLES

    listed = {name.strip() for name in _ALL_TABLES.split(",")}

    assert set(Base.metadata.tables) - listed == set(), "add the new table to BOTH _ALL_TABLES"


def test_the_contract_truncate_list_matches_conftest():
    from tests.conftest import _ALL_TABLES as spine
    from tests.test_contract import _ALL_TABLES as contract

    assert {n.strip() for n in spine.split(",")} == {n.strip() for n in contract.split(",")}


@pytest.mark.parametrize(
    ("model", "extra"),
    [
        (ApiaryRecord, {"latitude": Decimal("-1.28"), "longitude": Decimal("36.81")}),
        (
            BatchMetadata,
            {
                "honey_type": "acacia",
                "expected_yield_kg": Decimal("50.00"),
                "harvest_window_start": dt.date(2026, 3, 1),
                "harvest_window_end": dt.date(2026, 4, 1),
                "apiary_management_method": "langstroth",
            },
        ),
        (
            HarvestRecord,
            {
                "harvest_date": dt.datetime(2026, 3, 15, 9, 0),
                "quantity_kg": Decimal("42.50"),
                "hive_ids": ["H1", "H2"],
            },
        ),
        (ProcessRecord, {"extraction_method": "centrifugal"}),
        (LabResult, {"moisture_pct": Decimal("18.40")}),
        (PackagingRecord, {"unit_count": 120, "jar_ids": ["J1"]}),
        (DistributionRecord, {"retailer_name": "Nakumatt"}),
        (
            CodexConformance,
            {"rule_set_version": "codex-kenya-v1", "verdict": ConformanceVerdict.PASS},
        ),
    ],
)
def test_a_stage_may_be_recorded_only_once_per_batch(db, model, extra):
    batch_id, apiary_id = _seed_batch(db)
    if model is ApiaryRecord:
        extra = {**extra, "apiary_id": apiary_id}

    db.add(model(batch_id=batch_id, **extra))
    db.flush()

    db.add(model(batch_id=batch_id, **extra))
    with pytest.raises(IntegrityError):
        db.flush()


def test_lab_results_carries_the_six_codex_parameters_by_their_measured_names(db):
    """02 R9: a field named for one quantity must not hold another."""
    batch_id, _ = _seed_batch(db)
    row = LabResult(
        batch_id=batch_id,
        moisture_pct=Decimal("18.40"),
        fructose_glucose_g_100g=Decimal("72.10"),
        sucrose_g_100g=Decimal("2.30"),
        hmf_mg_kg=Decimal("21.00"),
        diastase_schade=Decimal("14.20"),
        free_acidity_meq_kg=Decimal("28.00"),
    )
    db.add(row)
    db.flush()

    assert row.sucrose_g_100g == Decimal("2.30")


def test_lab_results_does_not_carry_v1s_retired_columns():
    """The ML decision is retired (02); sucrose_level was total sugars (R9)."""
    retired = {
        "sucrose_level",
        "predicted_moisture",
        "predicted_sugar",
        "predicted_hmf",
        "authenticity_score",
        "validation_status",
        "explanation",
    }

    assert retired & set(LabResult.__table__.columns.keys()) == set()


def test_no_stage_table_carries_a_proof_hash_column():
    """v2 has no per-stage chain tx to compare against; the audit row's
    payload_hash is the recorded hash, and /verify locates it by action."""
    for name in _STAGE_TABLES:
        columns = set(Base.metadata.tables[name].columns.keys())
        assert not {c for c in columns if c.endswith("_proof_hash")}, name


def test_an_apiary_record_snapshots_its_coordinates_rather_than_joining(db):
    """Sprint 6's rule, carried forward: editing the apiary later must not
    invalidate a hash already anchored over the batch's snapshot."""
    batch_id, apiary_id = _seed_batch(db)
    record = ApiaryRecord(
        batch_id=batch_id,
        apiary_id=apiary_id,
        latitude=Decimal("-1.286389"),
        longitude=Decimal("36.817223"),
    )
    db.add(record)
    db.flush()

    apiary = db.get(ApiaryLocation, apiary_id)
    apiary.latitude = Decimal("0.000000")
    db.flush()
    db.refresh(record)

    assert record.latitude == Decimal("-1.286389")


def test_batch_metadata_keeps_notes_out_of_nothing_but_still_stores_them(db):
    """`notes` is excluded from the hash (10 §6) but must still persist so
    /verify can display it."""
    batch_id, _ = _seed_batch(db)
    row = BatchMetadata(
        batch_id=batch_id,
        honey_type="acacia",
        expected_yield_kg=Decimal("50.00"),
        harvest_window_start=dt.date(2026, 3, 1),
        harvest_window_end=dt.date(2026, 4, 1),
        apiary_management_method="langstroth",
        notes="left of the river",
    )
    db.add(row)
    db.flush()

    assert row.notes == "left of the river"


def test_conformance_records_a_nullable_flag_per_parameter(db):
    """NULL means the lab did not report it — distinct from False (failed)."""
    batch_id, _ = _seed_batch(db)
    row = CodexConformance(
        batch_id=batch_id,
        rule_set_version="codex-kenya-v1",
        verdict=ConformanceVerdict.INCOMPLETE,
        moisture_passed=True,
        sucrose_passed=False,
        diastase_passed=None,
    )
    db.add(row)
    db.flush()

    assert (row.moisture_passed, row.sucrose_passed, row.diastase_passed) == (True, False, None)
