"""Shared request-field types (P3-D, 10 §3).

Two constraints that every request schema needs and nobody remembers to apply
one field at a time. Declaring them as types means the rule is applied by
using the type, and `tests/test_schema_guards.py` fails the build if a new
field opts out.

**`SafeStr`** — PostgreSQL `text` cannot store U+0000. Client text carrying one
reaches the driver, raises `DataError`, and comes back as a 400 the endpoint
never documented. `08` handled this inline on `LoginRequest.identifier`; Phase
3a adds roughly ten free-text fields, so it becomes a type.

**`EntityId`** — every id column here is a 32-bit integer. A value past that
bound is a numeric-overflow `DataError`, not a row that does not exist, so it
answers 400 where the schema promised 404 or 422. Phase 2 bounded the path
parameters (`Path(ge=1, le=...)`); these are the same values arriving in a
body, which were never bounded.

Both push the failure to the edge, where it is honest input validation, rather
than into the driver, where it is a status the contract did not promise.
"""

from decimal import Decimal
from typing import Annotated

from pydantic import Field

# Postgres text rejects U+0000; everything else, including full Unicode, is fine.
SafeStr = Annotated[str, Field(pattern=r"^[^\x00]*$")]

_MAX_INT4 = 2_147_483_647

# A reference to a row: positive, and within the range an int4 column can hold.
EntityId = Annotated[int, Field(ge=1, le=_MAX_INT4)]

# A non-negative count that also lands in an int4 column (unit_count, hives).
Count = Annotated[int, Field(ge=0, le=_MAX_INT4)]

# Decimal degrees. Bounded to the real range, because a `Numeric(9, 6)` column
# silently refuses anything wider and a 400 from the driver reads as a server
# fault rather than the invalid input it is.
Latitude = Annotated[Decimal, Field(ge=Decimal("-90"), le=Decimal("90"), decimal_places=6)]
Longitude = Annotated[Decimal, Field(ge=Decimal("-180"), le=Decimal("180"), decimal_places=6)]

# A physical measurement: never negative, and inside `Numeric(10, 2)`.
Measurement = Annotated[
    Decimal, Field(ge=Decimal("0"), le=Decimal("99999999.99"), decimal_places=2)
]

# A proportion of the whole. Bounded at 100 because the commonest lab-form
# mistake is a fraction where a percent was meant, or the reverse — and 02 R9
# records what happens when a value silently means something other than its
# field name says.
Percentage = Annotated[Decimal, Field(ge=Decimal("0"), le=Decimal("100"), decimal_places=2)]
