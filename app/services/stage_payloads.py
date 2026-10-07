"""Canonical payloads for the seven lifecycle stages (P3-D, 04 §5.2).

Each builder turns a stage row into the exact dict that is hashed into the
audit chain and, via Phase 2, anchored to Bitcoin. Ported from v1's
`_*_canonical_payload` helpers in `routers/batch.py` — v1's strongest work, and
the reason `/verify` can name *which* stage was altered rather than only that
something was.

One function per stage, with its field list written out. No dispatch over
column names: the field set of a hashed payload is the thing a reviewer most
needs to read in one place, and deriving it from the model would mean a new
column silently joins the hash.

**Conventions, stated once because every builder depends on them.**

- **Numbers are fixed-precision strings.** A hash must not depend on whether a
  value arrived as `42.5`, `42.50` or `Decimal("42.5")`. Coordinates render at
  six decimal places (~0.11 m), everything else at two. This is v1's `_q4`
  pattern, applied to every numeric rather than only the ML-derived ones.
- **Datetimes go through `canonical_dt`.** The columns are `TIMESTAMP WITHOUT
  TIME ZONE`, so a tz-aware value written by Pydantic comes back naive. v1's
  Sprint 6 watchlist records this as a silent determinism break that only a
  persisted-then-reloaded test catches.
- **Server bookkeeping timestamps are excluded.** `recorded_at` is when *we*
  wrote the row; the audit row's own `created_at` already records that fact and
  anchors it, so a second copy inside the payload buys nothing and adds a field
  that must be serialized correctly forever. Actor-asserted dates — the harvest
  date, the lab test date, the declared harvest window — are facts about the
  honey and are hashed. `batch_metadata.recorded_at` is the single exception,
  kept because v1 hashed it and parity is cheaper than a justification.
- **Absent optionals serialize as `None`, never as a dropped key**, because a
  missing key changes the hash.

**One inherited inconsistency, carried deliberately.** v1 excluded `notes` from
`batch_metadata`'s payload — so a farmer can fix a typo without invalidating
anchored history — and then included `notes`, `handling_notes` and
`handover_notes` in the other five. The rationale was applied to one table of
six. These ports keep v1's field sets exactly, so the asymmetry comes with
them; it is recorded in `PORTING.md` rather than quietly normalized, because
changing which fields are hashed is a design decision, not a port.
"""

import datetime as dt
from collections.abc import Callable, Mapping
from decimal import Decimal
from typing import Any

from app.models import (
    ApiaryRecord,
    BatchMetadata,
    DistributionRecord,
    HarvestRecord,
    LabResult,
    PackagingRecord,
    ProcessRecord,
)
from app.services.canonical import canonical_dt

_TWO_PLACES = Decimal("0.01")
_SIX_PLACES = Decimal("0.000001")


def _q2(value: Decimal | float | None) -> str | None:
    """Two decimal places: quantities, percentages, lab measurements."""
    if value is None:
        return None
    return str(Decimal(str(value)).quantize(_TWO_PLACES))


def _q6(value: Decimal | float | None) -> str | None:
    """Six decimal places: decimal degrees, ~0.11 m."""
    if value is None:
        return None
    return str(Decimal(str(value)).quantize(_SIX_PLACES))


def _date(value: dt.date | None) -> str | None:
    return value.isoformat() if value is not None else None


def _lower(value: str | None) -> str | None:
    """Enum-ish strings are compared case-insensitively, so hash them folded."""
    return value.lower() if value is not None else None


def apiary_record(row: ApiaryRecord) -> dict[str, Any]:
    """S0: where the honey was produced.

    A snapshot, not a join. v1 Sprint 6 moved these columns onto the record so
    that editing the underlying apiary later cannot invalidate this hash.
    """
    return {
        "batch_id": row.batch_id,
        "apiary_id": row.apiary_id,
        "latitude": _q6(row.latitude),
        "longitude": _q6(row.longitude),
        "altitude": _q2(row.altitude),
        "vegetation_type": row.vegetation_type,
        "hive_count": row.hive_count,
    }


def batch_metadata(row: BatchMetadata) -> dict[str, Any]:
    """S0: what the farmer declared. `notes` is excluded (v1 Sprint 8)."""
    return {
        "batch_id": row.batch_id,
        "honey_type": _lower(row.honey_type),
        "expected_yield_kg": _q2(row.expected_yield_kg),
        "harvest_window_start": _date(row.harvest_window_start),
        "harvest_window_end": _date(row.harvest_window_end),
        "apiary_management_method": _lower(row.apiary_management_method),
        "recorded_at": canonical_dt(row.recorded_at),
    }


def harvest_record(row: HarvestRecord) -> dict[str, Any]:
    """S1: the harvest."""
    return {
        "batch_id": row.batch_id,
        "harvest_date": canonical_dt(row.harvest_date),
        "quantity_kg": _q2(row.quantity_kg),
        "hive_ids": list(row.hive_ids) if row.hive_ids else [],
        "gps_lat": _q6(row.gps_lat),
        "gps_lon": _q6(row.gps_lon),
        "notes": row.notes,
    }


def process_record(row: ProcessRecord) -> dict[str, Any]:
    """S2: extraction and handling."""
    return {
        "batch_id": row.batch_id,
        "extraction_method": row.extraction_method,
        "moisture_content": _q2(row.moisture_content),
        "handling_notes": row.handling_notes,
    }


def lab_result(row: LabResult) -> dict[str, Any]:
    """S3: the lab panel.

    Adapted rather than ported. v1's payload carried `sucrose_level`, which
    actually held total sugars (`02` R9), and five GeoAI fields from the model
    `02` retires. Both are gone; each measurement now names its quantity and
    unit. `tested_at` is added — v1 hashed no timestamp here, which left the
    date on a certificate alterable without breaking verification.
    """
    return {
        "batch_id": row.batch_id,
        "moisture_pct": _q2(row.moisture_pct),
        "fructose_glucose_g_100g": _q2(row.fructose_glucose_g_100g),
        "sucrose_g_100g": _q2(row.sucrose_g_100g),
        "hmf_mg_kg": _q2(row.hmf_mg_kg),
        "diastase_schade": _q2(row.diastase_schade),
        "free_acidity_meq_kg": _q2(row.free_acidity_meq_kg),
        "pollen_density": _q2(row.pollen_density),
        "laboratory_name": row.laboratory_name,
        "analyst_name": row.analyst_name,
        "certificate_number": row.certificate_number,
        "notes": row.notes,
        "tested_at": canonical_dt(row.tested_at),
    }


def packaging_record(row: PackagingRecord) -> dict[str, Any]:
    """S4: units and jar identity. One QR per batch, so no `qr_codes`."""
    return {
        "batch_id": row.batch_id,
        "unit_count": row.unit_count,
        "jar_ids": list(row.jar_ids) if row.jar_ids else [],
        "notes": row.notes,
    }


def distribution_record(row: DistributionRecord) -> dict[str, Any]:
    """S5: handover to the retailer. Terminal."""
    return {
        "batch_id": row.batch_id,
        "retailer_name": row.retailer_name,
        "transport_reference": row.transport_reference,
        "handover_notes": row.handover_notes,
    }


# The registry `/verify` walks. A stage missing here would drop silently out of
# the three-way match, so `tests/test_stage_payloads.py` pins the key set.
BUILDERS: Mapping[str, Callable[[Any], dict[str, Any]]] = {
    "apiary": apiary_record,
    "metadata": batch_metadata,
    "harvest": harvest_record,
    "process": process_record,
    "lab": lab_result,
    "packaging": packaging_record,
    "distribution": distribution_record,
}
