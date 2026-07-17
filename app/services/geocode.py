"""Ported from v1 ApiChain--Backend/backend/app/services/geocode.py (unchanged).

Nominatim ToS requires a custom User-Agent; the project uses 'AgriScanAI-App'.
"""

import requests

_NOMINATIM = "https://nominatim.openstreetmap.org/reverse"


def reverse_geocode(latitude: float, longitude: float) -> str | None:
    """Reverse-geocode apiary coords to a human place string for the consumer
    scan page. Fail-soft (returns None on ANY error): never block /verify."""
    try:
        params: dict[str, str | float | int] = {
            "lat": latitude,
            "lon": longitude,
            "format": "json",
            "zoom": 10,
        }
        r = requests.get(
            _NOMINATIM,
            params=params,
            headers={"User-Agent": "AgriScanAI-App"},
            timeout=8,
        )
        if r.status_code != 200:
            return None
        addr = (r.json() or {}).get("address", {})
        parts = [
            addr.get("county") or addr.get("city") or addr.get("town"),
            addr.get("state"),
            addr.get("country"),
        ]
        parts = [p for p in parts if p]
        return ", ".join(parts) or None
    except Exception:
        return None
