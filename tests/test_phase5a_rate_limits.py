"""Phase 5a, P5-B: rate limiting and API headers (14 §4, D7).

Login failures count per (identifier, IP) and per identifier; every login
attempt counts per IP; wrong current passwords count per account; code sends
and uploads count per IP. A refusal is 429 `rate_limited` with `Retry-After`,
and it holds even for the right password until the window passes. Small
limits are set per test where the defaults would mean dozens of bcrypt calls.
"""

import datetime as dt
import logging

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import settings
from app.enums import Role
from app.services import rate_limit
from tests.helpers import auth, seed_user

PW = "honey from tetu in march"
CLIENT = {"X-ApiChain-Client": "pwa"}
PROXY = "do-connecting-ip"


@pytest.fixture
def via_proxy(monkeypatch):
    """Trust App Platform's header, so a test can speak from several addresses."""
    monkeypatch.setattr(settings, "trusted_proxy_header", PROXY)


@pytest.fixture
def clock(monkeypatch):
    """A clock the test moves by hand."""
    now = {"t": dt.datetime(2026, 10, 20, 9, 0, 0)}
    monkeypatch.setattr(rate_limit, "_now", lambda: now["t"])

    def advance(**delta) -> None:
        now["t"] += dt.timedelta(**delta)

    return advance


def _login(client, identifier: str, password: str, ip: str | None = None):
    client.cookies.clear()
    headers = {PROXY: ip} if ip else {}
    return client.post(
        "/v2/auth/login", json={"identifier": identifier, "password": password}, headers=headers
    )


def _enable(monkeypatch) -> None:
    monkeypatch.setattr(logging.getLogger("apichain.rate_limit"), "disabled", False)


# --- Login ----------------------------------------------------------------------


def test_the_eleventh_failure_is_refused_even_with_the_right_password(
    client, migrated_engine, via_proxy, clock
):
    seed_user(migrated_engine, Role.operator, "rl-op1", password=PW)
    for _ in range(10):
        assert _login(client, "rl-op1", "wrong", "198.51.100.1").status_code == 401

    res = _login(client, "rl-op1", PW, "198.51.100.1")
    assert res.status_code == 429
    assert res.json()["code"] == "rate_limited"
    wait = int(res.headers["Retry-After"])
    assert 0 < wait <= 15 * 60
    assert res.json()["details"]["retry_after_seconds"] == wait

    clock(minutes=15, seconds=1)
    assert _login(client, "rl-op1", PW, "198.51.100.1").status_code == 200


def test_the_same_account_from_another_address_still_signs_in(
    client, migrated_engine, via_proxy, clock
):
    seed_user(migrated_engine, Role.operator, "rl-op2", password=PW)
    for _ in range(10):
        _login(client, "rl-op2", "wrong", "198.51.100.2")
    assert _login(client, "rl-op2", PW, "198.51.100.2").status_code == 429
    # Someone typing a farmer's number wrong cannot lock the farmer out of
    # their own phone (D7).
    assert _login(client, "rl-op2", PW, "203.0.113.9").status_code == 200


def test_the_identifier_ceiling_stops_a_guesser_spread_over_addresses(
    client, migrated_engine, via_proxy, clock, monkeypatch
):
    monkeypatch.setattr(settings, "login_identifier_failures_max", 4)
    seed_user(migrated_engine, Role.operator, "rl-op3", password=PW)
    for n in range(4):
        assert _login(client, "rl-op3", "wrong", f"198.51.100.{10 + n}").status_code == 401
    assert _login(client, "rl-op3", PW, "198.51.100.99").status_code == 429


def test_a_phone_counts_as_one_identifier_however_it_is_typed(
    client, migrated_engine, via_proxy, clock, monkeypatch
):
    monkeypatch.setattr(settings, "login_pair_failures_max", 2)
    seed_user(migrated_engine, Role.operator, "rl-op4", password=PW)
    with Session(migrated_engine) as s:
        s.execute(text("update users set phone = '+254745220970' where username = 'rl-op4'"))
        s.commit()
    _login(client, "0745 220 970", "wrong", "198.51.100.3")
    _login(client, "+254745220970", "wrong", "198.51.100.3")
    assert _login(client, "254745220970", PW, "198.51.100.3").status_code == 429


def test_a_success_resets_the_count_for_that_address(client, migrated_engine, via_proxy, clock):
    seed_user(migrated_engine, Role.operator, "rl-op5", password=PW)
    for _ in range(9):
        _login(client, "rl-op5", "wrong", "198.51.100.4")
    assert _login(client, "rl-op5", PW, "198.51.100.4").status_code == 200
    for _ in range(9):
        _login(client, "rl-op5", "wrong", "198.51.100.4")
    assert _login(client, "rl-op5", PW, "198.51.100.4").status_code == 200


def test_every_attempt_counts_against_the_address(
    client, migrated_engine, via_proxy, clock, monkeypatch
):
    monkeypatch.setattr(settings, "login_ip_attempts_max", 3)
    for n in range(3):
        seed_user(migrated_engine, Role.operator, f"rl-ip{n}", password=PW)
        assert _login(client, f"rl-ip{n}", PW, "198.51.100.5").status_code == 200
    assert _login(client, "rl-ip0", PW, "198.51.100.5").status_code == 429
    assert _login(client, "rl-ip0", PW, "198.51.100.6").status_code == 200


def test_a_spoofed_forwarded_for_is_ignored_without_a_trusted_header(
    client, migrated_engine, clock, monkeypatch
):
    monkeypatch.setattr(settings, "trusted_proxy_header", None)
    monkeypatch.setattr(settings, "login_ip_attempts_max", 2)
    seed_user(migrated_engine, Role.operator, "rl-op6", password=PW)
    for n in range(2):
        client.cookies.clear()
        client.post(
            "/v2/auth/login",
            json={"identifier": "rl-op6", "password": PW},
            headers={"X-Forwarded-For": f"192.0.2.{n}", PROXY: f"192.0.2.{n}"},
        )
    client.cookies.clear()
    res = client.post(
        "/v2/auth/login",
        json={"identifier": "rl-op6", "password": PW},
        headers={"X-Forwarded-For": "192.0.2.77", PROXY: "192.0.2.77"},
    )
    assert res.status_code == 429


def test_a_refusal_is_logged_without_the_identifier_or_address(
    client, migrated_engine, via_proxy, clock, monkeypatch, caplog
):
    monkeypatch.setattr(settings, "login_pair_failures_max", 1)
    seed_user(migrated_engine, Role.operator, "rl-op7", password=PW)
    _enable(monkeypatch)
    _login(client, "rl-op7", "wrong", "198.51.100.7")
    with caplog.at_level("WARNING", logger="apichain.rate_limit"):
        assert _login(client, "rl-op7", PW, "198.51.100.7").status_code == 429
    assert "login" in caplog.text
    assert "rl-op7" not in caplog.text
    assert "198.51.100.7" not in caplog.text


def test_the_counter_table_holds_no_identifier_or_address(
    client, migrated_engine, via_proxy, clock
):
    seed_user(migrated_engine, Role.operator, "rl-op8", password=PW)
    _login(client, "rl-op8", "wrong", "198.51.100.8")
    with Session(migrated_engine) as s:
        rows = s.execute(text("select scope, key_hash from rate_limit_events")).all()
    assert rows
    dumped = repr(rows)
    assert "rl-op8" not in dumped
    assert "198.51.100.8" not in dumped
    assert b"rl-op8" not in b"".join(r.key_hash for r in rows)


def test_limits_audit_nothing(client, migrated_engine, via_proxy, clock, monkeypatch):
    # A refusal written to the chain would let anyone write to it at will (D7).
    monkeypatch.setattr(settings, "login_pair_failures_max", 1)
    seed_user(migrated_engine, Role.operator, "rl-op9", password=PW)
    _login(client, "rl-op9", "wrong", "198.51.100.9")
    _login(client, "rl-op9", PW, "198.51.100.9")
    with Session(migrated_engine) as s:
        assert s.execute(text("select count(*) from audit_log")).scalar_one() == 0


# --- Password change --------------------------------------------------------------


def test_a_sixth_wrong_current_password_is_refused(client, migrated_engine, via_proxy, clock):
    seed_user(migrated_engine, Role.operator, "rl-pw1", password=PW)
    token = _login(client, "rl-pw1", PW, "198.51.100.20").json()["access_token"]
    h = {"Authorization": f"Bearer {token}", **CLIENT}

    def change(current: str):
        return client.post(
            "/v2/auth/password",
            json={"current_password": current, "new_password": "another long sentence here"},
            headers=h,
        )

    for _ in range(5):
        assert change("wrong").status_code == 403
    res = change(PW)
    assert res.status_code == 429
    assert res.json()["code"] == "rate_limited"
    clock(minutes=15, seconds=1)
    assert change(PW).status_code == 200


# --- Code sends and uploads, per address -----------------------------------------


def test_code_sends_are_limited_per_address(client, migrated_engine, via_proxy, clock, monkeypatch):
    monkeypatch.setattr(settings, "verification_send_ip_max", 2)
    h = {**auth(seed_user(migrated_engine, Role.field_officer, "rl-fo1"), Role.field_officer)}
    body = {"channel": "sms"}
    for _ in range(2):
        res = client.post(
            "/v2/farmers/999999/verifications", json=body, headers={**h, PROXY: "198.51.100.30"}
        )
        assert res.status_code == 404
    res = client.post(
        "/v2/farmers/999999/verifications", json=body, headers={**h, PROXY: "198.51.100.30"}
    )
    assert res.status_code == 429
    assert "Retry-After" in res.headers


def test_uploads_are_limited_per_address(client, migrated_engine, via_proxy, clock, monkeypatch):
    monkeypatch.setattr(settings, "upload_ip_max", 2)
    h = {**auth(seed_user(migrated_engine, Role.field_officer, "rl-fo2"), Role.field_officer)}

    def upload():
        return client.post(
            "/v2/farmers/999999/documents",
            files={"file": ("id.pdf", b"%PDF-1.4 test", "application/pdf")},
            data={"doc_type": "national_id"},
            headers={**h, PROXY: "198.51.100.31"},
        )

    assert upload().status_code == 404
    assert upload().status_code == 404
    res = upload()
    assert res.status_code == 429
    assert "Retry-After" in res.headers


# --- The contract shows it ---------------------------------------------------------


def test_every_limited_route_documents_its_429(client):
    paths = client.get("/openapi.json").json()["paths"]
    for path, method in (
        ("/v2/auth/login", "post"),
        ("/v2/auth/password", "post"),
        ("/v2/farmers/{farmer_id}/verifications", "post"),
        ("/v2/farmers/{farmer_id}/documents", "post"),
    ):
        assert "429" in paths[path][method]["responses"], path


# --- API security headers -----------------------------------------------------------


def test_every_response_carries_the_basic_headers(client):
    res = client.get("/health")
    assert res.headers["X-Content-Type-Options"] == "nosniff"
    assert res.headers["Referrer-Policy"] == "no-referrer"
    assert "Strict-Transport-Security" not in res.headers  # not outside production


def test_private_api_responses_are_not_stored_by_caches(client, migrated_engine):
    h = auth(seed_user(migrated_engine, Role.operator, "rl-hd1"), Role.operator)
    assert client.get("/v2/me", headers=h).headers["Cache-Control"] == "no-store"
    login = _login(client, "nobody", "wrong")
    assert login.headers["Cache-Control"] == "no-store"


def test_public_jar_responses_are_left_to_their_own_caching(client):
    res = client.get(f"/v2/public/batches/{'0' * 32}/verify")
    assert res.status_code == 404
    assert "no-store" not in res.headers.get("Cache-Control", "")


def test_production_adds_hsts(client, monkeypatch):
    monkeypatch.setattr(settings, "app_env", "production")
    res = client.get("/health")
    assert res.headers["Strict-Transport-Security"] == "max-age=31536000; includeSubDomains"
