"""Identity tables: `users` (credentialed actors) and `farmers` (profiles).

Ported and stripped from v1 `app/models/user.py` and `farmer.py`. Dropped in
v2 (04 §5.2, §5.8): `wallet_address` (custodial keys retired) and the v1
plaintext-ish `password` naming (now `password_hash`). A farmer is BOTH a
`users` row (credentials, `role=farmer`) and a `farmers` row (profile), linked
by `farmers.user_id` (08 D8). PII columns carry an `info={"pii": ...}` tag for
the Phase 5 data-access audit work (04 §5.8) — cheap to add now.

Geo columns (v1 `farmers.location`) are deferred to Phase 3 with the apiary
work, keeping the Phase 1 baseline PostGIS-free.
"""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.enums import Role


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Login is phone-or-username; either may be absent, at least one is required
    # (enforced at the app layer in P1-F). Both unique when present.
    username: Mapped[str | None] = mapped_column(String, unique=True, index=True)
    phone: Mapped[str | None] = mapped_column(
        String, unique=True, index=True, info={"pii": "contact"}
    )
    password_hash: Mapped[str] = mapped_column(String)
    role: Mapped[Role] = mapped_column(Enum(Role, name="role"))
    # super_admin collapses into admin with is_root for the bootstrap only (04 §5.3).
    is_root: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class Farmer(Base):
    __tablename__ = "farmers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    first_name: Mapped[str] = mapped_column(String, info={"pii": "identity"})
    last_name: Mapped[str] = mapped_column(String, info={"pii": "identity"})
    phone: Mapped[str] = mapped_column(String, unique=True, index=True, info={"pii": "contact"})
    email: Mapped[str | None] = mapped_column(String, unique=True, info={"pii": "contact"})
    address: Mapped[str | None] = mapped_column(String, info={"pii": "identity"})
    number_of_hives: Mapped[int | None] = mapped_column(Integer)
    # The credential row for this farmer (08 D8).
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    # The field officer who enrolled them (04 §5.3).
    enrolled_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
