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
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

import app.config
from alembic import command

# Spine tables (child-first) for truncate cleanup between endpoint tests.
# Every new table must be added here AND in test_contract.py, or rows leak
# between tests in ways that only show up in a full run.
_ALL_TABLES = (
    "verification_codes, communications, documents, "
    "merkle_anchor, audit_log, consent_records, refresh_tokens, idempotency_keys, "
    "rate_limit_events, "
    "codex_conformance, distribution_records, packaging_records, lab_results, "
    "process_records, harvest_records, batch_metadata, apiary_records, "
    "apiary_locations, honey_batches, farmers, users"
)


@pytest.fixture(autouse=True, scope="session")
def _disable_scheduler():
    """Keep the background integrity scheduler off during tests (P1-G)."""
    app.config.settings.scheduler_enabled = False
    # No test reaches MinIO, ClamAV, Africa's Talking or SMTP (11 D4). Tests
    # that need to inspect a boundary inject their own fake.
    app.config.settings.storage_backend = "fake"
    app.config.settings.scanner_backend = "fake"
    app.config.settings.sms_backend = "fake"
    app.config.settings.email_backend = "fake"
    app.config.settings.geocoder_backend = "none"
    yield


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

    # The same connection settings the app uses, the UTC pin included (14 D20).
    from app.database import make_engine

    engine = make_engine(url)
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


class Boundaries:
    """The four external boundaries as fakes a test can inspect (11 D4)."""

    def __init__(self) -> None:
        from tests.fakes import (
            FakeObjectStore,
            FakeScanner,
            RecordingEmail,
            RecordingGeocoder,
            RecordingSms,
        )

        self.store = FakeObjectStore()
        self.scanner = FakeScanner()
        self.sms = RecordingSms()
        self.email = RecordingEmail()
        self.geocoder = RecordingGeocoder()


@pytest.fixture
def boundaries():
    return Boundaries()


class _PwaLikeClient(TestClient):
    """A TestClient that behaves like the Phase 4 PWA (12 D7).

    The PWA always sends an `Idempotency-Key` on a write, so this client adds
    a fresh one to any POST that does not set its own. Tests that exercise
    replay set the header explicitly and are untouched. The base URL is https
    so the `Secure` refresh cookie round-trips (12 D3). A test that needs a
    write *without* a key builds a bare `TestClient`.
    """

    def request(self, method, url, *args, **kwargs):  # type: ignore[override]
        if method.upper() == "POST":
            headers = dict(kwargs.get("headers") or {})
            if not any(k.lower() == "idempotency-key" for k in headers):
                headers["Idempotency-Key"] = str(uuid.uuid4())
            kwargs["headers"] = headers
        return super().request(method, url, *args, **kwargs)


@pytest.fixture
def client(migrated_engine, boundaries):
    """TestClient with get_db bound to the migrated DB (endpoints commit for
    real); all spine tables are truncated after each test for isolation."""
    from app.database import get_db
    from app.main import app
    from app.routers._boundaries import (
        get_email,
        get_geocoder,
        get_object_store,
        get_scanner,
        get_sms,
    )

    testing_session = sessionmaker(bind=migrated_engine, autoflush=False, autocommit=False)

    def _override_get_db():
        s = testing_session()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_object_store] = lambda: boundaries.store
    app.dependency_overrides[get_scanner] = lambda: boundaries.scanner
    app.dependency_overrides[get_sms] = lambda: boundaries.sms
    app.dependency_overrides[get_email] = lambda: boundaries.email
    app.dependency_overrides[get_geocoder] = lambda: boundaries.geocoder
    try:
        yield _PwaLikeClient(app, base_url="https://testserver")
    finally:
        app.dependency_overrides.clear()
        with migrated_engine.begin() as conn:
            conn.execute(text(f"truncate table {_ALL_TABLES} restart identity cascade"))
