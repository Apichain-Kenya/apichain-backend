"""Contract tests: Schemathesis checks that the running app conforms to its
own OpenAPI schema (status codes, response shapes, content types). This is
guardrail 3 from 05 §6.2, backend side, and it grows automatically as Phase 1
adds real endpoints.

The DB-backed endpoints need a working database, so get_db is pointed at the
migrated throwaway DB (same as the `client` fixture); tables are truncated after
so generated traffic never leaks between tests.
"""

import pytest
import schemathesis
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from app.database import get_db
from app.main import app

_ALL_TABLES = "audit_log, consent_records, refresh_tokens, honey_batches, farmers, users"

schema = schemathesis.openapi.from_asgi("/openapi.json", app)


@pytest.fixture(autouse=True)
def _db_override(migrated_engine):
    testing_session = sessionmaker(bind=migrated_engine, autoflush=False, autocommit=False)

    def _override_get_db():
        s = testing_session()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _override_get_db
    try:
        yield
    finally:
        app.dependency_overrides.clear()
        with migrated_engine.begin() as conn:
            conn.execute(text(f"truncate table {_ALL_TABLES} restart identity cascade"))


@schema.parametrize()
def test_app_conforms_to_its_openapi_schema(case):
    case.call_and_validate()
