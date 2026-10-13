"""Phase 4a acceptance, backend side (12 §1): the phase's evidence in one file.

Walks what the Phase 4a client does, over HTTP, as the client does it: a
farmer signs in and gets only a cookie for the refresh token, restores a
session from that cookie, survives a lost refresh reply, registers an apiary
and a harvest with the keys the outbox mints (a replay changes nothing), reads
them back, and a consumer sees a place name and no coordinates.

The same walk ran live against the compose stack (real PostGIS, MinIO, ClamAV,
Mailpit and Nominatim) on 2026-10-13: 23/23 checks
(`scripts/live_walk.py`).
"""

import uuid

from sqlalchemy.orm import Session

from app.enums import Role
from app.models import HoneyBatch
from tests.helpers import public_path, seed_farmer, seed_user

COOKIE = "__Secure-apichain_rt"
PWA = {"X-ApiChain-Client": "pwa"}


def _cookie(res):
    return res.cookies[COOKIE]


def test_the_phase_4a_client_walk(client, migrated_engine, boundaries):
    farmer_id = seed_farmer(migrated_engine, "+254700090001")
    seed_user(migrated_engine, Role.farmer, "accept-4a", password="farm-pw", farmer_id=farmer_id)
    boundaries.geocoder.answer = "Nyeri Central, Nyeri, Kenya"

    # Sign in: the refresh token is a cookie, never in the body.
    login = client.post("/v2/auth/login", json={"identifier": "accept-4a", "password": "farm-pw"})
    assert login.status_code == 200
    assert "refresh_token" not in login.json()
    first = _cookie(login)

    # A reload: the session comes back from the cookie alone.
    client.cookies.clear()
    restored = client.post("/v2/auth/refresh", headers=PWA, cookies={COOKIE: first})
    assert restored.status_code == 200
    # ...and that reply is lost on 2G: the old cookie again is forgiven (D5).
    client.cookies.clear()
    retry = client.post("/v2/auth/refresh", headers=PWA, cookies={COOKIE: first})
    assert retry.status_code == 200
    token = retry.json()["access_token"]
    auth = {"Authorization": f"Bearer {token}"}

    me = client.get("/v2/me", headers=auth).json()
    assert me["farmer_id"] == farmer_id

    # The outbox's three linked actions, each with its own key.
    keys = [str(uuid.uuid4()) for _ in range(3)]
    apiary = client.post(
        "/v2/apiaries",
        json={
            "farmer_id": farmer_id,
            "name": "Ridge",
            "latitude": "-0.428713",
            "longitude": "36.951247",
            "hive_count": 6,
        },
        headers={**auth, "Idempotency-Key": keys[0]},
    )
    assert apiary.status_code == 201
    assert apiary.json()["locality"] == "Nyeri Central, Nyeri, Kenya"
    batch_body = {
        "farmer_id": farmer_id,
        "apiary_id": apiary.json()["id"],
        "metadata": {
            "honey_type": "acacia",
            "expected_yield_kg": "28.00",
            "harvest_window_start": "2026-10-13",
            "harvest_window_end": "2026-10-13",
            "apiary_management_method": "organic",
        },
    }
    batch = client.post(
        "/v2/batches", json=batch_body, headers={**auth, "Idempotency-Key": keys[1]}
    )
    replay = client.post(
        "/v2/batches", json=batch_body, headers={**auth, "Idempotency-Key": keys[1]}
    )
    assert batch.status_code == 201
    assert replay.json()["id"] == batch.json()["id"]
    batch_id = batch.json()["id"]
    harvest = client.post(
        f"/v2/batches/{batch_id}/harvest",
        json={"harvest_date": "2026-10-13", "quantity_kg": "35.00", "hive_ids": []},
        headers={**auth, "Idempotency-Key": keys[2]},
    )
    assert harvest.status_code == 201
    with Session(migrated_engine) as s:
        assert s.query(HoneyBatch).count() == 1

    # Reads for "My harvests" and the apiary picker.
    listed = client.get("/v2/batches", headers=auth).json()["items"]
    assert [b["id"] for b in listed] == [batch_id]
    assert listed[0]["state"] == "HARVESTED"
    detail = client.get(f"/v2/batches/{batch_id}", headers=auth).json()
    assert {s["actor_role"] for s in detail["stages"]} == {"farmer"}
    assert [
        a["name"] for a in client.get(f"/v2/apiaries?farmer_id={farmer_id}", headers=auth).json()
    ] == ["Ridge"]

    # The jar: a place, a dated journey, no hive coordinates.
    verify = client.get(public_path(migrated_engine, batch_id, "verify"))
    body = verify.json()
    assert body["place"] == "Nyeri Central, Nyeri, Kenya"
    assert body["verification"]["harvest"]["recorded_on"] is not None
    assert "0.428713" not in verify.text
