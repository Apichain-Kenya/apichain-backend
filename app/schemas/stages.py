"""Request schemas for the five lifecycle transitions (P3-F, P3-G, P3-H).

Each mirrors the columns its stage table hashes. Constrained types do the
boundary work: `SafeStr` keeps U+0000 out of Postgres text, `Measurement`
keeps a value inside its `Numeric(10, 2)` column, and the coordinate types
bound decimal degrees to the real range. A value that would only fail at the
driver becomes a 422 here, where it is honestly invalid input.
"""

import datetime as dt

from pydantic import BaseModel, model_validator

from app.schemas.common import (
    Count,
    Latitude,
    Longitude,
    Measurement,
    Percentage,
    SafeStr,
)


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


class LabResultRequest(BaseModel):
    """S3. Every measurement names its quantity and its unit.

    `02` R9 is why: v1 carried `sucrose_level`, which actually held total
    sugars around 75-80% while the Codex sucrose limit is 5 g/100g, so a scorer
    reading it would have failed every honest batch. Naming the field for what
    it measures is the cheapest possible guard against that class of mistake.

    Every parameter is optional. A lab that reports five of six should be able
    to submit; the scorer marks the sixth `not_measured` and the verdict is
    `INCOMPLETE` rather than a confident pass.
    """

    moisture_pct: Percentage | None = None
    fructose_glucose_g_100g: Percentage | None = None
    sucrose_g_100g: Percentage | None = None
    hmf_mg_kg: Measurement | None = None
    diastase_schade: Measurement | None = None
    free_acidity_meq_kg: Measurement | None = None
    pollen_density: Measurement | None = None

    laboratory_name: SafeStr | None = None
    analyst_name: SafeStr | None = None
    certificate_number: SafeStr | None = None
    notes: SafeStr | None = None
    tested_at: dt.datetime | None = None

    @model_validator(mode="after")
    def _sugars_are_not_transposed(self) -> "LabResultRequest":
        """Sucrose <=5 and fructose+glucose >=60 are the two fields a form is
        most likely to swap, and a swap passes range validation on both. Honey
        with more sucrose than fructose+glucose does not exist, so this is
        invalid input rather than a batch that confidently fails."""
        if (
            self.sucrose_g_100g is not None
            and self.fructose_glucose_g_100g is not None
            and self.sucrose_g_100g > self.fructose_glucose_g_100g
        ):
            raise ValueError(
                "sucrose_g_100g exceeds fructose_glucose_g_100g; the two are likely transposed"
            )
        return self


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
