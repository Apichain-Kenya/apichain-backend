"""Phase 5a, P5-C: structured logs, the request id, redaction, Sentry
(14 §4, D10, D11).

Log assertions read the *rendered* output of the real handler, not caplog:
caplog sees records before any formatter runs, so it would pass with the
request id and the redaction both broken. The Sentry test captures the
events a real client would send, from a failing request that carries a
farmer's details.
"""

import io
import json
import logging

import pytest
import sentry_sdk
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sentry_sdk.transport import Transport

from app.config import settings
from app.middleware.request_id import RequestId
from app.observability import configure_logging, init_sentry
from app.services.redaction import REDACTED, scrub, scrub_text, sensitive_keys

# Kept away from the code that raises, so Sentry's source context (five lines
# around each frame) shows code, not these values.
PHONE = "+254712345678"
LOCAL_PHONE = "0712345678"
EMAIL = "wanjiru.k@example.co.ke"
FIRST_NAME = "Wanjiru"
PASSWORD = "honey from tetu in march"
DIGITS = "712345678"


@pytest.fixture
def rendered(monkeypatch):
    """Every log line, rendered as JSON by the real formatter, as dicts."""
    monkeypatch.setattr(settings, "log_format", "json")
    buf = io.StringIO()
    configure_logging(stream=buf)
    # Alembic's fileConfig (run in-process by `migrated_engine`) disables the
    # loggers that already existed, these among them.
    for name in ("apichain.access", "apichain.request", "apichain.test"):
        monkeypatch.setattr(logging.getLogger(name), "disabled", False)

    def lines() -> list[dict]:
        return [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]

    lines.raw = buf.getvalue  # type: ignore[attr-defined]
    yield lines
    monkeypatch.undo()
    configure_logging()


# --- Redaction ---------------------------------------------------------------


@pytest.mark.parametrize(
    "phone", [PHONE, "254712345678", LOCAL_PHONE, "0112345678", "+447700900123"]
)
def test_every_phone_form_is_masked_in_free_text(phone):
    assert scrub_text(f"farmer {phone} enrolled") == f"farmer {REDACTED} enrolled"


@pytest.mark.parametrize(
    "text",
    [
        "request 9f0712345678ab4c8d0e1f2a3b4c5d6e",  # a minted request id
        "at 2026-11-04T11:47:25.021353Z",
        "code 123456 sent",
        "farmer 1234567890",
        "batch APC-2026-0001",
        "took 0712345678901 ms",
    ],
)
def test_ids_timestamps_and_codes_are_not_mistaken_for_phones(text):
    assert scrub_text(text) == text


def test_emails_are_masked():
    assert scrub_text(f"mail to {EMAIL} failed") == f"mail to {REDACTED} failed"


def test_the_key_list_comes_from_the_pii_tags_plus_the_secrets():
    keys = sensitive_keys()
    for tagged in ("phone", "first_name", "last_name", "email", "latitude", "gps_lon", "recipient"):
        assert tagged in keys
    assert "locality" not in keys  # tagged pii="none": shown to consumers
    for fixed in ("password", "authorization", "cookie", "code", "username", "identifier"):
        assert fixed in keys


def test_sensitive_keys_are_replaced_at_any_depth():
    out = scrub(
        {
            "farmer_id": 7,
            "farmer": {"first_name": FIRST_NAME, "phone": PHONE, "locality": "Tetu"},
            "headers": [{"Authorization": "Bearer abc"}],
            "new_password": PASSWORD,
            "refresh_token": "rt",
        }
    )
    assert out == {
        "farmer_id": 7,
        "farmer": {"first_name": REDACTED, "phone": REDACTED, "locality": "Tetu"},
        "headers": [{"Authorization": REDACTED}],
        "new_password": REDACTED,
        "refresh_token": REDACTED,
    }


def test_a_unique_violation_detail_loses_its_value_only_for_sensitive_columns():
    assert scrub_text("DETAIL:  Key (username)=(ian) already exists.") == (
        f"DETAIL:  Key (username)=({REDACTED}) already exists."
    )
    kept = "DETAIL:  Key (batch_code)=(APC-0001) already exists."
    assert scrub_text(kept) == kept


# --- Logs ----------------------------------------------------------------------


def test_a_login_is_logged_with_its_request_id_and_without_the_phone(client, rendered):
    res = client.post(
        "/v2/auth/login",
        json={"identifier": LOCAL_PHONE, "password": PASSWORD},
        headers={"X-Request-ID": "walk-0001-login"},
    )
    assert res.status_code == 401
    assert res.headers["X-Request-ID"] == "walk-0001-login"

    access = [line for line in rendered() if line["logger"] == "apichain.access"]
    assert access[-1]["request_id"] == "walk-0001-login"
    assert access[-1]["path"] == "/v2/auth/login"
    assert access[-1]["status"] == 401
    assert DIGITS not in rendered.raw()
    assert PASSWORD not in rendered.raw()


def test_a_line_logged_inside_a_sync_handler_carries_the_request_id(client, rendered, monkeypatch):
    """The login route is a plain `def`, so it runs in the threadpool; the
    request id has to cross that hop to reach the limiter's warning."""
    monkeypatch.setattr(settings, "login_ip_attempts_max", 0)
    monkeypatch.setattr(logging.getLogger("apichain.rate_limit"), "disabled", False)
    res = client.post(
        "/v2/auth/login",
        json={"identifier": LOCAL_PHONE, "password": PASSWORD},
        headers={"X-Request-ID": "walk-0003-limit"},
    )
    assert res.status_code == 429

    limited = [line for line in rendered() if line["logger"] == "apichain.rate_limit"]
    assert limited and limited[-1]["request_id"] == "walk-0003-limit"
    assert DIGITS not in rendered.raw()


def test_dev_message_bodies_keep_the_code_but_mask_the_recipient(rendered, monkeypatch):
    from app.services.dev_fakes import LogSms

    monkeypatch.setattr(settings, "dev_log_message_bodies", True)
    monkeypatch.setattr(logging.getLogger("apichain.dev_fakes"), "disabled", False)
    LogSms().send(PHONE, "Your ApiChain code is 482913")

    line = rendered()[-1]["event"]
    assert "482913" in line
    assert DIGITS not in line


def test_the_access_line_leaves_out_the_query_string(client, rendered):
    client.get(f"/v2/farmers?q={LOCAL_PHONE}")
    access = [line for line in rendered() if line["logger"] == "apichain.access"][-1]
    assert access["path"] == "/v2/farmers"
    assert DIGITS not in rendered.raw()


def test_a_malformed_inbound_id_is_replaced(client, rendered):
    res = client.get("/health", headers={"X-Request-ID": "bad id\twith spaces"})
    rid = res.headers["X-Request-ID"]
    assert rid != "bad id\twith spaces"
    assert len(rid) == 32
    assert rendered()[-1]["request_id"] == rid


def test_every_request_gets_a_fresh_id(client):
    ids = {client.get("/health").headers["X-Request-ID"] for _ in range(3)}
    assert len(ids) == 3


def test_messages_extras_and_tracebacks_are_redacted(rendered):
    log = logging.getLogger("apichain.test")
    log.warning("sms to %s failed", PHONE, extra={"first_name": FIRST_NAME, "farmer_id": 7})
    try:
        raise ValueError(f"Key (phone)=({LOCAL_PHONE}) already exists; contact {EMAIL}")
    except ValueError:
        log.exception("enrolment failed")

    warning, error = rendered()[-2:]
    assert warning["event"] == f"sms to {REDACTED} failed"
    assert warning["first_name"] == REDACTED
    assert warning["farmer_id"] == 7
    assert "ValueError" in error["exception"]
    raw = rendered.raw()
    for value in (DIGITS, FIRST_NAME, EMAIL):
        assert value not in raw


def _failing_app() -> FastAPI:
    """A farmer request that fails: its body, its token and the exception
    text all carry the farmer's details."""
    app = FastAPI()
    app.add_middleware(RequestId)

    @app.post("/farmers")
    async def enrol(request: Request) -> None:
        body = await request.json()
        logging.getLogger("apichain.test").warning("enrolling %s", body["phone"])
        raise RuntimeError(f"duplicate farmer {body['phone']} <{body['email']}>")

    return app


def _post_failing(app: FastAPI):
    client = TestClient(app, raise_server_exceptions=False)
    return client.post(
        "/farmers",
        json={"first_name": FIRST_NAME, "phone": PHONE, "email": EMAIL, "password": PASSWORD},
        headers={"Authorization": "Bearer secret-access-token", "X-Request-ID": "walk-0002-boom"},
    )


def test_an_unhandled_error_is_logged_with_its_request_id(rendered):
    res = _post_failing(_failing_app())
    assert res.status_code == 500

    lines = rendered()
    boom = next(line for line in lines if line["event"] == "unhandled error")
    assert boom["request_id"] == "walk-0002-boom"
    assert "RuntimeError" in boom["exception"]
    access = [line for line in lines if line["logger"] == "apichain.access"][-1]
    assert access["status"] == 500
    assert DIGITS not in rendered.raw()


# --- Sentry ----------------------------------------------------------------------


class _Capture(Transport):
    def __init__(self) -> None:
        super().__init__()
        self.events: list[dict] = []

    def capture_envelope(self, envelope) -> None:
        for item in envelope.items:
            if item.type == "event":
                self.events.append(item.payload.json)


@pytest.fixture
def sentry_events():
    transport = _Capture()
    assert init_sentry(dsn="https://public@sentry.example.invalid/1", transport=transport)
    yield transport.events
    sentry_sdk.get_client().close()
    for scope in (
        sentry_sdk.get_global_scope(),
        sentry_sdk.get_isolation_scope(),
        sentry_sdk.get_current_scope(),
    ):
        scope.set_client(None)


def test_sentry_does_not_start_without_a_dsn(monkeypatch):
    monkeypatch.setattr(settings, "sentry_dsn", None)
    assert init_sentry() is False
    assert not sentry_sdk.get_client().is_active()


def test_a_sentry_event_from_a_farmer_request_holds_no_personal_data(sentry_events):
    _post_failing(_failing_app())

    errors = [e for e in sentry_events if e.get("exception")]
    assert errors, "the failing request produced no Sentry event"
    event = errors[-1]
    assert event["tags"]["request_id"] == "walk-0002-boom"
    assert event["exception"]["values"][-1]["type"] == "RuntimeError"

    sent = json.dumps(sentry_events)
    for value in (DIGITS, FIRST_NAME, EMAIL, PASSWORD, "secret-access-token"):
        assert value not in sent, value
    # The log line logged before the failure travelled as a breadcrumb, masked.
    crumbs = [c["message"] for c in event["breadcrumbs"]["values"] if c.get("message")]
    assert f"enrolling {REDACTED}" in crumbs
