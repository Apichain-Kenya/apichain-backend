"""Enrolment contact verification (P3b-G, 11 §9).

The test to read first is `test_a_wrong_guess_is_counted_even_though_it_is_refused`.
In this codebase an `APIError` rolls the transaction back, so a counter
incremented and then raised would never have been incremented. Every
response would still look right. Only a read from a fresh session shows it.
"""

import datetime as dt
import json

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.enums import CommStatus, Role
from app.models import AuditLog, Communication, Farmer, VerificationCode
from app.services import verification_codes as codes
from tests.helpers import auth, seed_farmer, seed_user

_PHONE = "0712 345 678"


class Clock:
    """Replaces verification_codes.utcnow so expiry and rate windows can be
    walked forward without sleeping."""

    def __init__(self) -> None:
        self.now = dt.datetime(2026, 10, 8, 9, 0, 0)

    def __call__(self) -> dt.datetime:
        return self.now

    def advance(self, **delta: float) -> None:
        self.now += dt.timedelta(**delta)


@pytest.fixture
def clock(monkeypatch) -> Clock:
    c = Clock()
    monkeypatch.setattr(codes, "utcnow", c)
    return c


def _enrolled(engine, phone: str = _PHONE, email: str | None = None) -> int:
    """A farmer as enrolment leaves them: with data_processing consent."""
    with Session(engine) as s:
        farmer = Farmer(first_name="A", last_name="B", phone=phone, email=email)
        s.add(farmer)
        s.flush()
        s.execute(
            text(
                "insert into consent_records (subject_type, subject_id, consent_purpose, granted,"
                " granted_via, text_version) values ('farmer', :id, 'data_processing', true,"
                " 'onboarder', 'v1')"
            ),
            {"id": farmer.id},
        )
        s.commit()
        return farmer.id


def _officer(engine, name: str = "fo-v") -> dict[str, str]:
    return auth(seed_user(engine, Role.field_officer, name), Role.field_officer)


def _send(client, farmer: int, headers, channel: str = "sms"):
    return client.post(
        f"/v2/farmers/{farmer}/verifications", json={"channel": channel}, headers=headers
    )


def _confirm(client, farmer: int, headers, code: str, channel: str = "sms"):
    return client.post(
        f"/v2/farmers/{farmer}/verifications/confirm",
        json={"channel": channel, "code": code},
        headers=headers,
    )


def _sent_code(boundaries) -> str:
    body = boundaries.sms.sent[-1][1]
    return next(word.rstrip(".") for word in body.split() if word.rstrip(".").isdigit())


# --- send ---------------------------------------------------------------------


def test_a_send_logs_and_audits_and_stores_no_code(client, migrated_engine, boundaries, clock):
    farmer = _enrolled(migrated_engine)
    r = _send(client, farmer, _officer(migrated_engine))
    assert r.status_code == 202, r.text
    assert "code" not in r.json() and "recipient" not in r.json()

    to, body = boundaries.sms.sent[0]
    assert to == "+254712345678"  # normalized before the provider sees it
    code = _sent_code(boundaries)

    with Session(migrated_engine) as s:
        comm = s.execute(select(Communication)).scalar_one()
        issued = s.execute(select(VerificationCode)).scalar_one()
        audit = s.execute(
            select(AuditLog).where(AuditLog.action == "communication.sent")
        ).scalar_one()
    assert comm.status is CommStatus.sent and comm.purpose == "verification"
    assert issued.code_hmac == codes.mac(farmer, "sms", code)
    stored = json.dumps(comm.payload) + json.dumps(audit.payload)
    assert code not in stored and "+254712345678" not in json.dumps(audit.payload)


def test_the_right_code_verifies_the_contact(client, migrated_engine, boundaries, clock):
    farmer = _enrolled(migrated_engine)
    officer = _officer(migrated_engine)
    _send(client, farmer, officer)
    r = _confirm(client, farmer, officer, _sent_code(boundaries))
    assert r.status_code == 200, r.text
    with Session(migrated_engine) as s:
        assert s.get(Farmer, farmer).phone_verified_at == clock.now
        verified = s.execute(
            select(AuditLog).where(AuditLog.action == "farmer.contact_verified")
        ).scalar_one()
    assert verified.payload == {
        "farmer_id": farmer,
        "channel": "sms",
        "verification_id": verified.payload["verification_id"],
    }
    # Consumed: the same code does not work twice.
    assert _confirm(client, farmer, officer, _sent_code(boundaries)).status_code == 422


def test_a_wrong_guess_is_counted_even_though_it_is_refused(
    client, migrated_engine, boundaries, clock
):
    farmer = _enrolled(migrated_engine)
    officer = _officer(migrated_engine)
    _send(client, farmer, officer)
    right = _sent_code(boundaries)
    wrong = f"{(int(right) + 1) % 10**6:06d}"

    r = _confirm(client, farmer, officer, wrong)
    assert r.status_code == 422
    assert r.json()["code"] == "invalid_code"
    assert r.json()["details"]["attempts_remaining"] == codes.MAX_ATTEMPTS - 1
    with Session(migrated_engine) as fresh:  # not the request's session
        assert fresh.execute(select(VerificationCode.attempts)).scalar_one() == 1


def test_five_wrong_guesses_lock_the_code_even_against_the_right_one(
    client, migrated_engine, boundaries, clock
):
    farmer = _enrolled(migrated_engine)
    officer = _officer(migrated_engine)
    _send(client, farmer, officer)
    right = _sent_code(boundaries)
    wrong = f"{(int(right) + 1) % 10**6:06d}"
    for _ in range(codes.MAX_ATTEMPTS):
        assert _confirm(client, farmer, officer, wrong).json()["code"] == "invalid_code"
    r = _confirm(client, farmer, officer, right)
    assert r.status_code == 422
    assert r.json()["code"] == "code_locked"


def test_an_expired_code_is_refused(client, migrated_engine, boundaries, clock):
    farmer = _enrolled(migrated_engine)
    officer = _officer(migrated_engine)
    _send(client, farmer, officer)
    clock.advance(minutes=10)
    r = _confirm(client, farmer, officer, _sent_code(boundaries))
    assert r.status_code == 422
    assert r.json()["code"] == "no_active_code"


def test_a_new_code_supersedes_the_old_one(client, migrated_engine, boundaries, clock):
    farmer = _enrolled(migrated_engine)
    officer = _officer(migrated_engine)
    _send(client, farmer, officer)
    old = _sent_code(boundaries)
    clock.advance(seconds=61)
    _send(client, farmer, officer)
    new = _sent_code(boundaries)
    if old != new:  # one in a million they collide; then there is nothing to show
        assert _confirm(client, farmer, officer, old).status_code == 422
    assert _confirm(client, farmer, officer, new).status_code == 200


# --- limits ---------------------------------------------------------------------


def test_a_second_send_inside_the_cooldown_is_429(client, migrated_engine, boundaries, clock):
    farmer = _enrolled(migrated_engine)
    officer = _officer(migrated_engine)
    _send(client, farmer, officer)
    clock.advance(seconds=20)
    r = _send(client, farmer, officer)
    assert r.status_code == 429
    assert r.headers["Retry-After"] == "40"
    assert len(boundaries.sms.sent) == 1


def test_the_sixth_send_in_an_hour_is_429(client, migrated_engine, boundaries, clock):
    farmer = _enrolled(migrated_engine)
    officer = _officer(migrated_engine)
    for _ in range(codes.SENDS_PER_WINDOW):
        assert _send(client, farmer, officer).status_code == 202
        clock.advance(seconds=61)
    r = _send(client, farmer, officer)
    assert r.status_code == 429
    assert r.json()["code"] == "rate_limited"
    clock.advance(hours=1)
    assert _send(client, farmer, officer).status_code == 202


def test_the_limit_is_per_channel(client, migrated_engine, boundaries, clock):
    farmer = _enrolled(migrated_engine, email="farmer@example.test")
    officer = _officer(migrated_engine)
    assert _send(client, farmer, officer, "sms").status_code == 202
    assert _send(client, farmer, officer, "email").status_code == 202
    assert boundaries.email.sent[0][0] == "farmer@example.test"


# --- refusals ---------------------------------------------------------------------


def test_a_provider_failure_is_503_keeps_the_failed_row_and_burns_nothing(
    client, migrated_engine, boundaries, clock
):
    farmer = _enrolled(migrated_engine)
    officer = _officer(migrated_engine)
    boundaries.sms.fail_next = 1
    with Session(migrated_engine) as s:
        before = s.execute(text("select coalesce(max(id), 0) from audit_log")).scalar_one()
    r = _send(client, farmer, officer)
    assert r.status_code == 503
    assert r.json()["code"] == "send_failed"
    with Session(migrated_engine) as s:
        comm = s.execute(select(Communication)).scalar_one()
        issued = s.execute(select(VerificationCode)).scalar_one()
        after = s.execute(text("select coalesce(max(id), 0) from audit_log")).scalar_one()
    assert comm.status is CommStatus.failed
    assert issued.superseded_at is not None  # nobody received it; it cannot be used
    assert after == before


@pytest.mark.parametrize("phone", ["12345", "+14155550123"])
def test_an_unsendable_number_is_422_and_writes_nothing(client, migrated_engine, clock, phone):
    farmer = _enrolled(migrated_engine, phone=phone)
    r = _send(client, farmer, _officer(migrated_engine))
    assert r.status_code == 422
    assert r.json()["code"] == "invalid_recipient"
    with Session(migrated_engine) as s:
        assert s.execute(text("select count(*) from communications")).scalar_one() == 0


def test_email_with_no_address_on_file_is_422(client, migrated_engine, clock):
    farmer = _enrolled(migrated_engine)
    r = _send(client, farmer, _officer(migrated_engine), "email")
    assert r.status_code == 422
    assert r.json()["code"] == "invalid_recipient"


def test_no_enrolment_consent_no_code(client, migrated_engine, boundaries, clock):
    farmer = seed_farmer(migrated_engine, "+254700360001")
    r = _send(client, farmer, _officer(migrated_engine))
    assert r.status_code == 422
    assert r.json()["code"] == "consent_required"
    assert boundaries.sms.sent == []


def test_a_farmer_cannot_verify_or_confirm_for_another(client, migrated_engine, clock):
    victim = _enrolled(migrated_engine)
    intruder = seed_farmer(migrated_engine, "+254700360002")
    headers = auth(seed_user(migrated_engine, Role.farmer, "intr", farmer_id=intruder), Role.farmer)
    assert _send(client, victim, headers).status_code == 403
    assert _confirm(client, victim, headers, "123456").status_code == 403


def test_a_farmer_may_verify_their_own_number(client, migrated_engine, boundaries, clock):
    farmer = _enrolled(migrated_engine)
    headers = auth(seed_user(migrated_engine, Role.farmer, "own", farmer_id=farmer), Role.farmer)
    assert _send(client, farmer, headers).status_code == 202
    assert _confirm(client, farmer, headers, _sent_code(boundaries)).status_code == 200


def test_operators_cannot_send_codes(client, migrated_engine, clock):
    farmer = _enrolled(migrated_engine)
    headers = auth(seed_user(migrated_engine, Role.operator, "op-v"), Role.operator)
    assert _send(client, farmer, headers).status_code == 403


@pytest.mark.parametrize("code", ["12345a", "12345", "1234567", "12 456"])
def test_a_malformed_code_is_refused_at_the_edge(client, migrated_engine, clock, code):
    farmer = _enrolled(migrated_engine)
    assert _confirm(client, farmer, _officer(migrated_engine), code).status_code == 422


def test_unknown_farmer_is_404(client, migrated_engine, clock):
    assert _send(client, 999999, _officer(migrated_engine)).status_code == 404


# --- the pure limiter -----------------------------------------------------------


def test_retry_after_is_none_when_clear():
    now = dt.datetime(2026, 10, 8, 9, 0)
    assert codes.retry_after([], now) is None
    assert codes.retry_after([now - dt.timedelta(minutes=5)], now) is None


def test_the_codes_are_six_digits_and_the_mac_binds_farmer_and_channel():
    code = codes.generate()
    assert len(code) == 6 and code.isdigit()
    assert codes.mac(1, "sms", code) != codes.mac(2, "sms", code)
    assert codes.mac(1, "sms", code) != codes.mac(1, "email", code)
    assert codes.matches(codes.mac(1, "sms", code), 1, "sms", code)
