"""Per-stage record tables — the canonical payloads of the six-state lifecycle.

Ported from v1's Sprint 5-8 work (`app/models/{apiary,apiary_record,
batch_metadata,harvest_record,process_record,lab_result,packaging_record,
distribution_record}.py`), which `04` §5.2 carries forward wholesale. One
normalized row per stage; the row IS the pre-image that gets hashed into the
audit chain, so `/verify` can recompute it later and name which stage changed.

**Three things every table here shares, and nothing else.** A `batch_id` with
`UNIQUE` on it, a recorded-at timestamp, and an integer primary key. The
payload columns are disjoint, which is why these stay seven tables rather than
one JSONB blob: `lab_results` in particular is the conformance scorer's input,
and typed columns are what make "moisture is a percent" a constraint instead
of a hope.

**`UNIQUE(batch_id)` is load-bearing.** It makes a stage unrepeatable at the
database level, so a double-submitted transition raises `IntegrityError` and
the handler can turn that into a 409 *before* `audit_log.append()` flushes. An
append that later rolls back burns an audit id permanently (Postgres sequences
are not transactional), so the ordering matters more than it looks.

**Two v1 columns deliberately not carried forward.**

- `*_proof_hash` on each stage table. v1 needed somewhere to keep the hash it
  had anchored in a per-stage Sepolia transaction. v2 has no per-stage
  transaction: the audit row written in the same transaction already holds
  `payload_hash`, and Phase 2 anchors it. A duplicate column would be a second
  source of truth for one anchored fact, and could only be written *after* the
  append, which the handler order forbids.
- `lab_results.sucrose_level` and the six GeoAI columns. `02` R9: that field
  held total sugars (~75-80%) under a name meaning the Codex parameter capped
  at 5 g/100g, and Sprint 14 deliberately routed total sugars through it. The
  measured fields here name their quantity and unit. The ML columns are gone
  because `02` retires the model as decision-maker; `codex_conformance`
  replaces the decision and `origin_verification` will hold the origin work.

**Numerics are `Numeric`, not `Float`.** v1 hashed native floats and got away
with it. Exact decimals render to a fixed-precision string with no repr
surprises, which is what a hash whose determinism is non-negotiable wants.

**No PostGIS here.** `04` and the backend principles want `Geography(POINT,
4326)`, but coordinates sit inside hashed payloads, and putting WKB/WKT
serialization next to a hash is risk taken for a capability nothing in Phase 3a
consumes (`10` D5). Plain decimal degrees now; Geography lands with the
geo-mapping UI.
"""

import datetime as dt
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    Date,
    Enum,
    ForeignKey,
    Integer,
    Numeric,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.enums import ConformanceVerdict
from app.models.types import UtcDateTime

# Decimal degrees to six places is ~0.11 m — finer than any consumer GPS.
_COORD = Numeric(9, 6)
# Lab and yield quantities. Two places matches how labs report and how the
# canonical payload renders them.
_MEASURE = Numeric(10, 2)


class ApiaryLocation(Base):
    """A farmer's apiary. The source an `ApiaryRecord` snapshots from."""

    __tablename__ = "apiary_locations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    farmer_id: Mapped[int] = mapped_column(ForeignKey("farmers.id"), index=True)
    name: Mapped[str] = mapped_column(String)
    # Exact point, stored and used internally; the anonymous consumer view
    # reduces precision rather than the store doing so (03 §8.1, 10 D11).
    latitude: Mapped[Decimal] = mapped_column(_COORD, info={"pii": "sensitive"})
    longitude: Mapped[Decimal] = mapped_column(_COORD, info={"pii": "sensitive"})
    altitude: Mapped[Decimal | None] = mapped_column(_MEASURE)
    vegetation_type: Mapped[str | None] = mapped_column(String)
    hive_count: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[dt.datetime] = mapped_column(UtcDateTime, server_default=func.now())


class ApiaryRecord(Base):
    """S0 pre-image: where this batch's honey was produced.

    The coordinate columns deliberately duplicate `apiary_locations` as a
    snapshot (v1 Sprint 6). Editing the apiary later must not invalidate a hash
    already anchored over this batch.
    """

    __tablename__ = "apiary_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("honey_batches.id"), unique=True)
    apiary_id: Mapped[int] = mapped_column(ForeignKey("apiary_locations.id"))
    latitude: Mapped[Decimal] = mapped_column(_COORD, info={"pii": "sensitive"})
    longitude: Mapped[Decimal] = mapped_column(_COORD, info={"pii": "sensitive"})
    altitude: Mapped[Decimal | None] = mapped_column(_MEASURE)
    vegetation_type: Mapped[str | None] = mapped_column(String)
    hive_count: Mapped[int | None] = mapped_column(Integer)
    recorded_at: Mapped[dt.datetime] = mapped_column(UtcDateTime, server_default=func.now())


class BatchMetadata(Base):
    """S0 pre-image: what the farmer declared about the batch.

    `notes` is stored but excluded from the canonical payload, so correcting a
    typo cannot invalidate anchored history (v1 Sprint 8). `honey_type` and
    `apiary_management_method` are plain strings validated by Pydantic enums at
    the API boundary, so amending the allowed values is a one-file edit rather
    than an `ALTER TYPE`.
    """

    __tablename__ = "batch_metadata"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("honey_batches.id"), unique=True)
    honey_type: Mapped[str] = mapped_column(String)
    expected_yield_kg: Mapped[Decimal] = mapped_column(_MEASURE)
    harvest_window_start: Mapped[dt.date] = mapped_column(Date)
    harvest_window_end: Mapped[dt.date] = mapped_column(Date)
    apiary_management_method: Mapped[str] = mapped_column(String)
    notes: Mapped[str | None] = mapped_column(String)
    recorded_at: Mapped[dt.datetime] = mapped_column(UtcDateTime, server_default=func.now())


class HarvestRecord(Base):
    """S1 pre-image: the harvest itself."""

    __tablename__ = "harvest_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("honey_batches.id"), unique=True)
    harvest_date: Mapped[dt.datetime] = mapped_column(UtcDateTime)
    quantity_kg: Mapped[Decimal] = mapped_column(_MEASURE)
    hive_ids: Mapped[list[Any]] = mapped_column(JSONB)
    gps_lat: Mapped[Decimal | None] = mapped_column(_COORD, info={"pii": "sensitive"})
    gps_lon: Mapped[Decimal | None] = mapped_column(_COORD, info={"pii": "sensitive"})
    notes: Mapped[str | None] = mapped_column(String)
    recorded_at: Mapped[dt.datetime] = mapped_column(UtcDateTime, server_default=func.now())


class ProcessRecord(Base):
    """S2 pre-image: extraction and handling."""

    __tablename__ = "process_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("honey_batches.id"), unique=True)
    extraction_method: Mapped[str] = mapped_column(String)
    moisture_content: Mapped[Decimal | None] = mapped_column(_MEASURE)
    handling_notes: Mapped[str | None] = mapped_column(String)
    recorded_at: Mapped[dt.datetime] = mapped_column(UtcDateTime, server_default=func.now())


class LabResult(Base):
    """S3 pre-image: the lab panel, and the conformance scorer's input.

    Every measured column names its quantity and its unit. `pollen_density` is
    stored but not scored: it is not a Codex parameter, and `02` demotes pollen
    to a corroboration signal until melissopalynology lands, at which point it
    belongs to `origin_verification`.
    """

    __tablename__ = "lab_results"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("honey_batches.id"), unique=True)

    moisture_pct: Mapped[Decimal | None] = mapped_column(_MEASURE)
    fructose_glucose_g_100g: Mapped[Decimal | None] = mapped_column(_MEASURE)
    sucrose_g_100g: Mapped[Decimal | None] = mapped_column(_MEASURE)
    hmf_mg_kg: Mapped[Decimal | None] = mapped_column(_MEASURE)
    diastase_schade: Mapped[Decimal | None] = mapped_column(_MEASURE)
    free_acidity_meq_kg: Mapped[Decimal | None] = mapped_column(_MEASURE)
    pollen_density: Mapped[Decimal | None] = mapped_column(_MEASURE)

    laboratory_name: Mapped[str | None] = mapped_column(String)
    analyst_name: Mapped[str | None] = mapped_column(String, info={"pii": "identity"})
    certificate_number: Mapped[str | None] = mapped_column(String)
    notes: Mapped[str | None] = mapped_column(String)
    tested_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)
    recorded_at: Mapped[dt.datetime] = mapped_column(UtcDateTime, server_default=func.now())


class PackagingRecord(Base):
    """S4 pre-image: units and jar identity.

    v1 dropped `qr_codes` in Sprint 13 — one QR per batch, not per jar — and
    `unit_count` already equals the jar count. Both facts carried forward.
    """

    __tablename__ = "packaging_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("honey_batches.id"), unique=True)
    unit_count: Mapped[int] = mapped_column(Integer)
    jar_ids: Mapped[list[Any]] = mapped_column(JSONB)
    notes: Mapped[str | None] = mapped_column(String)
    recorded_at: Mapped[dt.datetime] = mapped_column(UtcDateTime, server_default=func.now())


class DistributionRecord(Base):
    """S5 pre-image: handover to the retailer. Terminal state."""

    __tablename__ = "distribution_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("honey_batches.id"), unique=True)
    retailer_name: Mapped[str] = mapped_column(String)
    transport_reference: Mapped[str | None] = mapped_column(String)
    handover_notes: Mapped[str | None] = mapped_column(String)
    recorded_at: Mapped[dt.datetime] = mapped_column(UtcDateTime, server_default=func.now())


class CodexConformance(Base):
    """The rule-based verdict over a batch's lab panel (04 §5.2, 02 §4).

    One nullable flag per Codex parameter: NULL means the lab did not report
    it, which is not the same as False. The limits are NOT stored here — they
    are reproducible from the frozen rule set named by `rule_set_version`, and
    a second copy would be a second source of truth for an anchored fact.
    """

    __tablename__ = "codex_conformance"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("honey_batches.id"), unique=True)
    rule_set_version: Mapped[str] = mapped_column(String)
    verdict: Mapped[ConformanceVerdict] = mapped_column(
        Enum(ConformanceVerdict, name="conformance_verdict")
    )

    moisture_passed: Mapped[bool | None] = mapped_column(Boolean)
    fructose_glucose_passed: Mapped[bool | None] = mapped_column(Boolean)
    sucrose_passed: Mapped[bool | None] = mapped_column(Boolean)
    hmf_passed: Mapped[bool | None] = mapped_column(Boolean)
    diastase_passed: Mapped[bool | None] = mapped_column(Boolean)
    free_acidity_passed: Mapped[bool | None] = mapped_column(Boolean)

    evaluated_at: Mapped[dt.datetime] = mapped_column(UtcDateTime, server_default=func.now())
