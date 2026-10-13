"""Dev seed for scripts/live_walk.py: one field officer (`officer-live`) in the
compose dev database. Staff creation has no endpoint until Phase 4b. Run inside
the api container; never against a real deployment."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import engine
from app.enums import Role
from app.models import User
from app.services import security

with Session(engine) as s:
    if s.execute(select(User).where(User.username == "officer-live")).scalar_one_or_none() is None:
        s.add(
            User(
                username="officer-live",
                password_hash=security.hash_password("live-check-pw"),
                role=Role.field_officer,
                is_root=False,
                is_active=True,
            )
        )
        s.commit()
    print("officer ready")
