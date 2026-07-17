"""Engine and session wiring, shape ported from v1 ApiChain--Backend
(app/database.py) and updated to SQLAlchemy 2 style with psycopg 3.

Models declare against Base; Alembic's env.py imports Base.metadata as the
autogenerate target. The v2 schema baseline is authored in Phase 1.
"""

from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import settings


class Base(DeclarativeBase):
    pass


engine = create_engine(settings.database_url)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
