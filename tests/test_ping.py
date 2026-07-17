from fastapi.testclient import TestClient

from app.main import app


def test_ping_returns_typed_body():
    client = TestClient(app)
    res = client.get("/v2/ping")
    assert res.status_code == 200
    body = res.json()
    assert body == {"service": "apichain-backend", "version": "2.0.0"}


def test_ping_is_in_openapi_schema():
    """The contract guardrail depends on this endpoint appearing in the schema
    with a typed response, so the generated TS client has something to bind to."""
    schema = app.openapi()
    assert "/v2/ping" in schema["paths"]
    assert "PingResponse" in schema["components"]["schemas"]
