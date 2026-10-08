"""The document endpoints (P3b-E, 11 §11).

What has to hold, in the order the handler enforces it: nothing is stored,
inserted or audited for a refused upload; the stored type is what the bytes
sniffed as; dedup is per farmer in the ledger and global in the bucket; the
bytes are reachable only through a signed URL, only while clean, and only
while consent stands.
"""

import asyncio
from urllib.parse import parse_qs, urlparse

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.config import settings
from app.enums import Role, ScanStatus
from app.models import AuditLog, Document
from app.services import media
from app.services.dev_fakes import EICAR
from tests.helpers import auth, seed_farmer, seed_user

PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 4
PDF = b"%PDF-1.7\n" + b"1 0 obj\n" * 100


def _farmer(engine, phone: str) -> tuple[int, dict[str, str]]:
    farmer = seed_farmer(engine, phone)
    user = seed_user(engine, Role.farmer, f"d{phone[-6:]}", farmer_id=farmer)
    return farmer, auth(user, Role.farmer)


def _grant(client, farmer: int, headers: dict[str, str], granted: bool = True) -> None:
    r = client.post(
        f"/v2/farmers/{farmer}/consents",
        json={"purpose": "document_upload", "granted": granted, "text_version": "v1"},
        headers=headers,
    )
    assert r.status_code == 201, r.text


def _upload(client, farmer, headers, data=PNG, name="id.png", doc_type="national_id", mime=None):
    return client.post(
        f"/v2/farmers/{farmer}/documents",
        files={"file": (name, data, mime or "application/octet-stream")},
        data={"doc_type": doc_type},
        headers=headers,
    )


def _counts(engine) -> tuple[int, int]:
    with Session(engine) as s:
        return (
            s.execute(text("select count(*) from documents")).scalar_one(),
            s.execute(
                text("select count(*) from audit_log where action = 'document.uploaded'")
            ).scalar_one(),
        )


@pytest.fixture
def ready(client, migrated_engine):
    """A farmer with document_upload consent."""
    farmer, headers = _farmer(migrated_engine, "+254700340001")
    _grant(client, farmer, headers)
    return farmer, headers


# --- the happy path and the acceptance property ------------------------------


def test_an_upload_is_content_addressed_scanned_and_audited(
    client, migrated_engine, ready, boundaries
):
    farmer, headers = ready
    r = _upload(client, farmer, headers)
    assert r.status_code == 201, r.text
    body = r.json()
    digest = media.sha256(PNG)
    assert body["content_hash"] == digest.hex()
    assert body["content_type"] == "image/png"
    assert body["scan_status"] == "clean"
    assert boundaries.store.objects[f"sha256/{digest.hex()}"][0] == PNG

    with Session(migrated_engine) as s:
        doc = s.get(Document, body["id"])
        audit = s.execute(
            select(AuditLog).where(AuditLog.action == "document.uploaded")
        ).scalar_one()
    assert doc.object_key == f"sha256/{digest.hex()}"
    assert doc.scan_engine == "fake"
    assert audit.payload["content_hash"] == digest.hex()
    assert "id.png" not in str(audit.payload)  # no filename in the chain


def test_the_signed_url_is_short_lived_and_forces_type_and_attachment(client, ready):
    farmer, headers = ready
    doc = _upload(client, farmer, headers).json()
    r = client.get(f"/v2/documents/{doc['id']}/download-url", headers=headers)
    assert r.status_code == 200, r.text
    query = parse_qs(urlparse(r.json()["url"]).query)
    assert query["response-content-type"] == ["image/png"]
    assert query["response-content-disposition"] == ["attachment"]
    assert r.json()["expires_at"]


def test_withdrawing_consent_hides_the_bytes_and_regranting_restores_them(client, ready):
    farmer, headers = ready
    doc = _upload(client, farmer, headers).json()
    url = f"/v2/documents/{doc['id']}/download-url"

    _grant(client, farmer, headers, granted=False)
    r = client.get(url, headers=headers)
    assert r.status_code == 422
    assert r.json()["code"] == "consent_required"
    # The metadata list is not gated; only the bytes are.
    listing = client.get(f"/v2/farmers/{farmer}/documents", headers=headers)
    assert [d["id"] for d in listing.json()["documents"]] == [doc["id"]]

    _grant(client, farmer, headers)
    assert client.get(url, headers=headers).status_code == 200


# --- dedup ---------------------------------------------------------------------


def test_the_same_bytes_again_return_the_existing_row(client, migrated_engine, ready):
    farmer, headers = ready
    first = _upload(client, farmer, headers)
    again = _upload(client, farmer, headers, name="renamed.png")
    assert (first.status_code, again.status_code) == (201, 200)
    assert again.json()["id"] == first.json()["id"]
    assert _counts(migrated_engine) == (1, 1)


def test_another_farmer_gets_their_own_row_over_the_same_object(
    client, migrated_engine, ready, boundaries
):
    farmer, headers = ready
    other, other_headers = _farmer(migrated_engine, "+254700340002")
    _grant(client, other, other_headers)
    a = _upload(client, farmer, headers).json()
    b = _upload(client, other, other_headers)
    assert b.status_code == 201
    assert b.json()["id"] != a["id"]
    assert boundaries.store.puts == 1
    assert _counts(migrated_engine) == (2, 2)


# --- refusals: nothing stored, nothing written ---------------------------------


def _nothing_written(engine, boundaries) -> None:
    assert _counts(engine) == (0, 0)
    assert boundaries.store.objects == {}


def test_no_consent_no_upload(client, migrated_engine, boundaries):
    farmer, headers = _farmer(migrated_engine, "+254700340003")
    r = _upload(client, farmer, headers)
    assert r.status_code == 422
    assert r.json()["code"] == "consent_required"
    _nothing_written(migrated_engine, boundaries)


def test_an_oversized_file_is_413(client, migrated_engine, ready, boundaries, monkeypatch):
    monkeypatch.setattr(settings, "max_file_bytes", 2048)
    farmer, headers = ready
    r = _upload(client, farmer, headers, data=PNG + b"\x01" * 2048)
    assert r.status_code == 413
    assert r.json()["code"] == "file_too_large"
    _nothing_written(migrated_engine, boundaries)


def test_the_quota_is_per_farmer(client, migrated_engine, ready, boundaries, monkeypatch):
    farmer, headers = ready
    monkeypatch.setattr(settings, "max_subject_bytes", len(PNG) + 10)
    assert _upload(client, farmer, headers).status_code == 201
    r = _upload(client, farmer, headers, data=PDF, name="deed.pdf")
    assert r.status_code == 413
    assert r.json()["code"] == "quota_exceeded"


def test_the_declared_type_is_ignored(client, ready):
    farmer, headers = ready
    r = _upload(client, farmer, headers, data=PDF, name="photo.png", mime="image/png")
    assert r.status_code == 201
    assert r.json()["content_type"] == "application/pdf"


def test_html_is_refused_whatever_it_is_called(client, migrated_engine, ready, boundaries):
    farmer, headers = ready
    r = _upload(client, farmer, headers, data=b"<html><script>x</script>", name="a.pdf")
    assert r.status_code == 415
    _nothing_written(migrated_engine, boundaries)


def test_an_infected_file_is_refused_and_not_stored(client, migrated_engine, ready, boundaries):
    farmer, headers = ready
    r = _upload(client, farmer, headers, data=PNG + EICAR)
    assert r.status_code == 422
    assert r.json()["code"] == "document_rejected"
    _nothing_written(migrated_engine, boundaries)


def test_a_down_scanner_fails_closed(client, migrated_engine, ready, boundaries):
    boundaries.scanner.down = True
    farmer, headers = ready
    r = _upload(client, farmer, headers)
    assert r.status_code == 503
    assert r.json()["code"] == "scanner_unavailable"
    _nothing_written(migrated_engine, boundaries)


def test_a_down_store_writes_no_row_and_burns_no_audit_id(
    client, migrated_engine, ready, boundaries
):
    boundaries.store.down = True
    farmer, headers = ready
    with Session(migrated_engine) as s:
        before = s.execute(text("select max(id) from audit_log")).scalar_one()
    r = _upload(client, farmer, headers)
    assert r.status_code == 503
    assert _counts(migrated_engine) == (0, 0)
    boundaries.store.down = False
    _upload(client, farmer, headers)
    with Session(migrated_engine) as s:
        after = s.execute(text("select max(id) from audit_log")).scalar_one()
    assert after == before + 1  # the next append took the next id: none burned


def test_hostile_filenames_are_stored_sanitized(client, ready):
    farmer, headers = ready
    r = _upload(client, farmer, headers, name="..\\..\\evil‮gpj.exe")
    assert r.status_code == 201
    assert r.json()["original_filename"] == "evilgpj.exe"


@pytest.mark.parametrize("doc_type", ["passport", "national_id\x00"])
def test_doc_type_is_a_closed_vocabulary(client, ready, doc_type):
    farmer, headers = ready
    assert _upload(client, farmer, headers, doc_type=doc_type).status_code == 422


# --- who may act --------------------------------------------------------------


def test_a_farmer_cannot_touch_another_farmers_documents(client, migrated_engine, ready):
    farmer, headers = ready
    doc = _upload(client, farmer, headers).json()
    intruder, intruder_headers = _farmer(migrated_engine, "+254700340004")
    _grant(client, intruder, intruder_headers)

    assert _upload(client, farmer, intruder_headers, data=PDF).status_code == 403
    assert (
        client.get(f"/v2/farmers/{farmer}/documents", headers=intruder_headers).status_code == 403
    )
    r = client.get(f"/v2/documents/{doc['id']}/download-url", headers=intruder_headers)
    assert r.status_code == 403


def test_operators_and_lab_officers_have_no_document_access(client, migrated_engine, ready):
    farmer, _ = ready
    for role in (Role.operator, Role.lab_officer):
        headers = auth(seed_user(migrated_engine, role, f"no-{role}"), role)
        assert _upload(client, farmer, headers).status_code == 403
        assert client.get(f"/v2/farmers/{farmer}/documents", headers=headers).status_code == 403


def test_a_field_officer_may_upload_for_a_farmer(client, migrated_engine, ready):
    farmer, _ = ready
    officer = auth(seed_user(migrated_engine, Role.field_officer, "fo-docs"), Role.field_officer)
    assert _upload(client, farmer, officer).status_code == 201


def test_a_document_that_is_not_clean_is_not_served(client, migrated_engine, ready):
    farmer, headers = ready
    doc = _upload(client, farmer, headers).json()
    with Session(migrated_engine) as s:
        s.get(Document, doc["id"]).scan_status = ScanStatus.infected
        s.commit()
    r = client.get(f"/v2/documents/{doc['id']}/download-url", headers=headers)
    assert r.status_code == 409
    assert r.json()["code"] == "document_not_clean"


@pytest.mark.parametrize(
    "path",
    ["/v2/documents/0/download-url", "/v2/documents/2147483648/download-url"],
)
def test_out_of_range_ids_are_422(client, ready, path):
    _, headers = ready
    assert client.get(path, headers=headers).status_code == 422


def test_unknown_ids_are_404(client, ready):
    farmer, headers = ready
    officer_like = headers
    assert client.get("/v2/documents/999999/download-url", headers=officer_like).status_code == 404


# --- the body-size middleware -----------------------------------------------


def test_a_declared_oversize_body_is_refused_before_it_is_read(monkeypatch):
    """The middleware answers from the header alone: the app never runs and
    not one body chunk is pulled."""
    from app.middleware.body_limit import UploadSizeLimit

    monkeypatch.setattr(settings, "max_file_bytes", 1024)
    pulled: list[int] = []
    app_ran: list[bool] = []

    async def app(scope, receive, send):  # pragma: no cover - must not run
        app_ran.append(True)

    async def receive():
        pulled.append(1)
        return {"type": "http.request", "body": b"x" * 1024, "more_body": True}

    sent: list[dict] = []

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/v2/farmers/1/documents",
        "headers": [(b"content-length", str(10 * 1024 * 1024).encode())],
    }
    asyncio.run(UploadSizeLimit(app)(scope, receive, send))
    assert sent[0]["status"] == 413
    assert pulled == [] and app_ran == []


def test_an_undeclared_oversize_body_is_cut_off_mid_stream(monkeypatch):
    from app.middleware.body_limit import ENVELOPE_BYTES, UploadSizeLimit

    monkeypatch.setattr(settings, "max_file_bytes", 1024)
    pulled: list[int] = []

    async def app(scope, receive, send):
        while True:  # an app that would read forever
            await receive()

    async def receive():
        pulled.append(1)
        return {"type": "http.request", "body": b"x" * 4096, "more_body": True}

    sent: list[dict] = []

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "method": "POST", "path": "/v2/farmers/1/documents", "headers": []}
    asyncio.run(UploadSizeLimit(app)(scope, receive, send))
    assert sent[0]["status"] == 413
    assert len(pulled) * 4096 <= 1024 + ENVELOPE_BYTES + 4096


def test_concurrent_uploads_cannot_both_slip_under_the_quota(
    client, migrated_engine, ready, boundaries, monkeypatch
):
    """The race the security review found: two uploads for one farmer both
    read the quota sum before either inserted, so both passed. The farmer row
    lock serializes them; the second sees the first's bytes and is refused.

    The scanner gate holds upload A mid-flight (past its quota check) while
    upload B starts. Without the lock, B passes the quota check too."""
    import threading
    import time

    farmer, headers = ready
    monkeypatch.setattr(settings, "max_subject_bytes", len(PNG) + len(PDF) - 1)
    boundaries.scanner.gate = threading.Event()
    results: dict[str, int] = {}

    def upload(name: str, data: bytes) -> None:
        results[name] = _upload(client, farmer, headers, data=data, name=name).status_code

    first = threading.Thread(target=upload, args=("a.png", PNG))
    first.start()
    assert boundaries.scanner.entered.wait(timeout=10)
    second = threading.Thread(target=upload, args=("b.pdf", PDF))
    second.start()
    time.sleep(0.5)  # let B reach the quota check (or the lock) while A is held
    boundaries.scanner.gate.set()
    first.join(timeout=15)
    second.join(timeout=15)

    assert sorted(results.values()) == [201, 413], results
    assert _counts(migrated_engine) == (1, 1)
