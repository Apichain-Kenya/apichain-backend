"""Phase 4b acceptance, backend side (13 §1): the phase's evidence in one file.

The headline walk over HTTP, as the console does it, with no account seeded
behind the API's back except the first one: `create_root_admin` (what
`scripts/create_admin.py` runs) makes the root admin; the admin creates the
staff, each signs in with the password they were given and is made to choose
their own; a field officer enrols the farmer, who does the same; the farmer
registers and harvests; then four distinct people take the four staff steps,
the admin packaging in the operator's place. The record names each of them by
full name with the role they acted in.

The detailed cases live beside their code: `test_phase4b_identity.py`
(accounts, passwords, fingerprints, audit payloads) and
`test_phase4b_reads.py` (farmer reads, filters, `next_action`).
"""

import uuid

from sqlalchemy.orm import Session

from app.services import bootstrap

CLIENT = {"X-ApiChain-Client": "pwa"}
GIVEN = "honey from tetu in march"  # what the admin types for each new person
FARMER_GIVEN = "farmpass9"


def _key() -> dict:
    return {"Idempotency-Key": str(uuid.uuid4())}


def _sign_in(client, identifier: str, password: str) -> dict:
    client.cookies.clear()
    res = client.post("/v2/auth/login", json={"identifier": identifier, "password": password})
    assert res.status_code == 200, res.text
    return {"Authorization": f"Bearer {res.json()['access_token']}"}


def _first_sign_in(client, identifier: str, given: str, own: str) -> dict:
    """Sign in with the password someone else set: everything but `/me` and
    the change is refused until the person chooses their own (D5)."""
    h = _sign_in(client, identifier, given)
    assert client.get("/v2/me", headers=h).json()["must_change_password"] is True
    refused = client.get("/v2/batches", headers=h)
    assert refused.status_code == 403
    assert refused.json()["code"] == "password_change_required"
    res = client.post(
        "/v2/auth/password",
        json={"current_password": given, "new_password": own},
        headers={**h, **CLIENT},
    )
    assert res.status_code == 200, res.text
    # The old password no longer works; the new one does.
    client.cookies.clear()
    old = client.post("/v2/auth/login", json={"identifier": identifier, "password": given})
    assert old.status_code == 401
    return _sign_in(client, identifier, own)


def test_the_phase_4b_headline_walk(client, migrated_engine, boundaries):
    boundaries.geocoder.answer = "Mukurwe-ini, Nyeri, Kenya"

    # The first admin, from the bootstrap script's service.
    with Session(migrated_engine) as s:
        bootstrap.create_root_admin(
            s, username="grace", full_name="Grace Njeri", password="the first admin's own sentence"
        )
        s.commit()
    root = _sign_in(client, "grace", "the first admin's own sentence")

    # The admin creates each person's account; each must change the password.
    people = {
        "otieno": ("Otieno Kiprop", "operator"),
        "akinyi": ("Akinyi Odhiambo", "lab_officer"),
        "peter": ("Peter Mwangi", "admin"),
        "muthoni": ("Muthoni Wairimu", "field_officer"),
    }
    for username, (full_name, role) in people.items():
        res = client.post(
            "/v2/users",
            json={"username": username, "full_name": full_name, "role": role, "password": GIVEN},
            headers={**root, **_key()},
        )
        assert res.status_code == 201, res.text
        assert res.json()["must_change_password"] is True
    h = {
        username: _first_sign_in(client, username, GIVEN, f"{username} chose this long one")
        for username in people
    }

    # A field officer enrols the farmer, with her consent; she too must change.
    enrolled = client.post(
        "/v2/farmers",
        json={
            "first_name": "Wanjiru",
            "last_name": "Kamau",
            "phone": "+254712345312",
            "password": FARMER_GIVEN,
            "consent_granted": True,
            "consent_text_version": "en-2026-10",
        },
        headers={**h["muthoni"], **_key()},
    )
    assert enrolled.status_code == 201, enrolled.text
    farmer_id = enrolled.json()["id"]
    farmer = _first_sign_in(client, "+254712345312", FARMER_GIVEN, "mukurweini26")

    # The farmer registers an apiary, a batch and its harvest (her outbox).
    apiary = client.post(
        "/v2/apiaries",
        json={
            "farmer_id": farmer_id,
            "name": "Ridge apiary",
            "latitude": "-0.428713",
            "longitude": "36.951247",
            "hive_count": 12,
        },
        headers={**farmer, **_key()},
    )
    assert apiary.status_code == 201, apiary.text
    batch = client.post(
        "/v2/batches",
        json={
            "farmer_id": farmer_id,
            "apiary_id": apiary.json()["id"],
            "metadata": {
                "honey_type": "acacia",
                "expected_yield_kg": "35.00",
                "harvest_window_start": "2026-10-14",
                "harvest_window_end": "2026-10-14",
                "apiary_management_method": "organic",
            },
        },
        headers={**farmer, **_key()},
    )
    assert batch.status_code == 201, batch.text
    batch_id = batch.json()["id"]

    steps = (
        ("harvest", farmer, {"harvest_date": "2026-10-14", "quantity_kg": "35.00", "hive_ids": []}),
        ("process", h["otieno"], {"extraction_method": "centrifugal"}),
        ("lab-verify", h["akinyi"], {"moisture_pct": "18.40", "tested_at": "2026-10-16"}),
        ("package", h["peter"], {"unit_count": 120, "jar_ids": []}),
        ("distribute", h["otieno"], {"retailer_name": "Naivas Nyeri"}),
    )
    for route, who, body in steps:
        # Before each step, the record offers it to whoever may take it.
        detail = client.get(f"/v2/batches/{batch_id}", headers=who).json()
        assert detail["next_action"]["allowed"] is True, (route, detail["next_action"])
        res = client.post(f"/v2/batches/{batch_id}/{route}", json=body, headers={**who, **_key()})
        assert res.status_code == 201, (route, res.text)

    final = client.get(f"/v2/batches/{batch_id}", headers=h["otieno"]).json()
    assert final["state"] == "DISTRIBUTED"
    by_action = {s["action"]: (s["actor_display_name"], s["actor_role"]) for s in final["stages"]}
    assert by_action["batch.harvest_recorded"] == ("Wanjiru Kamau", "farmer")
    assert by_action["batch.process_recorded"] == ("Otieno Kiprop", "operator")
    assert by_action["batch.lab_verified"] == ("Akinyi Odhiambo", "lab_officer")
    assert by_action["batch.packaged"] == ("Peter Mwangi", "admin")
    assert by_action["batch.distributed"] == ("Otieno Kiprop", "operator")

    # And a shopper's scan of the jar shows the same journey, no names.
    public = client.get(f"/v2/public/batches/{final['public_id']}/verify")
    assert public.status_code == 200
    assert public.json()["state"] == "DISTRIBUTED"
    assert "Otieno" not in public.text and "+254712345312" not in public.text


def test_next_action_offers_each_step_only_to_its_roles(client, migrated_engine, boundaries):
    """At HARVESTED the operator and the admin are offered processing; the
    lab officer is shown it as someone else's step."""
    with Session(migrated_engine) as s:
        bootstrap.create_root_admin(
            s, username="grace", full_name="Grace Njeri", password="the first admin's own sentence"
        )
        s.commit()
    root = _sign_in(client, "grace", "the first admin's own sentence")
    for username, role in (("otieno", "operator"), ("akinyi", "lab_officer")):
        res = client.post(
            "/v2/users",
            json={
                "username": username,
                "full_name": username.title(),
                "role": role,
                "password": GIVEN,
            },
            headers={**root, **_key()},
        )
        assert res.status_code == 201, res.text
    op = _first_sign_in(client, "otieno", GIVEN, "otieno chose this long one")
    lab = _first_sign_in(client, "akinyi", GIVEN, "akinyi chose this long one")

    enrolled = client.post(
        "/v2/farmers",
        json={
            "first_name": "Wanjiru",
            "last_name": "Kamau",
            "phone": "+254712345313",
            "password": FARMER_GIVEN,
            "consent_granted": True,
            "consent_text_version": "en-2026-10",
        },
        headers={**root, **_key()},
    )
    assert enrolled.status_code == 201, enrolled.text
    farmer_id = enrolled.json()["id"]
    apiary = client.post(
        "/v2/apiaries",
        json={"farmer_id": farmer_id, "name": "Ridge", "latitude": "-0.4", "longitude": "36.9"},
        headers={**root, **_key()},
    )
    batch = client.post(
        "/v2/batches",
        json={
            "farmer_id": farmer_id,
            "apiary_id": apiary.json()["id"],
            "metadata": {
                "honey_type": "acacia",
                "expected_yield_kg": "10.00",
                "harvest_window_start": "2026-10-14",
                "harvest_window_end": "2026-10-14",
                "apiary_management_method": "organic",
            },
        },
        headers={**root, **_key()},
    ).json()
    harvested = client.post(
        f"/v2/batches/{batch['id']}/harvest",
        json={"harvest_date": "2026-10-14", "quantity_kg": "10.00", "hive_ids": []},
        headers={**root, **_key()},
    )
    assert harvested.status_code == 201, harvested.text

    offer = {
        name: client.get(f"/v2/batches/{batch['id']}", headers=who).json()["next_action"]
        for name, who in (("operator", op), ("lab", lab), ("admin", root))
    }
    assert offer["operator"]["allowed"] is True
    assert offer["admin"]["allowed"] is True
    assert offer["lab"]["allowed"] is False
    # Refused at the server too, not only hidden by the console.
    refused = client.post(f"/v2/batches/{batch['id']}/process", json={}, headers={**lab, **_key()})
    assert refused.status_code == 403
