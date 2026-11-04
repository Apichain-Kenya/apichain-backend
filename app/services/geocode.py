"""Reverse-geocode an apiary to a locality label for the consumer view.

Ported from v1 `ApiChain--Backend/backend/app/services/geocode.py`; adapted in
P4a-B (12 D12):

- **Coordinates are rounded to 2 dp (about 1.1 km) before they leave our
  system.** The exact hive point is sensitive personal data (03 §8.1), and
  Nominatim is a third party; a locality lookup needs no more precision than
  the consumer is shown.
- The User-Agent names this project (Nominatim's policy requires a real one;
  v1 sent `AgriScanAI-App`).
- A 3 s timeout. The call is made with no database connection held (14 D19):
  the apiary-create handler ends its read transaction first.

Fail-soft: any error returns None, and the apiary is created without a
locality. Every miss is logged with its reason (never the point), and
`backfill_localities` fills the gap later. The result is a display label,
never part of a hashed payload.
"""

import logging
import time
from collections.abc import Callable
from decimal import ROUND_HALF_EVEN, Decimal

import requests
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import ApiaryLocation

logger = logging.getLogger("apichain.geocode")

# Nominatim's usage policy: at most one request per second.
BACKFILL_INTERVAL_SECONDS = 1.0

_NOMINATIM = "https://nominatim.openstreetmap.org/reverse"
_USER_AGENT = "ApiChainKenya/2 (+https://github.com/Apichain-Kenya/apichain-backend)"
_LOCALITY = Decimal("0.01")

Geocoder = Callable[[Decimal, Decimal], str | None]


def locality_point(latitude: Decimal, longitude: Decimal) -> tuple[Decimal, Decimal]:
    """The only precision a locality lookup is ever given (12 D12)."""
    return (
        Decimal(latitude).quantize(_LOCALITY, rounding=ROUND_HALF_EVEN),
        Decimal(longitude).quantize(_LOCALITY, rounding=ROUND_HALF_EVEN),
    )


def reverse_geocode(latitude: Decimal, longitude: Decimal) -> str | None:
    lat, lon = locality_point(latitude, longitude)
    params: dict[str, str | int] = {"lat": str(lat), "lon": str(lon), "format": "json", "zoom": 10}
    try:
        r = requests.get(
            _NOMINATIM,
            params=params,
            headers={"User-Agent": _USER_AGENT},
            timeout=3,
        )
        if r.status_code != 200:
            logger.warning("locality lookup refused: HTTP %s", r.status_code)
            return None
        addr = (r.json() or {}).get("address", {})
        parts = [
            addr.get("county") or addr.get("city") or addr.get("town"),
            addr.get("state"),
            addr.get("country"),
        ]
        label = ", ".join(p for p in parts if p)
        return label[:120] or None
    except Exception as exc:
        logger.warning("locality lookup failed: %s", type(exc).__name__)
        return None


def _no_lookup(latitude: Decimal, longitude: Decimal) -> str | None:
    return None


def from_settings() -> Geocoder:
    """`nominatim` (default) or `none`. `none` skips the lookup entirely, for
    deployments that decline the third-party call (12 D12's alternative)."""
    if settings.geocoder_backend == "none":
        return _no_lookup
    return reverse_geocode


def backfill_localities(
    db: Session,
    geocoder: Geocoder,
    *,
    pause: Callable[[float], None] = time.sleep,
) -> int:
    """Look up every apiary that has no locality yet; returns how many it filled.

    One lookup per second, and each filled row is committed on its own, so an
    interrupted run keeps what it did and a re-run picks up the rest. A miss
    leaves the row NULL for the next run. `locality` is outside every hashed
    payload, so writing it later changes no record's hash.
    """
    ids = (
        db.execute(
            select(ApiaryLocation.id)
            .where(ApiaryLocation.locality.is_(None))
            .order_by(ApiaryLocation.id)
        )
        .scalars()
        .all()
    )
    filled = 0
    for n, apiary_id in enumerate(ids):
        if n:
            pause(BACKFILL_INTERVAL_SECONDS)
        apiary = db.get(ApiaryLocation, apiary_id)
        if apiary is None or apiary.locality is not None:
            continue
        label = geocoder(*locality_point(apiary.latitude, apiary.longitude))
        if label is None:
            continue
        apiary.locality = label
        db.commit()
        filled += 1
    return filled
