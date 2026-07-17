"""Contract tests: Schemathesis checks that the running app conforms to its
own OpenAPI schema (status codes, response shapes, content types). This is
guardrail 3 from 05 §6.2, backend side, and it grows automatically as Phase 1
adds real endpoints.
"""

import schemathesis

from app.main import app

schema = schemathesis.openapi.from_asgi("/openapi.json", app)


@schema.parametrize()
def test_app_conforms_to_its_openapi_schema(case):
    case.call_and_validate()
