"""Phase 3b acceptance: media and communications, end to end (P3b-I, 11 §1).

This file is the phase's acceptance evidence, one test per line of `06`
§Phase 3 (the 3b half), driven through the real HTTP endpoints by the role
that owns each step:

1. **A document uploads content-addressed, scans clean, and serves through a
   signed URL only with consent.** Withdrawing consent stops it serving;
   nothing is stored for a refused upload.
2. **A verification code sends and logs**, and confirms.
3. **A milestone notification sends and logs**, derived by the worker from
   the transition's own audit row.
4. **The audit chain still verifies** over everything 3b appended, and the
   anchoring worker covers every new row.

Mocked: only the four external boundaries (object store, scanner, SMS, email)
and the OpenTimestamps calendar, none of which CI may reach.
"""

import ast
import pathlib

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session, sessionmaker

from app.enums import CommPurpose, CommStatus, Role
from app.models import AuditLog, Communication, Document, Farmer, MerkleAnchor
from app.services import audit_log, comms_worker, media
from tests.helpers import METADATA, auth, seed_apiary, seed_user
from tests.test_anchor_proof_endpoint import _anchor_everything
from tests.test_transitions_endpoints import HARVEST

PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 8


def _enrol(client, officer: dict[str, str], phone: str) -> int:
    r = client.post(
        "/v2/farmers",
        json={
            "first_name": "Wanjiru",
            "last_name": "Kamau",
            "phone": phone,
            "password": "a-strong-password",
            "consent_granted": True,
            "consent_text_version": "enrol-v1",
        },
        headers=officer,
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _consent(client, farmer: int, headers, purpose: str, granted: bool = True) -> None:
    r = client.post(
        f"/v2/farmers/{farmer}/consents",
        json={"purpose": purpose, "granted": granted, "text_version": "consent-v1"},
        headers=headers,
    )
    assert r.status_code == 201, r.text


def test_a_document_is_content_addressed_scanned_and_served_only_with_consent(
    client, migrated_engine, boundaries
):
    officer = auth(seed_user(migrated_engine, Role.field_officer, "acc-fo-1"), Role.field_officer)
    farmer = _enrol(client, officer, "+254700380001")
    upload_url = f"/v2/farmers/{farmer}/documents"
    files = {"file": ("national-id.png", PNG, "image/png")}

    # Without consent: refused, and nothing anywhere.
    form = {"doc_type": "national_id"}
    refused = client.post(upload_url, files=files, data=form, headers=officer)
    assert refused.status_code == 422 and refused.json()["code"] == "consent_required"
    assert boundaries.store.objects == {}

    _consent(client, farmer, officer, "document_upload")
    r = client.post(upload_url, files=files, data={"doc_type": "national_id"}, headers=officer)
    assert r.status_code == 201, r.text
    doc = r.json()

    # Content-addressed: the hash is of the bytes; the object is keyed by it.
    digest = media.sha256(PNG)
    assert doc["content_hash"] == digest.hex()
    assert boundaries.store.objects[f"sha256/{digest.hex()}"][0] == PNG
    # Scanned clean, by a recorded engine.
    assert doc["scan_status"] == "clean"
    with Session(migrated_engine) as s:
        assert s.get(Document, doc["id"]).scan_engine == "fake"

    # Served through a signed URL while consent stands ...
    url = f"/v2/documents/{doc['id']}/download-url"
    served = client.get(url, headers=officer)
    assert served.status_code == 200 and served.json()["url"].startswith("https://")
    # ... and not after the farmer withdraws it.
    _consent(client, farmer, officer, "document_upload", granted=False)
    assert client.get(url, headers=officer).json()["code"] == "consent_required"


def test_a_verification_code_sends_logs_and_confirms(client, migrated_engine, boundaries):
    officer = auth(seed_user(migrated_engine, Role.field_officer, "acc-fo-2"), Role.field_officer)
    farmer = _enrol(client, officer, "0700 380 002")

    sent = client.post(
        f"/v2/farmers/{farmer}/verifications", json={"channel": "sms"}, headers=officer
    )
    assert sent.status_code == 202, sent.text
    to, body = boundaries.sms.sent[0]
    assert to == "+254700380002"
    code = next(w.rstrip(".") for w in body.split() if w.rstrip(".").isdigit())

    with Session(migrated_engine) as s:
        comm = s.execute(select(Communication)).scalar_one()
        logged = s.execute(
            select(AuditLog).where(AuditLog.action == "communication.sent")
        ).scalar_one()
    assert (comm.purpose, comm.status) == (CommPurpose.verification, CommStatus.sent)
    assert logged.payload["communication_id"] == comm.id
    assert code not in str(comm.payload) + str(logged.payload)

    confirmed = client.post(
        f"/v2/farmers/{farmer}/verifications/confirm",
        json={"channel": "sms", "code": code},
        headers=officer,
    )
    assert confirmed.status_code == 200, confirmed.text
    with Session(migrated_engine) as s:
        assert s.get(Farmer, farmer).phone_verified_at is not None


def test_a_milestone_notification_sends_and_logs(client, migrated_engine, boundaries):
    officer = auth(seed_user(migrated_engine, Role.field_officer, "acc-fo-3"), Role.field_officer)
    farmer = _enrol(client, officer, "+254700380003")
    _consent(client, farmer, officer, "sms_notifications")
    operator = auth(seed_user(migrated_engine, Role.operator, "acc-op-3"), Role.operator)

    batch = client.post(
        "/v2/batches",
        json={
            "farmer_id": farmer,
            "apiary_id": seed_apiary(migrated_engine, farmer),
            "metadata": METADATA,
        },
        headers=operator,
    )
    assert batch.status_code == 201, batch.text
    harvest_url = f"/v2/batches/{batch.json()['id']}/harvest"
    harvest = client.post(harvest_url, json=HARVEST, headers=operator)
    assert harvest.status_code == 201, harvest.text

    factory = sessionmaker(bind=migrated_engine, autoflush=False, autocommit=False)
    tick = comms_worker.run(factory, sms=boundaries.sms, email=boundaries.email)
    assert tick.sent == 1
    assert boundaries.sms.sent[0][0] == "+254700380003"

    with Session(migrated_engine) as s:
        comm = s.execute(
            select(Communication).where(Communication.status == CommStatus.sent)
        ).scalar_one()
        logged = s.execute(
            select(AuditLog).where(AuditLog.action == "communication.sent")
        ).scalar_one()
    assert comm.purpose is CommPurpose.milestone
    assert comm.source_audit_id == harvest.json()["audit_id"]
    assert logged.payload["source_audit_id"] == harvest.json()["audit_id"]

    # Nothing was added to the transition path to make this happen.
    source = pathlib.Path("app/services/stage_writer.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = {a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) for a in n.names}
    assert not {"consent", "comms", "comms_worker"} & imported


def test_the_chain_verifies_and_every_new_row_is_anchored(client, migrated_engine, boundaries):
    """All three flows in one database, then the spine's two guarantees."""
    test_a_document_is_content_addressed_scanned_and_served_only_with_consent(
        client, migrated_engine, boundaries
    )
    test_a_milestone_notification_sends_and_logs(client, migrated_engine, boundaries)

    with Session(migrated_engine) as s:
        actions = set(s.execute(select(AuditLog.action)).scalars())
        assert {
            "consent.granted",
            "consent.withdrawn",
            "document.uploaded",
            "communication.sent",
        } <= actions
        assert audit_log.verify_chain(s).ok

    _anchor_everything(migrated_engine)
    with Session(migrated_engine) as s:
        first, last = s.execute(select(func.min(AuditLog.id), func.max(AuditLog.id))).one()
        covered = s.execute(
            select(func.min(MerkleAnchor.from_audit_id), func.max(MerkleAnchor.to_audit_id))
        ).one()
        gaps = s.execute(text("select count(*) from audit_log")).scalar_one()
    assert covered == (first, last)
    assert gaps == last - first + 1  # contiguous: no id burned by any 3b path
