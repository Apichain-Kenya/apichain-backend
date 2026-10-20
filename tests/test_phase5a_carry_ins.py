"""Phase 5a, P5-A: correctness carry-ins (14 §4).

Each block is one carried item: the locality lookup no longer holds a pooled
connection (D19), every connection runs in UTC (D20), the server normalises a
Kenyan phone at enrolment and sign-in (D18), new audit payloads carry no PII
(14 §0 #9), and a developer can read a texted code in the api's own output.
"""

import datetime as dt
import logging
from decimal import Decimal

from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from app.database import make_engine
from app.enums import Role
from app.models import ApiaryLocation, AuditLog, Farmer, HoneyBatch, User
from app.services import geocode
from tests.helpers import METADATA, auth, seed_farmer, seed_user

FARMER_PW = "farmpass9"
STAFF_PW = "honey from tetu in march"


def _enable(monkeypatch, name: str) -> None:
    # Alembic's fileConfig disables loggers that existed before it ran.
    monkeypatch.setattr(logging.getLogger(name), "disabled", False)


def _farmer(engine, phone: str, username: str):
    farmer_id = seed_farmer(engine, phone)
    return farmer_id, auth(
        seed_user(engine, Role.farmer, username, farmer_id=farmer_id), Role.farmer
    )


def _apiary_body(farmer_id: int) -> dict:
    return {
        "farmer_id": farmer_id,
        "name": "Ridge",
        "latitude": "-0.428713",
        "longitude": "36.951247",
    }


def _payloads(engine, action: str) -> list[dict]:
    with Session(engine) as s:
        rows = s.execute(select(AuditLog).where(AuditLog.action == action)).scalars()
        return [r.payload for r in rows]


def _enroll_body(phone: str) -> dict:
    return {
        "first_name": "Wanjiru",
        "last_name": "Kamau",
        "phone": phone,
        "password": FARMER_PW,
        "consent_granted": True,
        "consent_text_version": "en-2026-10",
    }


# --- The locality lookup holds no connection (D19) ----------------------------


class _PoolProbe:
    """A geocoder that records how many pooled connections are checked out
    while it runs: the slow third-party call must not be holding one."""

    def __init__(self, engine, answer: str | None = "Nyeri, Central, Kenya") -> None:
        self.engine = engine
        self.answer = answer
        self.checked_out: list[int] = []

    def __call__(self, latitude, longitude):
        self.checked_out.append(self.engine.pool.checkedout())
        return self.answer


def test_the_lookup_runs_with_no_connection_checked_out(client, migrated_engine, boundaries):
    farmer_id, h = _farmer(migrated_engine, "+254700050001", "f-p5a1")
    probe = _PoolProbe(migrated_engine)
    boundaries.geocoder = probe

    res = client.post("/v2/apiaries", json=_apiary_body(farmer_id), headers=h)
    assert res.status_code == 201, res.text
    assert res.json()["locality"] == "Nyeri, Central, Kenya"
    assert probe.checked_out == [0]


def test_a_replay_and_a_refusal_never_reach_the_lookup(client, migrated_engine, boundaries):
    farmer_id, h = _farmer(migrated_engine, "+254700050002", "f-p5a2")
    _, other_h = _farmer(migrated_engine, "+254700050003", "f-p5a3")
    probe = _PoolProbe(migrated_engine)
    boundaries.geocoder = probe

    keyed = {**h, "Idempotency-Key": "apiary-replay-1"}
    first = client.post("/v2/apiaries", json=_apiary_body(farmer_id), headers=keyed)
    again = client.post("/v2/apiaries", json=_apiary_body(farmer_id), headers=keyed)
    assert first.status_code == again.status_code == 201
    assert again.json() == first.json()

    refused = client.post("/v2/apiaries", json=_apiary_body(farmer_id), headers=other_h)
    assert refused.status_code == 403
    assert len(probe.checked_out) == 1


def test_a_refused_lookup_is_logged_not_silent(monkeypatch, caplog):
    class _Limited:
        status_code = 429

        def json(self):
            return {}

    monkeypatch.setattr(geocode.requests, "get", lambda *a, **k: _Limited())
    _enable(monkeypatch, "apichain.geocode")
    with caplog.at_level("WARNING", logger="apichain.geocode"):
        assert geocode.reverse_geocode(Decimal("-0.42"), Decimal("36.95")) is None
    assert "429" in caplog.text


def test_a_failed_lookup_is_logged_without_the_point(monkeypatch, caplog):
    def _down(*a, **k):
        raise geocode.requests.ConnectionError("unreachable")

    monkeypatch.setattr(geocode.requests, "get", _down)
    _enable(monkeypatch, "apichain.geocode")
    with caplog.at_level("WARNING", logger="apichain.geocode"):
        assert geocode.reverse_geocode(Decimal("-0.428713"), Decimal("36.951247")) is None
    assert "ConnectionError" in caplog.text
    assert "428713" not in caplog.text  # the exact point never reaches a log


def test_the_backfill_fills_missing_localities_at_one_a_second(db):
    farmer = Farmer(first_name="A", last_name="B", phone="+254700050010")
    db.add(farmer)
    db.flush()

    def _apiary(name: str, locality: str | None) -> ApiaryLocation:
        row = ApiaryLocation(
            farmer_id=farmer.id,
            name=name,
            latitude=Decimal("-0.428713"),
            longitude=Decimal("36.951247"),
            locality=locality,
        )
        db.add(row)
        return row

    missing = [_apiary("one", None), _apiary("two", None)]
    named = _apiary("three", "Already, Named")
    db.flush()

    asked: list[tuple] = []
    pauses: list[float] = []

    def _geocoder(lat, lon):
        asked.append((lat, lon))
        return "Nyeri, Central, Kenya"

    filled = geocode.backfill_localities(db, _geocoder, pause=pauses.append)
    assert filled == 2
    assert all(row.locality == "Nyeri, Central, Kenya" for row in missing)
    assert named.locality == "Already, Named"
    assert asked == [(Decimal("-0.43"), Decimal("36.95"))] * 2  # 2 dp only (12 D12)
    assert pauses == [1.0]  # one gap between two lookups


def test_the_backfill_leaves_a_row_null_when_the_lookup_misses(db):
    farmer = Farmer(first_name="A", last_name="B", phone="+254700050011")
    db.add(farmer)
    db.flush()
    row = ApiaryLocation(
        farmer_id=farmer.id, name="x", latitude=Decimal("-0.4"), longitude=Decimal("36.9")
    )
    db.add(row)
    db.flush()
    assert geocode.backfill_localities(db, lambda lat, lon: None, pause=lambda s: None) == 0
    assert row.locality is None


def test_locality_is_outside_every_hashed_payload():
    # The hashed S0 payload is built from the per-batch `ApiaryRecord`
    # snapshot, which has no locality; the label lives only on the apiary row,
    # so filling it in later changes no record's hash.
    from app.models import ApiaryRecord
    from app.services import stage_payloads

    assert "locality" not in ApiaryRecord.__table__.columns
    row = ApiaryRecord(batch_id=1, apiary_id=1, latitude=Decimal("-0.4"), longitude=Decimal("36.9"))
    assert "locality" not in stage_payloads.apiary_record(row)


# --- Every connection runs in UTC (D20) ---------------------------------------


def test_every_connection_runs_in_utc_whatever_the_server_zone(migrated_engine):
    url = migrated_engine.url.render_as_string(hide_password=False)
    dbname = migrated_engine.url.database
    with migrated_engine.connect() as c:
        c.execute(text(f"ALTER DATABASE \"{dbname}\" SET timezone TO 'Africa/Nairobi'"))
        c.commit()
    unpinned = create_engine(url)
    pinned = make_engine(url)
    try:
        with unpinned.connect() as c:
            assert c.execute(text("SHOW TimeZone")).scalar_one() == "Africa/Nairobi"
        with pinned.connect() as c:
            assert c.execute(text("SHOW TimeZone")).scalar_one() == "UTC"

        with Session(pinned) as s:
            farmer = Farmer(first_name="A", last_name="B", phone="+254700050020")
            s.add(farmer)
            s.flush()
            batch = HoneyBatch(farmer_id=farmer.id, batch_code="B-UTC00000001")
            s.add(batch)
            s.flush()
            s.refresh(batch)
            now = dt.datetime.now(dt.UTC).replace(tzinfo=None)
            assert abs(batch.created_at - now) < dt.timedelta(minutes=1)
            assert abs(batch.state_updated_at - now) < dt.timedelta(minutes=1)
            s.rollback()
    finally:
        unpinned.dispose()
        pinned.dispose()
        with migrated_engine.connect() as c:
            c.execute(text(f'ALTER DATABASE "{dbname}" RESET timezone'))
            c.commit()


# --- The server normalises a Kenyan phone (D18) -------------------------------


def test_enrolment_stores_a_typed_number_in_e164(client, migrated_engine):
    officer = seed_user(migrated_engine, Role.field_officer, "off-p5a1")
    res = client.post(
        "/v2/farmers",
        json=_enroll_body("0745 220 961"),
        headers=auth(officer, Role.field_officer),
    )
    assert res.status_code == 201, res.text
    assert res.json()["phone"] == "+254745220961"
    with Session(migrated_engine) as s:
        user = s.get(User, res.json()["user_id"])
        farmer = s.get(Farmer, res.json()["id"])
        assert user.phone == farmer.phone == "+254745220961"


def test_enrolment_refuses_a_number_that_is_not_a_kenyan_mobile(client, migrated_engine):
    officer = seed_user(migrated_engine, Role.field_officer, "off-p5a2")
    res = client.post(
        "/v2/farmers", json=_enroll_body("12345"), headers=auth(officer, Role.field_officer)
    )
    assert res.status_code == 422
    assert res.json()["code"] == "invalid_phone"


def test_the_same_number_typed_two_ways_is_one_farmer(client, migrated_engine):
    officer = auth(seed_user(migrated_engine, Role.field_officer, "off-p5a3"), Role.field_officer)
    first = client.post("/v2/farmers", json=_enroll_body("+254745220962"), headers=officer)
    assert first.status_code == 201, first.text
    again = client.post("/v2/farmers", json=_enroll_body("0745-220-962"), headers=officer)
    assert again.status_code == 409
    assert again.json()["code"] == "phone_taken"


def test_sign_in_accepts_the_number_as_a_farmer_types_it(client, migrated_engine):
    officer = seed_user(migrated_engine, Role.field_officer, "off-p5a4")
    enrolled = client.post(
        "/v2/farmers", json=_enroll_body("+254745220963"), headers=auth(officer, Role.field_officer)
    )
    assert enrolled.status_code == 201, enrolled.text
    for typed in ("0745 220 963", "254745220963", "+254745220963"):
        client.cookies.clear()
        res = client.post("/v2/auth/login", json={"identifier": typed, "password": FARMER_PW})
        assert res.status_code == 200, (typed, res.text)


def test_sign_in_still_matches_a_phone_stored_before_normalisation(client, migrated_engine):
    # Dev databases may hold a phone enrolled in another form before P5-A; it
    # is matched as stored, so nobody is locked out and nothing is rewritten.
    with Session(migrated_engine) as s:
        from app.services import security

        s.add(
            User(
                phone="0745220964",
                password_hash=security.hash_password(FARMER_PW),
                role=Role.farmer,
                is_root=False,
                is_active=True,
            )
        )
        s.commit()
    res = client.post("/v2/auth/login", json={"identifier": "0745220964", "password": FARMER_PW})
    assert res.status_code == 200, res.text


# --- New audit payloads carry no PII (14 §0 #9) -------------------------------


def test_new_audit_payloads_carry_no_personal_data(client, migrated_engine):
    officer = seed_user(migrated_engine, Role.field_officer, "off-p5a5")
    oh = auth(officer, Role.field_officer)
    enrolled = client.post("/v2/farmers", json=_enroll_body("+254745220965"), headers=oh)
    assert enrolled.status_code == 201, enrolled.text
    farmer_id = enrolled.json()["id"]

    apiary = client.post(
        "/v2/apiaries",
        json={**_apiary_body(farmer_id), "name": "Wanjiru's ridge"},
        headers=oh,
    )
    assert apiary.status_code == 201, apiary.text
    operator = auth(seed_user(migrated_engine, Role.operator, "op-p5a1"), Role.operator)
    batch = client.post(
        "/v2/batches",
        json={
            "farmer_id": farmer_id,
            "apiary_id": apiary.json()["id"],
            "batch_code": "WANJIRU-0745",
            "metadata": METADATA,
        },
        headers=operator,
    )
    assert batch.status_code == 201, batch.text

    admin = seed_user(migrated_engine, Role.admin, "adm-p5a1")
    user = client.post(
        "/v2/users",
        json={
            "username": "jmwende",
            "full_name": "Jane Mwende",
            "role": "operator",
            "password": STAFF_PW,
        },
        headers=auth(admin, Role.admin),
    )
    assert user.status_code == 201, user.text

    assert _payloads(migrated_engine, "farmer.enrolled") == [
        {"farmer_id": farmer_id, "enrolled_by": officer}
    ]
    assert _payloads(migrated_engine, "apiary.created") == [
        {"apiary_id": apiary.json()["id"], "farmer_id": farmer_id}
    ]
    assert _payloads(migrated_engine, "batch.created") == [
        {"farmer_id": farmer_id, "state": "CREATED"}
    ]
    assert _payloads(migrated_engine, "user.created") == [
        {"user_id": user.json()["id"], "role": "operator", "created_by": admin}
    ]

    secrets = ("Wanjiru", "Kamau", "745220965", "ridge", "WANJIRU", "jmwende", "Jane")
    for row in (
        _payloads(migrated_engine, "farmer.enrolled")
        + _payloads(migrated_engine, "apiary.created")
        + _payloads(migrated_engine, "batch.created")
        + _payloads(migrated_engine, "user.created")
    ):
        assert not any(s in repr(row) for s in secrets), row


def test_the_bootstrap_row_carries_no_username(migrated_engine):
    from app.services import bootstrap

    with Session(migrated_engine) as s:
        user_id = bootstrap.create_root_admin(
            s, username="groot-p5a", full_name="Grace Njeri", password=STAFF_PW
        )
        row = s.execute(select(AuditLog).where(AuditLog.action == "user.bootstrapped")).scalar_one()
        assert row.payload == {"user_id": user_id}
        s.rollback()


# --- A developer can read a texted code (handoff item 6) ----------------------


def test_with_bodies_on_the_fake_sms_prints_where_the_api_shows_it(monkeypatch, caplog):
    from app.config import settings
    from app.services.dev_fakes import LogSms

    monkeypatch.setattr(settings, "dev_log_message_bodies", True)
    _enable(monkeypatch, "apichain.dev_fakes")
    # The api prints WARNING and above; with no handler configured, INFO is lost.
    with caplog.at_level("WARNING", logger="apichain.dev_fakes"):
        LogSms().send("+254712345678", "code 482913")
    assert "482913" in caplog.text


def test_with_bodies_off_nothing_reaches_the_warning_level(monkeypatch, caplog):
    from app.config import settings
    from app.services.dev_fakes import LogSms

    monkeypatch.setattr(settings, "dev_log_message_bodies", False)
    _enable(monkeypatch, "apichain.dev_fakes")
    with caplog.at_level("WARNING", logger="apichain.dev_fakes"):
        LogSms().send("+254712345678", "code 482913")
    assert caplog.text == ""
