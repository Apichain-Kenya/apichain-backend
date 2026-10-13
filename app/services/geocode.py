"""Reverse-geocode an apiary to a locality label for the consumer view.

Ported from v1 `ApiChain--Backend/backend/app/services/geocode.py`; adapted in
P4a-B (12 D12):

- **Coordinates are rounded to 2 dp (about 1.1 km) before they leave our
  system.** The exact hive point is sensitive personal data (03 §8.1), and
  Nominatim is a third party; a locality lookup needs no more precision than
  the consumer is shown.
- The User-Agent names this project (Nominatim's policy requires a real one;
  v1 sent `AgriScanAI-App`).
- A 3 s timeout, because the call sits in the apiary-create request.

Fail-soft: any error returns None, and the apiary is created without a
locality. The result is a display label, never part of a hashed payload.
"""

from collections.abc import Callable
from decimal import ROUND_HALF_EVEN, Decimal

import requests

from app.config import settings

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
            return None
        addr = (r.json() or {}).get("address", {})
        parts = [
            addr.get("county") or addr.get("city") or addr.get("town"),
            addr.get("state"),
            addr.get("country"),
        ]
        label = ", ".join(p for p in parts if p)
        return label[:120] or None
    except Exception:
        return None


def _no_lookup(latitude: Decimal, longitude: Decimal) -> str | None:
    return None


def from_settings() -> Geocoder:
    """`nominatim` (default) or `none`. `none` skips the lookup entirely, for
    deployments that decline the third-party call (12 D12's alternative)."""
    if settings.geocoder_backend == "none":
        return _no_lookup
    return reverse_geocode
