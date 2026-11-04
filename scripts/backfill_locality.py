"""Fill in the locality of every apiary created without one (12 §6, 14 D19).

    docker compose exec api uv run python scripts/backfill_locality.py

An apiary is created even when the Nominatim lookup times out or is refused,
and its locality is left empty. This looks each one up again, at most once a
second (Nominatim's usage policy), and is safe to run as often as you like:
it only touches rows that still have no locality.
"""

import sys

from sqlalchemy.orm import Session

from app.database import engine
from app.services import geocode


def main() -> int:
    geocoder = geocode.from_settings()
    with Session(engine) as db:
        filled = geocode.backfill_localities(db, geocoder)
    print(f"Filled {filled} apiary localities.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
