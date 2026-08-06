"""Shared seeding helpers for endpoint tests (P3-E).

P3-E made `POST /v2/batches` require an apiary and a typed metadata block, so
"give me a batch" stopped being a one-line JSON body. Every test that needs a
batch would otherwise carry its own copy of the seeding, and the next required
field would mean editing all of them again. `create_batch` is the seam.

These are helpers, not fixtures: a test that needs two farmers or a specific
role calls them twice with different arguments, which a fixture cannot do
without parametrization gymnastics.
"""

from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from app.enums import Role
from app.models import ApiaryLocation, Farmer, User
from app.services import security

# A valid metadata block. Tests that care about one field spread over this
# rather than restating the other five.
METADATA: dict[str, Any] = {
    "honey_type": "acacia",
    "expected_yield_kg": "50.00",
    "harvest_window_start": "2026-03-01",
    "harvest_window_end": "2026-04-01",
    "apiary_management_method": "organic",
}


def auth(user_id: int, role: Role) -> dict[str, str]:
    return {"Authorization": f"Bearer {security.create_access_token(sub=user_id, role=role)}"}


def seed_user(
    engine, role: Role, username: str, *, password: str = "pw", farmer_id: int | None = None
) -> int:
    """A credential row. `farmer_id` links it to an existing farmer profile,
    which is what a `farmer`-role token needs to pass an ownership check."""
    with Session(engine) as s:
        user = User(
            username=username,
            password_hash=security.hash_password(password),
            role=role,
            is_root=False,
            is_active=True,
        )
        s.add(user)
        s.flush()
        if farmer_id is not None:
            s.get(Farmer, farmer_id).user_id = user.id
        s.commit()
        return user.id


def seed_farmer(engine, phone: str) -> int:
    with Session(engine) as s:
        farmer = Farmer(first_name="A", last_name="B", phone=phone)
        s.add(farmer)
        s.commit()
        return farmer.id


def seed_apiary(engine, farmer_id: int, *, name: str = "Ridge apiary") -> int:
    with Session(engine) as s:
        apiary = ApiaryLocation(
            farmer_id=farmer_id,
            name=name,
            latitude=Decimal("-1.286389"),
            longitude=Decimal("36.817223"),
            altitude=Decimal("1795.00"),
            vegetation_type="acacia_woodland",
            hive_count=12,
        )
        s.add(apiary)
        s.commit()
        return apiary.id


def create_batch(
    client,
    engine,
    *,
    phone: str,
    actor_role: Role = Role.operator,
    username: str | None = None,
    headers: dict[str, str] | None = None,
    **overrides: Any,
) -> dict[str, Any]:
    """Seed a farmer, an apiary and an actor, then create a batch over HTTP.

    Returns the response body. `headers` overrides the generated auth header
    (for an Idempotency-Key test); `overrides` patches the request body.
    """
    farmer_id = seed_farmer(engine, phone)
    apiary_id = seed_apiary(engine, farmer_id)
    actor = seed_user(engine, actor_role, username or f"actor{phone[-6:]}")
    body: dict[str, Any] = {
        "farmer_id": farmer_id,
        "apiary_id": apiary_id,
        "metadata": METADATA,
        **overrides,
    }
    response = client.post("/v2/batches", json=body, headers=headers or auth(actor, actor_role))
    assert response.status_code == 201, response.text
    return response.json()
