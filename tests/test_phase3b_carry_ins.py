"""P3b-A carry-ins (11 §4): a bounded `Idempotency-Key`, and `batch_code`
out of the two anonymous views.

`batch_code` is client-suppliable as any `SafeStr` on `POST /v2/batches`, so a
name or phone number typed there reached every jar scan. A format rule does
not close that (`0712345678` and `JOHNKAMAU` both match `^[A-Z0-9-]+$`), so the
field leaves the anonymous responses instead (11 D11). The value check below is
the one that matters: a renamed key would pass a key-name check.
"""

from app.enums import Role
from app.main import app
from tests.helpers import (
    METADATA,
    auth,
    create_batch,
    public_path,
    seed_apiary,
    seed_farmer,
    seed_user,
)

# Every mutating endpoint that honours Idempotency-Key.
_IDEMPOTENT_OPERATIONS = [
    ("/v2/farmers", "post"),
    ("/v2/apiaries", "post"),
    ("/v2/batches", "post"),
    ("/v2/batches/{batch_id}/harvest", "post"),
    ("/v2/batches/{batch_id}/process", "post"),
    ("/v2/batches/{batch_id}/lab-verify", "post"),
    ("/v2/batches/{batch_id}/package", "post"),
    ("/v2/batches/{batch_id}/distribute", "post"),
]


def test_every_idempotency_key_header_is_bounded_in_the_contract():
    paths = app.openapi()["paths"]
    for path, method in _IDEMPOTENT_OPERATIONS:
        params = paths[path][method]["parameters"]
        header = next(p for p in params if p["name"] == "Idempotency-Key")
        schema = header["schema"]
        # Optional header: the bound lives on the non-null branch.
        branch = next(s for s in schema.get("anyOf", [schema]) if s.get("type") == "string")
        assert branch["minLength"] == 1, path
        assert branch["maxLength"] == 255, path


def _batch_request(engine, phone: str) -> tuple[dict, dict]:
    farmer = seed_farmer(engine, phone)
    apiary = seed_apiary(engine, farmer)
    op = seed_user(engine, Role.operator, f"idem{phone[-6:]}")
    body = {"farmer_id": farmer, "apiary_id": apiary, "metadata": METADATA}
    return body, auth(op, Role.operator)


def test_an_overlong_idempotency_key_is_refused_at_the_edge(client, migrated_engine):
    body, headers = _batch_request(migrated_engine, "+254700310001")
    r = client.post("/v2/batches", json=body, headers={**headers, "Idempotency-Key": "k" * 256})
    assert r.status_code == 422, r.text


def test_a_255_character_idempotency_key_is_accepted(client, migrated_engine):
    body, headers = _batch_request(migrated_engine, "+254700310002")
    r = client.post("/v2/batches", json=body, headers={**headers, "Idempotency-Key": "k" * 255})
    assert r.status_code == 201, r.text


def test_a_client_batch_code_reaches_neither_anonymous_view(client, migrated_engine):
    leaky = "0712345678"  # what a careless operator types into a code field
    batch = create_batch(client, migrated_engine, phone="+254700310003", batch_code=leaky)
    for view in ("verify", "anchor-proof"):
        r = client.get(public_path(migrated_engine, batch["id"], view))
        assert r.status_code == 200, r.text
        assert "batch_code" not in r.json()
        assert leaky not in r.text


def test_staff_responses_keep_the_batch_code(client, migrated_engine):
    batch = create_batch(client, migrated_engine, phone="+254700310004", batch_code="JAR-0001")
    assert batch["batch_code"] == "JAR-0001"
