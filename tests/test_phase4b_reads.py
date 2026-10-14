"""Phase 4b, P4b-B: the reads the console needs (13 §6, D7, D10).

Every farmer-reachable read is checked from the other side: farmer A's token
against farmer B's record is refused and returns nothing. Operators and lab
officers never read a farmer's contact details (D7).
"""

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from app.enums import BatchState, Role
from app.models import Farmer
from app.routers.batches import _ACTION_FOR
from app.services.transitions import LEGAL
from tests.helpers import METADATA, auth, seed_apiary, seed_user
from tests.test_lab_verify import CLEAN_PANEL
from tests.test_package_distribute import DISTRIBUTE, PACKAGE
from tests.test_transitions_endpoints import HARVEST, PROCESS

FIXTURES = Path(__file__).parent / "fixtures" / "console"


def _farmer(
    engine,
    first: str,
    last: str,
    phone: str,
    *,
    enrolled_by: int | None = None,
    email: str | None = None,
    phone_verified: bool = False,
) -> int:
    with Session(engine) as s:
        farmer = Farmer(
            first_name=first,
            last_name=last,
            phone=phone,
            email=email,
            number_of_hives=14,
            enrolled_by=enrolled_by,
            phone_verified_at=datetime.now(UTC).replace(tzinfo=None) if phone_verified else None,
        )
        s.add(farmer)
        s.commit()
        return farmer.id


def _farmer_login(engine, farmer_id: int, username: str) -> dict[str, str]:
    return auth(seed_user(engine, Role.farmer, username, farmer_id=farmer_id), Role.farmer)


def _staff(engine, role: Role, username: str, full_name: str | None = None) -> dict[str, str]:
    uid = seed_user(engine, role, username)
    if full_name:
        with Session(engine) as s:
            from app.models import User

            s.get(User, uid).full_name = full_name
            s.commit()
    return auth(uid, role)


def _batch(client, farmer_id: int, headers: dict[str, str], engine) -> dict:
    apiary = seed_apiary(engine, farmer_id)
    res = client.post(
        "/v2/batches",
        json={"farmer_id": farmer_id, "apiary_id": apiary, "metadata": METADATA},
        headers=headers,
    )
    assert res.status_code == 201, res.text
    return res.json()


# --- GET /v2/farmers -----------------------------------------------------------


def test_field_officers_and_admins_list_every_farmer(client, migrated_engine):
    officer_id = seed_user(migrated_engine, Role.field_officer, "fo-list")
    with Session(migrated_engine) as s:
        from app.models import User

        s.get(User, officer_id).full_name = "Mutua Kioko"
        s.commit()
    a = _farmer(
        migrated_engine,
        "Wanjiru",
        "Kamau",
        "+254700050001",
        enrolled_by=officer_id,
        phone_verified=True,
    )
    b = _farmer(migrated_engine, "Mwende", "Musyoka", "+254700050002")
    for headers in (
        auth(officer_id, Role.field_officer),
        _staff(migrated_engine, Role.admin, "ad-list"),
    ):
        res = client.get("/v2/farmers", headers=headers)
        assert res.status_code == 200, res.text
        items = res.json()["items"]
        assert [f["id"] for f in items] == [b, a]  # newest first, not only "mine" (D7)
        wanjiru = items[1]
        assert wanjiru["display_name"] == "Wanjiru Kamau"
        assert wanjiru["phone"] == "+254700050001"
        assert wanjiru["phone_verified"] is True
        assert wanjiru["email_verified"] is False
        assert wanjiru["number_of_hives"] == 14
        assert wanjiru["enrolled_by_display_name"] == "Mutua Kioko"
        assert items[0]["enrolled_by_display_name"] is None


@pytest.mark.parametrize("role", [Role.operator, Role.lab_officer])
def test_operators_and_lab_officers_never_list_farmers(client, migrated_engine, role):
    _farmer(migrated_engine, "Wanjiru", "Kamau", "+254700050003")
    res = client.get("/v2/farmers", headers=_staff(migrated_engine, role, f"nolist-{role}"))
    assert res.status_code == 403
    assert "Wanjiru" not in res.text


def test_a_farmer_cannot_list_farmers(client, migrated_engine):
    a = _farmer(migrated_engine, "Wanjiru", "Kamau", "+254700050004")
    _farmer(migrated_engine, "Mwende", "Musyoka", "+254700050005")
    res = client.get("/v2/farmers", headers=_farmer_login(migrated_engine, a, "f-nolist"))
    assert res.status_code == 403
    assert "Musyoka" not in res.text


def test_farmer_search_by_name_prefix_or_phone_suffix(client, migrated_engine):
    a = _farmer(migrated_engine, "Wanjiru", "Kamau", "+254700050006")
    b = _farmer(migrated_engine, "Mwende", "Musyoka", "+254700050312")
    c = _farmer(migrated_engine, "Kiprono", "Chebet", "+254700050008")
    h = _staff(migrated_engine, Role.field_officer, "fo-search")

    def ids(q: str) -> list[int]:
        res = client.get("/v2/farmers", params={"q": q}, headers=h)
        assert res.status_code == 200, res.text
        return [f["id"] for f in res.json()["items"]]

    assert ids("wan") == [a]  # first name, case-insensitive
    assert ids("MUS") == [b]  # last name
    assert ids("wanjiru kam") == [a]  # full name
    assert ids("312") == [b]  # phone suffix
    assert ids("chebet") == [c]
    assert ids("%") == []  # a wildcard is a character, not a pattern
    assert ids("_") == []


def test_farmer_list_pages_by_keyset(client, migrated_engine):
    made = [_farmer(migrated_engine, f"F{i}", "Page", f"+25470005010{i}") for i in range(5)]
    h = _staff(migrated_engine, Role.admin, "ad-page")
    first = client.get("/v2/farmers?limit=2", headers=h).json()
    second = client.get(f"/v2/farmers?limit=2&cursor={first['next_cursor']}", headers=h).json()
    third = client.get(f"/v2/farmers?limit=2&cursor={second['next_cursor']}", headers=h).json()
    seen = [f["id"] for f in first["items"] + second["items"] + third["items"]]
    assert seen == sorted(made, reverse=True)
    assert third["next_cursor"] is None


# --- GET /v2/farmers/{id} ------------------------------------------------------


def test_farmer_profile_for_staff_carries_contact_and_counts(client, migrated_engine):
    a = _farmer(
        migrated_engine,
        "Wanjiru",
        "Kamau",
        "+254700050020",
        email="w@example.test",
        phone_verified=True,
    )
    seed_apiary(migrated_engine, a)
    res = client.get(
        f"/v2/farmers/{a}", headers=_staff(migrated_engine, Role.field_officer, "fo-p")
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["display_name"] == "Wanjiru Kamau"
    assert body["email"] == "w@example.test"
    assert body["has_email"] is True
    assert body["phone_verified_at"] is not None
    assert body["email_verified_at"] is None
    assert body["apiaries_count"] == 1
    assert body["documents_count"] == 0


def test_a_farmer_reads_their_own_profile(client, migrated_engine):
    a = _farmer(migrated_engine, "Wanjiru", "Kamau", "+254700050021")
    res = client.get(f"/v2/farmers/{a}", headers=_farmer_login(migrated_engine, a, "f-own"))
    assert res.status_code == 200
    assert res.json()["id"] == a


def test_farmer_a_cannot_read_farmer_b(client, migrated_engine):
    a = _farmer(migrated_engine, "Wanjiru", "Kamau", "+254700050022")
    b = _farmer(migrated_engine, "Mwende", "Musyoka", "+254700050023")
    res = client.get(f"/v2/farmers/{b}", headers=_farmer_login(migrated_engine, a, "f-snoop"))
    assert res.status_code == 403
    assert "Musyoka" not in res.text and "+254700050023" not in res.text


@pytest.mark.parametrize("role", [Role.operator, Role.lab_officer])
def test_operators_and_lab_officers_never_read_a_profile(client, migrated_engine, role):
    a = _farmer(migrated_engine, "Wanjiru", "Kamau", "+254700050024")
    res = client.get(f"/v2/farmers/{a}", headers=_staff(migrated_engine, role, f"nop-{role}"))
    assert res.status_code == 403
    assert "+254700050024" not in res.text


def test_an_unknown_farmer_is_404(client, migrated_engine):
    res = client.get("/v2/farmers/999999", headers=_staff(migrated_engine, Role.admin, "ad-404"))
    assert res.status_code == 404


# --- The batch list: work queues and search ---------------------------------------


def _queue(client, engine) -> dict[str, int]:
    """Three batches in three states."""
    a = _farmer(engine, "Wanjiru", "Kamau", "+254700050030")
    owner = _farmer_login(engine, a, "f-queue")
    op = _staff(engine, Role.operator, "op-queue")
    lab = _staff(engine, Role.lab_officer, "lab-queue")
    made: dict[str, int] = {}
    for name, steps in (
        ("created", []),
        ("harvested", [("harvest", HARVEST, owner)]),
        (
            "lab_verified",
            [
                ("harvest", HARVEST, owner),
                ("process", PROCESS, op),
                ("lab-verify", CLEAN_PANEL, lab),
            ],
        ),
    ):
        batch = _batch(client, a, owner, engine)
        for stage, body, headers in steps:
            res = client.post(f"/v2/batches/{batch['id']}/{stage}", json=body, headers=headers)
            assert res.status_code == 201, res.text
        made[name] = batch["id"]
    return made


def test_state_is_repeatable_for_a_work_queue(client, migrated_engine):
    made = _queue(client, migrated_engine)
    h = _staff(migrated_engine, Role.operator, "op-q2")
    res = client.get("/v2/batches?state=HARVESTED&state=LAB_VERIFIED", headers=h)
    assert res.status_code == 200, res.text
    assert [b["id"] for b in res.json()["items"]] == [made["lab_verified"], made["harvested"]]
    one = client.get("/v2/batches?state=CREATED", headers=h).json()["items"]
    assert [b["id"] for b in one] == [made["created"]]


def test_batch_search_by_code_prefix_or_public_id(client, migrated_engine):
    made = _queue(client, migrated_engine)
    h = _staff(migrated_engine, Role.admin, "ad-q")
    detail = client.get(f"/v2/batches/{made['harvested']}", headers=h).json()
    code = detail["batch_code"]

    def ids(q: str) -> list[int]:
        res = client.get("/v2/batches", params={"q": q}, headers=h)
        assert res.status_code == 200, res.text
        return [b["id"] for b in res.json()["items"]]

    assert ids(code.lower()[:7]) == [made["harvested"]]
    assert ids(code) == [made["harvested"]]
    assert ids(detail["public_id"]) == [made["harvested"]]
    assert ids("B-%") == []
    assert client.get("/v2/batches", params={"q": "x" * 41}, headers=h).status_code == 422


def test_a_farmers_own_scope_survives_the_new_filters(client, migrated_engine):
    made = _queue(client, migrated_engine)
    other = _farmer(migrated_engine, "Mwende", "Musyoka", "+254700050031")
    other_h = _farmer_login(migrated_engine, other, "f-other-q")
    mine = _batch(client, other, other_h, migrated_engine)
    detail = client.get(
        f"/v2/batches/{made['harvested']}", headers=_staff(migrated_engine, Role.admin, "ad-q3")
    ).json()

    res = client.get("/v2/batches", params={"q": detail["batch_code"]}, headers=other_h)
    assert res.json()["items"] == []
    res = client.get("/v2/batches?state=CREATED&state=HARVESTED", headers=other_h)
    assert [b["id"] for b in res.json()["items"]] == [mine["id"]]


def test_summary_and_detail_name_the_farmer(client, migrated_engine):
    made = _queue(client, migrated_engine)
    h = _staff(migrated_engine, Role.operator, "op-names")
    items = client.get("/v2/batches", headers=h).json()["items"]
    assert {b["farmer_display_name"] for b in items} == {"Wanjiru Kamau"}
    detail = client.get(f"/v2/batches/{made['created']}", headers=h).json()
    assert detail["farmer_display_name"] == "Wanjiru Kamau"


# --- next_action (D10) ---------------------------------------------------------

# Written out by hand rather than derived from ACTION_ROLES, so a change to the
# RBAC table shows up here as a decision someone has to make.
EXPECTED = {
    BatchState.CREATED: ("batch.harvest_record", "HARVESTED", {"farmer", "operator", "admin"}),
    BatchState.HARVESTED: ("batch.process_record", "PROCESSED", {"operator", "admin"}),
    BatchState.PROCESSED: ("batch.lab_verify", "LAB_VERIFIED", {"lab_officer", "admin"}),
    BatchState.LAB_VERIFIED: ("batch.package", "PACKAGED", {"operator", "admin"}),
    BatchState.PACKAGED: ("batch.distribute", "DISTRIBUTED", {"operator", "admin"}),
}


def test_every_lifecycle_target_has_a_next_action():
    """A new state cannot be added to the machine without the console
    learning who may move a batch into it."""
    assert set(_ACTION_FOR) == set(LEGAL.values())


def test_next_action_for_each_role_at_each_state(client, migrated_engine):
    a = _farmer(migrated_engine, "Wanjiru", "Kamau", "+254700050040")
    viewers = {
        "farmer": _farmer_login(migrated_engine, a, "f-next"),
        "field_officer": _staff(migrated_engine, Role.field_officer, "fo-next"),
        "operator": _staff(migrated_engine, Role.operator, "op-next"),
        "lab_officer": _staff(migrated_engine, Role.lab_officer, "lab-next"),
        "admin": _staff(migrated_engine, Role.admin, "ad-next"),
    }
    batch = _batch(client, a, viewers["farmer"], migrated_engine)
    steps = [
        ("harvest", HARVEST, "farmer"),
        ("process", PROCESS, "operator"),
        ("lab-verify", CLEAN_PANEL, "lab_officer"),
        ("package", PACKAGE, "admin"),
        ("distribute", DISTRIBUTE, "operator"),
    ]
    for state in [*EXPECTED, BatchState.DISTRIBUTED]:
        for role, headers in viewers.items():
            detail = client.get(f"/v2/batches/{batch['id']}", headers=headers).json()
            assert detail["state"] == state
            if state is BatchState.DISTRIBUTED:
                assert detail["next_action"] is None
                continue
            action, target, allowed = EXPECTED[state]
            assert detail["next_action"] == {
                "action": action,
                "target_state": target,
                "allowed": role in allowed,
            }, (state, role)
        if steps:
            stage, body, who = steps.pop(0)
            res = client.post(f"/v2/batches/{batch['id']}/{stage}", json=body, headers=viewers[who])
            assert res.status_code == 201, res.text


# --- Console fixtures for the client ---------------------------------------------


def test_console_fixtures_walk_the_headline_batch(client, migrated_engine):
    """The headline walk (13 §1): four distinct people, the admin packaging in
    the operator's place. Writes the real detail JSON at each state for the
    client's console tests, on request only:
    `APICHAIN_WRITE_FIXTURES=1 uv run pytest -k console_fixtures`."""
    a = _farmer(migrated_engine, "Wanjiru", "Kamau", "+254700050050")
    farmer = _farmer_login(migrated_engine, a, "f-headline")
    op = _staff(migrated_engine, Role.operator, "op-headline", "Jane Mwende")
    lab = _staff(migrated_engine, Role.lab_officer, "lab-headline", "Achieng Odhiambo")
    admin = _staff(migrated_engine, Role.admin, "ad-headline", "Peter Otieno")
    batch = _batch(client, a, farmer, migrated_engine)

    snapshots = {"created": client.get(f"/v2/batches/{batch['id']}", headers=op).json()}
    for name, stage, body, who in (
        ("harvested", "harvest", HARVEST, farmer),
        ("processed", "process", PROCESS, op),
        ("lab_verified", "lab-verify", CLEAN_PANEL, lab),
        ("packaged", "package", PACKAGE, admin),
        ("distributed", "distribute", DISTRIBUTE, op),
    ):
        res = client.post(f"/v2/batches/{batch['id']}/{stage}", json=body, headers=who)
        assert res.status_code == 201, res.text
        snapshots[name] = client.get(f"/v2/batches/{batch['id']}", headers=op).json()

    final = snapshots["distributed"]
    by_action = {s["action"]: s for s in final["stages"]}
    assert by_action["batch.harvest_recorded"]["actor_display_name"] == "Wanjiru Kamau"
    assert by_action["batch.process_recorded"]["actor_display_name"] == "Jane Mwende"
    assert by_action["batch.lab_verified"]["actor_display_name"] == "Achieng Odhiambo"
    assert by_action["batch.packaged"]["actor_display_name"] == "Peter Otieno"
    assert by_action["batch.packaged"]["actor_role"] == "admin"
    assert by_action["batch.distributed"]["actor_role"] == "operator"

    if os.environ.get("APICHAIN_WRITE_FIXTURES") != "1":
        return
    FIXTURES.mkdir(parents=True, exist_ok=True)
    for name, body in snapshots.items():
        (FIXTURES / f"{name}.json").write_text(
            json.dumps(body, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n"
        )
