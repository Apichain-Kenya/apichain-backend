"""Apiary seeding schemas (P3-E).

An apiary is a farmer's hive site: the thing a batch's provenance claim points
at. `03` §8.1 has the client capture it by map tap rather than typed
coordinates, so the wire format is plain decimal degrees and the picker's
output drops straight in.
"""

from decimal import Decimal

from pydantic import BaseModel, ConfigDict

from app.schemas.common import Count, EntityId, Latitude, Longitude, Measurement, SafeStr


class ApiaryCreateRequest(BaseModel):
    farmer_id: EntityId
    name: SafeStr
    latitude: Latitude
    longitude: Longitude
    altitude: Measurement | None = None
    vegetation_type: SafeStr | None = None
    hive_count: Count | None = None


class ApiaryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    farmer_id: int
    name: str
    latitude: Decimal
    longitude: Decimal
    altitude: Decimal | None
    vegetation_type: str | None
    hive_count: int | None
