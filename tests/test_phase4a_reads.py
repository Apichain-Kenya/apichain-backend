"""P4a-B: the read endpoints the Phase 4a client needs, a place name for the
consumer view, and the consumer fixtures (12 §6, D2/D12/D17).

Every read a farmer can reach is checked from the other side: farmer A's
token against farmer B's data must be refused and return nothing. The v1
farm-details bug was exactly a read with no such check.
"""

import json
import os
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.enums import Role
from app.models import ApiaryLocation, HarvestRecord, MerkleAnchor
from tests.helpers import METADATA, auth, public_path, seed_apiary, seed_farmer, seed_user
from tests.test_anchor_proof_endpoint import _anchor_everything
from tests.test_lab_verify import CLEAN_PANEL
from tests.test_package_distribute import DISTRIBUTE, PACKAGE
from tests.test_transitions_endpoints import HARVEST, PROCESS

FIXTURES = Path(__file__).parent / "fixtures" / "verify"


def _farmer(engine, phone, username):
    farmer_id = seed_farmer(engine, phone)
    headers = auth(seed_user(engine, Role.farmer, username, farmer_id=farmer_id), Role.farmer)
    return farmer_id, headers


def _staff(engine, role, username):
    return auth(seed_user(engine, role, username), role)


def _batch(client, farmer_id, apiary_id, headers):
    r = client.post(
        "/v2/batches",
        json={"farmer_id": farmer_id, "apiary_id": apiary_id, "metadata": METADATA},
        headers=headers,
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


# --- GET /v2/apiaries ---------------------------------------------------------


def test_a_farmer_lists_only_their_own_apiaries(client, migrated_engine):
    a, a_hdr = _farmer(migrated_engine, "+254700040001", "f-a1")
    b, _ = _farmer(migrated_engine, "+254700040002", "f-b1")
    seed_apiary(migrated_engine, a, name="Ridge")
    seed_apiary(migrated_engine, b, name="Valley")

    res = client.get(f"/v2/apiaries?farmer_id={a}", headers=a_hdr)
    assert res.status_code == 200, res.text
    assert [x["name"] for x in res.json()] == ["Ridge"]
    # The owner sees the exact point (03 §8.1).
    assert res.json()[0]["latitude"] == "-1.286389"


def test_a_farmer_cannot_list_another_farmers_apiaries(client, migrated_engine):
    a, a_hdr = _farmer(migrated_engine, "+254700040003", "f-a2")
    b, _ = _farmer(migrated_engine, "+254700040004", "f-b2")
    seed_apiary(migrated_engine, b, name="Valley")

    res = client.get(f"/v2/apiaries?farmer_id={b}", headers=a_hdr)
    assert res.status_code == 403
    assert "Valley" not in res.text


def test_a_field_officer_lists_a_farmers_apiaries(client, migrated_engine):
    a, _ = _farmer(migrated_engine, "+254700040005", "f-a3")
    seed_apiary(migrated_engine, a, name="Ridge")
    officer = _staff(migrated_engine, Role.field_officer, "officer-3")

    res = client.get(f"/v2/apiaries?farmer_id={a}", headers=officer)
    assert res.status_code == 200
    assert [x["name"] for x in res.json()] == ["Ridge"]


def test_an_operator_cannot_read_apiaries(client, migrated_engine):
    a, _ = _farmer(migrated_engine, "+254700040006", "f-a4")
    seed_apiary(migrated_engine, a)
    operator = _staff(migrated_engine, Role.operator, "op-4")
    assert client.get(f"/v2/apiaries?farmer_id={a}", headers=operator).status_code == 403


def test_staff_must_name_the_farmer(client, migrated_engine):
    officer = _staff(migrated_engine, Role.field_officer, "officer-5")
    res = client.get("/v2/apiaries", headers=officer)
    assert res.status_code == 422
    assert res.json()["code"] == "farmer_id_required"


# --- the place name (D12) -----------------------------------------------------


def test_creating_an_apiary_stores_a_locality_from_rounded_coordinates(
    client, migrated_engine, boundaries
):
    a, a_hdr = _farmer(migrated_engine, "+254700040007", "f-a6")
    boundaries.geocoder.answer = "Nyeri, Central, Kenya"

    res = client.post(
        "/v2/apiaries",
        json={"farmer_id": a, "name": "Ridge", "latitude": "-0.428713", "longitude": "36.951247"},
        headers=a_hdr,
    )
    assert res.status_code == 201, res.text
    assert res.json()["locality"] == "Nyeri, Central, Kenya"
    # The exact hive point never leaves our system: the lookup got 2 dp.
    assert boundaries.geocoder.calls == [(Decimal("-0.43"), Decimal("36.95"))]


def test_a_failed_lookup_still_creates_the_apiary(client, migrated_engine, boundaries):
    a, a_hdr = _farmer(migrated_engine, "+254700040008", "f-a7")
    boundaries.geocoder.answer = None

    res = client.post(
        "/v2/apiaries",
        json={"farmer_id": a, "name": "Ridge", "latitude": "-0.4", "longitude": "36.9"},
        headers=a_hdr,
    )
    assert res.status_code == 201
    assert res.json()["locality"] is None


def test_the_live_geocoder_rounds_and_names_itself(monkeypatch):
    import app.services.geocode as g

    seen = {}

    class _R:
        status_code = 200

        def json(self):
            return {"address": {"county": "Nyeri", "country": "Kenya"}}

    def _get(url, params, headers, timeout):
        seen.update(params=params, headers=headers, timeout=timeout)
        return _R()

    monkeypatch.setattr(g.requests, "get", _get)
    assert g.reverse_geocode(Decimal("-0.428713"), Decimal("36.951247")) == "Nyeri, Kenya"
    assert seen["params"]["lat"] == "-0.43" and seen["params"]["lon"] == "36.95"
    assert "AgriScanAI" not in seen["headers"]["User-Agent"]
    assert seen["headers"]["User-Agent"].startswith("ApiChainKenya/2")
    assert seen["timeout"] <= 3


def test_verify_shows_the_place_and_never_coordinates_in_it(client, migrated_engine):
    a, a_hdr = _farmer(migrated_engine, "+254700040009", "f-a8")
    apiary = seed_apiary(migrated_engine, a)
    with Session(migrated_engine) as s:
        s.execute(
            update(ApiaryLocation)
            .where(ApiaryLocation.id == apiary)
            .values(locality="Nyeri, Central, Kenya")
        )
        s.commit()
    batch_id = _batch(client, a, apiary, a_hdr)

    body = client.get(public_path(migrated_engine, batch_id, "verify")).json()
    assert body["place"] == "Nyeri, Central, Kenya"


# --- GET /v2/batches ----------------------------------------------------------


def test_a_farmer_lists_only_their_own_batches(client, migrated_engine):
    a, a_hdr = _farmer(migrated_engine, "+254700040010", "f-a9")
    b, b_hdr = _farmer(migrated_engine, "+254700040011", "f-b9")
    mine = _batch(client, a, seed_apiary(migrated_engine, a), a_hdr)
    _batch(client, b, seed_apiary(migrated_engine, b), b_hdr)

    res = client.get("/v2/batches", headers=a_hdr)
    assert res.status_code == 200, res.text
    items = res.json()["items"]
    assert [i["id"] for i in items] == [mine]
    assert items[0]["honey_type"] == METADATA["honey_type"]
    assert items[0]["apiary_name"] == "Ridge apiary"
    assert items[0]["state"] == "CREATED"


def test_a_farmer_cannot_list_another_farmers_batches(client, migrated_engine):
    a, a_hdr = _farmer(migrated_engine, "+254700040012", "f-a10")
    b, b_hdr = _farmer(migrated_engine, "+254700040013", "f-b10")
    _batch(client, b, seed_apiary(migrated_engine, b), b_hdr)

    res = client.get(f"/v2/batches?farmer_id={b}", headers=a_hdr)
    assert res.status_code == 403


def test_staff_list_every_batch_and_filter_by_state(client, migrated_engine):
    a, a_hdr = _farmer(migrated_engine, "+254700040014", "f-a11")
    apiary = seed_apiary(migrated_engine, a)
    first = _batch(client, a, apiary, a_hdr)
    second = _batch(client, a, apiary, a_hdr)
    assert (
        client.post(f"/v2/batches/{second}/harvest", json=HARVEST, headers=a_hdr).status_code == 201
    )
    operator = _staff(migrated_engine, Role.operator, "op-11")

    every = client.get("/v2/batches", headers=operator).json()["items"]
    assert {i["id"] for i in every} == {first, second}
    harvested = client.get("/v2/batches?state=HARVESTED", headers=operator).json()["items"]
    assert [i["id"] for i in harvested] == [second]


def test_batches_paginate_newest_first_without_overlap(client, migrated_engine):
    a, a_hdr = _farmer(migrated_engine, "+254700040015", "f-a12")
    apiary = seed_apiary(migrated_engine, a)
    ids = [_batch(client, a, apiary, a_hdr) for _ in range(3)]

    page1 = client.get("/v2/batches?limit=2", headers=a_hdr).json()
    assert [i["id"] for i in page1["items"]] == [ids[2], ids[1]]
    assert page1["next_cursor"]
    _batch(client, a, apiary, a_hdr)  # a new batch must not shift page 2
    page2 = client.get(f"/v2/batches?limit=2&cursor={page1['next_cursor']}", headers=a_hdr).json()
    assert [i["id"] for i in page2["items"]] == [ids[0]]
    assert page2["next_cursor"] is None


def test_the_page_size_is_bounded(client, migrated_engine):
    operator = _staff(migrated_engine, Role.operator, "op-13")
    assert client.get("/v2/batches?limit=101", headers=operator).status_code == 422


# --- GET /v2/batches/{id} -----------------------------------------------------


def test_the_owner_reads_a_batch_with_who_acted_at_each_step(client, migrated_engine):
    a, a_hdr = _farmer(migrated_engine, "+254700040016", "f-a14")
    batch_id = _batch(client, a, seed_apiary(migrated_engine, a), a_hdr)
    client.post(f"/v2/batches/{batch_id}/harvest", json=HARVEST, headers=a_hdr)

    res = client.get(f"/v2/batches/{batch_id}", headers=a_hdr)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["state"] == "HARVESTED"
    assert body["metadata"]["honey_type"] == METADATA["honey_type"]
    assert body["apiary"]["latitude"] == "-1.286389"
    actions = [s["action"] for s in body["stages"]]
    assert actions[0] == "batch.created" and actions[-1] == "batch.harvest_recorded"
    assert {s["actor_role"] for s in body["stages"]} == {"farmer"}
    assert body["stages"][0]["actor_display_name"] == "A B"
    assert all(s["anchor_status"] == "pending" for s in body["stages"])


def test_a_farmer_cannot_read_another_farmers_batch(client, migrated_engine):
    a, a_hdr = _farmer(migrated_engine, "+254700040017", "f-a15")
    b, b_hdr = _farmer(migrated_engine, "+254700040018", "f-b15")
    theirs = _batch(client, b, seed_apiary(migrated_engine, b), b_hdr)

    res = client.get(f"/v2/batches/{theirs}", headers=a_hdr)
    assert res.status_code == 403
    assert "Ridge apiary" not in res.text


def test_an_operator_reads_a_batch_without_the_hive_location(client, migrated_engine):
    a, a_hdr = _farmer(migrated_engine, "+254700040019", "f-a16")
    batch_id = _batch(client, a, seed_apiary(migrated_engine, a), a_hdr)
    operator = _staff(migrated_engine, Role.operator, "op-16")

    body = client.get(f"/v2/batches/{batch_id}", headers=operator).json()
    assert body["apiary"]["name"] == "Ridge apiary"
    assert body["apiary"]["latitude"] is None and body["apiary"]["longitude"] is None
    assert body["apiary"]["location_withheld"] is True
    assert "-1.286389" not in json.dumps(body)


def test_a_missing_batch_is_404(client, migrated_engine):
    operator = _staff(migrated_engine, Role.operator, "op-17")
    assert client.get("/v2/batches/999999", headers=operator).status_code == 404


# --- consumer fixtures for the client (P4a-F) ---------------------------------


def _walk(client, engine, n, upto):
    farmer_id, farmer = _farmer(engine, f"+2547000500{n:02d}", f"fx-farmer-{n}")
    apiary = seed_apiary(engine, farmer_id)
    with Session(engine) as s:
        s.execute(
            update(ApiaryLocation)
            .where(ApiaryLocation.id == apiary)
            .values(locality="Nyeri, Kenya")
        )
        s.commit()
    operator = _staff(engine, Role.operator, f"fx-op-{n}")
    lab = _staff(engine, Role.lab_officer, f"fx-lab-{n}")
    batch_id = _batch(client, farmer_id, apiary, farmer)
    steps = [
        ("harvest", HARVEST, farmer),
        ("process", PROCESS, operator),
        ("lab-verify", CLEAN_PANEL, lab),
        ("package", PACKAGE, operator),
        ("distribute", DISTRIBUTE, operator),
    ][:upto]
    for stage, body, headers in steps:
        assert (
            client.post(f"/v2/batches/{batch_id}/{stage}", json=body, headers=headers).status_code
            == 201
        )
    return batch_id


def test_consumer_fixtures_cover_the_four_trust_states(client, migrated_engine):
    """Writes real `/verify` responses for the client's trust-state tests, so
    its mapping is tested against what the server actually sends."""
    sealing = _walk(client, migrated_engine, 1, 5)  # DISTRIBUTED, not anchored
    in_progress = _walk(client, migrated_engine, 2, 4)  # PACKAGED
    sealed = _walk(client, migrated_engine, 3, 5)
    mismatch = _walk(client, migrated_engine, 4, 5)

    responses = {"sealing": client.get(public_path(migrated_engine, sealing, "verify")).json()}

    _anchor_everything(migrated_engine)
    with Session(migrated_engine) as s:
        s.execute(update(MerkleAnchor).values(verified_at=datetime.now(UTC).replace(tzinfo=None)))
        s.execute(
            update(HarvestRecord)
            .where(HarvestRecord.batch_id == mismatch)
            .values(quantity_kg=Decimal("999.00"))
        )
        s.commit()
    responses["in_progress"] = client.get(
        public_path(migrated_engine, in_progress, "verify")
    ).json()
    responses["sealed"] = client.get(public_path(migrated_engine, sealed, "verify")).json()
    responses["mismatch"] = client.get(public_path(migrated_engine, mismatch, "verify")).json()

    assert responses["sealing"]["state"] == "DISTRIBUTED"
    assert responses["sealing"]["anchor_status"] == "pending"
    assert responses["in_progress"]["state"] == "PACKAGED"
    assert responses["sealed"]["anchor_status"] == "confirmed"
    assert all(b is None or b["match"] for b in responses["sealed"]["verification"].values())
    assert responses["mismatch"]["verification"]["harvest"]["match"] is False

    # Ids, hashes and timestamps differ every run, so the files are rewritten
    # only on request: `APICHAIN_WRITE_FIXTURES=1 uv run pytest -k consumer_fixtures`.
    if os.environ.get("APICHAIN_WRITE_FIXTURES") != "1":
        return
    FIXTURES.mkdir(parents=True, exist_ok=True)
    for name, body in responses.items():
        (FIXTURES / f"{name}.json").write_text(
            json.dumps(body, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n"
        )


def test_verify_blocks_carry_the_day_each_stage_was_recorded(client, migrated_engine):
    """The consumer journey shows a date per step (12 §10). A day, not a
    timestamp: the shopper needs "harvested 9 Mar", nothing finer."""
    a, a_hdr = _farmer(migrated_engine, "+254700040020", "f-a17")
    batch_id = _batch(client, a, seed_apiary(migrated_engine, a), a_hdr)
    client.post(f"/v2/batches/{batch_id}/harvest", json=HARVEST, headers=a_hdr)

    body = client.get(public_path(migrated_engine, batch_id, "verify")).json()
    today = datetime.now(UTC).date().isoformat()
    assert body["verification"]["harvest"]["recorded_on"] == today
    assert body["verification"]["metadata"]["recorded_on"] == today
    assert body["verification"]["process"] is None
