"""Migration round-trip test (P1-B).

Creates a throwaway database, runs `alembic upgrade head` -> `downgrade base`
-> `upgrade head` against it, and asserts the spine tables and their PG enum
types are created and fully removed. The upgrade-after-downgrade step is what
catches the autogenerate gap where `drop_table` leaves enum types orphaned.

The throwaway DB is created explicitly so the test never touches the configured
database (whose local default points at the v1 DB). In CI it targets the fresh
Postgres service; either way it cleans up after itself.
"""

import uuid

import psycopg
import pytest
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url

import app.config
from alembic import command

SPINE_TABLES = {
    "users",
    "farmers",
    "honey_batches",
    "audit_log",
    "consent_records",
    "refresh_tokens",
    "idempotency_keys",
}
ENUM_TYPES = {"role", "batch_state", "consent_purpose", "granted_via"}


@pytest.fixture
def migtest_db(monkeypatch):
    """Yield a URL for a freshly created, isolated database; drop it after."""
    base = make_url(app.config.settings.database_url)
    dbname = f"apichain_migtest_{uuid.uuid4().hex[:8]}"
    admin_dsn = base.set(drivername="postgresql", database="postgres").render_as_string(
        hide_password=False
    )
    admin = psycopg.connect(admin_dsn, autocommit=True)
    admin.execute(f'DROP DATABASE IF EXISTS "{dbname}"')
    admin.execute(f'CREATE DATABASE "{dbname}"')

    target = base.set(database=dbname)
    # env.py reads sqlalchemy.url from app.config.settings, so patch there.
    monkeypatch.setattr(
        app.config.settings, "database_url", target.render_as_string(hide_password=False)
    )
    try:
        yield target
    finally:
        admin.execute(f'DROP DATABASE IF EXISTS "{dbname}"')
        admin.close()


def _tables(url) -> set[str]:
    eng = create_engine(url.render_as_string(hide_password=False))
    try:
        return set(inspect(eng).get_table_names())
    finally:
        eng.dispose()


def _enum_types(url) -> set[str]:
    eng = create_engine(url.render_as_string(hide_password=False))
    try:
        with eng.connect() as c:
            rows = c.execute(
                text("select typname from pg_type where typname = any(:names)"),
                {"names": list(ENUM_TYPES)},
            )
            return {r[0] for r in rows}
    finally:
        eng.dispose()


def test_upgrade_creates_spine(migtest_db):
    command.upgrade(Config("alembic.ini"), "head")
    assert SPINE_TABLES <= _tables(migtest_db)
    assert _enum_types(migtest_db) == ENUM_TYPES


def test_downgrade_is_clean_and_upgrade_is_repeatable(migtest_db):
    cfg = Config("alembic.ini")
    command.upgrade(cfg, "head")

    command.downgrade(cfg, "base")
    assert not (SPINE_TABLES & _tables(migtest_db)), "spine tables not fully dropped"
    assert _enum_types(migtest_db) == set(), "enum types orphaned by downgrade"

    # Must not fail on leftover types — proves the downgrade is truly clean.
    command.upgrade(cfg, "head")
    assert SPINE_TABLES <= _tables(migtest_db)
