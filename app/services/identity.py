"""How a person is named on screen (13 D6, handoff item 9).

One rule for `/v2/me`, batch detail and the console reads: a farmer by the name
on their profile, a staff member by `full_name`, and only then the username,
the phone, or "user <id>". Before 4b each call site had its own copy, and a
batch record could read "officer-live" where 03 §13.1 wants a person.
"""

from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Farmer, User


def farmer_name(farmer: Farmer) -> str:
    return f"{farmer.first_name} {farmer.last_name}"


def display_name(user: User, farmer: Farmer | None = None) -> str:
    if farmer is not None:
        return farmer_name(farmer)
    return user.full_name or user.username or user.phone or f"user {user.id}"


def display_names(db: Session, user_ids: Iterable[int]) -> dict[int, str]:
    """`display_name` for many users in two queries."""
    ids = set(user_ids)
    if not ids:
        return {}
    farmers = {
        f.user_id: f
        for f in db.execute(select(Farmer).where(Farmer.user_id.in_(ids))).scalars()
        if f.user_id is not None
    }
    return {
        user.id: display_name(user, farmers.get(user.id))
        for user in db.execute(select(User).where(User.id.in_(ids))).scalars()
    }
