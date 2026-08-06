"""Row-level ownership checks for farmer-scoped actions.

`requires(action)` answers "may this kind of actor do this kind of thing".
It cannot answer "may *this* actor do it to *this* row", and the gap between
those two questions is what IDOR is. `04` P3 records the v1 instance: `POST
/farmers/farm-details/{id}` had a JWT check and a role check and no ownership
check, so any farmer's token could mutate any farmer's record.

Three v2 actions admit `Role.farmer` — seeding an apiary, creating a batch, and
recording a harvest — so all three need the second check, and it lives here
once so a fourth cannot be added without finding it.

Staff roles are deliberately unrestricted. `04` §5.3 wants one actor able to
walk a batch end to end without a role change, and that stays safe because
`audit_log.actor_id` and `actor_role` record who actually performed each step.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.enums import Role
from app.errors import APIError
from app.models import Farmer, User


def farmer_profile_id(db: Session, actor: User) -> int | None:
    """The `farmers` row this credential belongs to, if any (08 D8)."""
    return db.execute(select(Farmer.id).where(Farmer.user_id == actor.id)).scalar_one_or_none()


def assert_acts_for_farmer(db: Session, actor: User, farmer_id: int) -> None:
    """Refuse a farmer acting on someone else's records. No-op for staff.

    A `farmer`-role credential with no profile row owns nothing and is refused
    outright — comparing a NULL against the target would otherwise decide the
    question by accident.
    """
    if actor.role is not Role.farmer:
        return

    own = farmer_profile_id(db, actor)
    if own is None or own != farmer_id:
        raise APIError(
            403,
            "forbidden",
            "A farmer may only act on their own records",
            {"farmer_id": farmer_id},
        )
