"""Engine and session wiring, shape ported from v1 ApiChain--Backend
(app/database.py) and updated to SQLAlchemy 2 style with psycopg 3.

Models declare against Base; Alembic's env.py imports Base.metadata as the
autogenerate target. The v2 schema baseline is authored in Phase 1.
"""

from collections.abc import Generator

from sqlalchemy import Engine, MetaData, create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import settings

# Deterministic constraint/index names so Alembic autogenerate and downgrades
# are stable across machines (no auto-generated PG names leaking into scripts).
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_name)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def make_engine(url: str) -> Engine:
    """Every connection runs in UTC (14 D20).

    Seventeen columns default to the database's `now()`. Under a session zone
    other than UTC (the native test Postgres runs in Nairobi) those naive
    `DateTime` columns would store local wall time, three hours off what the
    application writes itself. Pinning the zone at connect time fixes all of
    them at once and needs no migration; existing values are never rewritten,
    since some sit inside hashed payloads.
    """
    return create_engine(url, connect_args={"options": "-c TimeZone=UTC"})


engine = make_engine(settings.database_url)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
