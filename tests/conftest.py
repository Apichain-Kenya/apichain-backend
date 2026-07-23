"""Shared DB test fixtures.

`migrated_engine` (session-scoped) spins up an isolated throwaway database,
runs `alembic upgrade head` against it, and drops it at the end — so DB-backed
tests never touch the configured database (whose local default points at the
v1 DB). `db` (function-scoped) hands each test a session wrapped in a
transaction that is rolled back afterward, keeping tests independent.
"""

import uuid

import psycopg
import pytest
from alembic.config import Config
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

import app.config
from alembic import command


@pytest.fixture(scope="session")
def migrated_engine():
    base = make_url(app.config.settings.database_url)
    dbname = f"apichain_test_{uuid.uuid4().hex[:8]}"
    admin_dsn = base.set(drivername="postgresql", database="postgres").render_as_string(
        hide_password=False
    )
    admin = psycopg.connect(admin_dsn, autocommit=True)
    admin.execute(f'DROP DATABASE IF EXISTS "{dbname}"')
    admin.execute(f'CREATE DATABASE "{dbname}"')

    url = base.set(database=dbname).render_as_string(hide_password=False)
    # env.py reads sqlalchemy.url from settings, so point it at the throwaway db
    # for the migration run, then restore.
    original = app.config.settings.database_url
    app.config.settings.database_url = url
    try:
        command.upgrade(Config("alembic.ini"), "head")
    finally:
        app.config.settings.database_url = original

    engine = create_engine(url)
    try:
        yield engine
    finally:
        engine.dispose()
        admin.execute(f'DROP DATABASE IF EXISTS "{dbname}"')
        admin.close()


@pytest.fixture
def db(migrated_engine):
    conn = migrated_engine.connect()
    trans = conn.begin()
    session = Session(bind=conn)
    try:
        yield session
    finally:
        session.close()
        trans.rollback()
        conn.close()
