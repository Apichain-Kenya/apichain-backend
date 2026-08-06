"""Request schemas for the five lifecycle transitions (P3-F, P3-G, P3-H).

Each mirrors the columns its stage table hashes. Constrained types do the
boundary work: `SafeStr` keeps U+0000 out of Postgres text, `Measurement`
keeps a value inside its `Numeric(10, 2)` column, and the coordinate types
bound decimal degrees to the real range. A value that would only fail at the
driver becomes a 422 here, where it is honestly invalid input.
"""

import datetime as dt

from pydantic import BaseModel

from app.schemas.common import Count, Latitude, Longitude, Measurement, SafeStr


class HarvestRecordRequest(BaseModel):
    """S1. `harvest_date` is actor-asserted and hashed; an offset on the wire
    is normalized to UTC before storage (see `models/types.UtcDateTime`)."""

    harvest_date: dt.datetime
    quantity_kg: Measurement
    hive_ids: list[SafeStr] = []
    gps_lat: Latitude | None = None
    gps_lon: Longitude | None = None
    notes: SafeStr | None = None


class ProcessRecordRequest(BaseModel):
    """S2."""

    extraction_method: SafeStr
    moisture_content: Measurement | None = None
    handling_notes: SafeStr | None = None


class PackagingRecordRequest(BaseModel):
    """S4. One QR per batch, so v1's per-jar `qr_codes` is gone; `unit_count`
    already equals the jar count."""

    unit_count: Count
    jar_ids: list[SafeStr] = []
    notes: SafeStr | None = None


class DistributionRecordRequest(BaseModel):
    """S5, terminal."""

    retailer_name: SafeStr
    transport_reference: SafeStr | None = None
    handover_notes: SafeStr | None = None
