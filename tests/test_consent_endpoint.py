"""The consent surface (P3b-B, 11 §5, 11 D1).

Two things were missing before 3b, and either alone made the document-upload
gate useless:

1. `require_consent` picked the newest *granted* row, so a withdrawal was
   filtered out before "newest" was decided and a farmer who withdrew still
   passed. The newest row for (subject, purpose) now decides, either way.
2. Nothing could record any purpose except `data_processing` (enrolment), and
   nothing could record a withdrawal at all, so the gate was unsatisfiable and
   03 §7's opt-out had no path.
"""

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.enums import ConsentPurpose, GrantedVia, Role
from app.errors import APIError
from app.models import AuditLog, ConsentRecord
from app.services import consent
from tests.helpers import auth, seed_farmer, seed_user


def _record(db, purpose: ConsentPurpose, granted: bool, subject_id: int = 7) -> None:
    consent.record_consent(
        db,
        subject_type="farmer",
        subject_id=subject_id,
        purpose=purpose,
        granted=granted,
        granted_via=GrantedVia.onboarder,
        text_version="v1",
    )


def _gate(db, purpose: ConsentPurpose = ConsentPurpose.document_upload, subject_id: int = 7):
    return consent.require_consent(
        db, subject_type="farmer", subject_id=subject_id, purpose=purpose
    )


# --- the semantic fix -------------------------------------------------------


def test_a_withdrawal_after_a_grant_closes_the_gate(db):
    _record(db, ConsentPurpose.document_upload, True)
    _record(db, ConsentPurpose.document_upload, False)
    with pytest.raises(APIError) as ei:
        _gate(db)
    assert ei.value.code == "consent_required"
    assert (
        consent.has_consent(
            db, subject_type="farmer", subject_id=7, purpose=ConsentPurpose.document_upload
        )
        is False
    )


def test_a_regrant_after_a_withdrawal_reopens_it(db):
    _record(db, ConsentPurpose.document_upload, True)
    _record(db, ConsentPurpose.document_upload, False)
    _record(db, ConsentPurpose.document_upload, True)
    assert _gate(db).granted is True


def test_a_lone_withdrawal_is_not_consent(db):
    _record(db, ConsentPurpose.document_upload, False)
    with pytest.raises(APIError):
        _gate(db)


def test_purposes_and_subjects_are_independent(db):
    _record(db, ConsentPurpose.document_upload, True)
    _record(db, ConsentPurpose.sms_notifications, False)
    _record(db, ConsentPurpose.document_upload, False, subject_id=8)
    assert _gate(db).granted is True
    with pytest.raises(APIError):
        _gate(db, subject_id=8)


# --- the endpoint -----------------------------------------------------------


def _farmer_with_login(engine, phone: str) -> tuple[int, dict[str, str]]:
    farmer = seed_farmer(engine, phone)
    user = seed_user(engine, Role.farmer, f"f{phone[-6:]}", farmer_id=farmer)
    return farmer, auth(user, Role.farmer)


def _officer(engine, name: str) -> dict[str, str]:
    return auth(seed_user(engine, Role.field_officer, name), Role.field_officer)


def _body(purpose: str = "document_upload", granted: bool = True) -> dict:
    return {"purpose": purpose, "granted": granted, "text_version": "consent-v1"}


def _counts(engine) -> tuple[int, int]:
    with Session(engine) as s:
        return (
            s.execute(text("select count(*) from consent_records")).scalar_one(),
            s.execute(text("select count(*) from audit_log")).scalar_one(),
        )


def test_grant_then_withdraw_each_write_a_row_and_an_audit_row(client, migrated_engine):
    farmer, headers = _farmer_with_login(migrated_engine, "+254700320001")
    url = f"/v2/farmers/{farmer}/consents"

    r1 = client.post(url, json=_body(granted=True), headers=headers)
    assert r1.status_code == 201, r1.text
    r2 = client.post(url, json=_body(granted=False), headers=headers)
    assert r2.status_code == 201, r2.text
    assert r2.json()["granted"] is False

    with Session(migrated_engine) as s:
        actions = s.execute(select(AuditLog.action).order_by(AuditLog.id)).scalars().all()
        rows = s.execute(select(ConsentRecord).order_by(ConsentRecord.id)).scalars().all()
    assert actions == ["consent.granted", "consent.withdrawn"]
    assert [r.granted for r in rows] == [True, False]

    current = {c["purpose"]: c for c in client.get(url, headers=headers).json()["consents"]}
    assert current["document_upload"]["granted"] is False
    assert current["document_upload"]["recorded_at"] is not None
    assert set(current) == {p.value for p in ConsentPurpose}


def test_granted_via_is_derived_from_the_actor(client, migrated_engine):
    farmer, farmer_headers = _farmer_with_login(migrated_engine, "+254700320002")
    url = f"/v2/farmers/{farmer}/consents"
    client.post(url, json=_body("sms_notifications"), headers=farmer_headers)
    client.post(url, json=_body("email_notifications"), headers=_officer(migrated_engine, "fo2"))

    with Session(migrated_engine) as s:
        via = dict(
            s.execute(select(ConsentRecord.consent_purpose, ConsentRecord.granted_via)).all()
        )
    assert via[ConsentPurpose.sms_notifications] is GrantedVia.farmer_self
    assert via[ConsentPurpose.email_notifications] is GrantedVia.onboarder


def test_a_client_cannot_claim_granted_via(client, migrated_engine):
    farmer, headers = _farmer_with_login(migrated_engine, "+254700320003")
    r = client.post(
        f"/v2/farmers/{farmer}/consents",
        json={**_body(), "granted_via": "farmer_self"},
        headers=_officer(migrated_engine, "fo3"),
    )
    assert r.status_code == 422, r.text
    assert _counts(migrated_engine) == (0, 0)


def test_data_processing_cannot_be_withdrawn_here(client, migrated_engine):
    farmer, headers = _farmer_with_login(migrated_engine, "+254700320004")
    r = client.post(
        f"/v2/farmers/{farmer}/consents",
        json=_body("data_processing", granted=False),
        headers=headers,
    )
    assert r.status_code == 422
    assert r.json()["code"] == "withdrawal_not_supported"
    assert _counts(migrated_engine) == (0, 0)


def test_a_farmer_cannot_record_consent_for_another_farmer(client, migrated_engine):
    _, headers = _farmer_with_login(migrated_engine, "+254700320005")
    other = seed_farmer(migrated_engine, "+254700320006")
    r = client.post(f"/v2/farmers/{other}/consents", json=_body(), headers=headers)
    assert r.status_code == 403
    assert client.get(f"/v2/farmers/{other}/consents", headers=headers).status_code == 403
    assert _counts(migrated_engine) == (0, 0)


def test_operators_have_no_consent_access(client, migrated_engine):
    farmer = seed_farmer(migrated_engine, "+254700320007")
    op = auth(seed_user(migrated_engine, Role.operator, "op7"), Role.operator)
    r = client.post(f"/v2/farmers/{farmer}/consents", json=_body(), headers=op)
    assert r.status_code == 403


def test_unknown_farmer_is_404(client, migrated_engine):
    officer = _officer(migrated_engine, "fo8")
    r = client.post("/v2/farmers/999999/consents", json=_body(), headers=officer)
    assert r.status_code == 404


def test_a_nul_byte_in_text_version_is_refused_at_the_edge(client, migrated_engine):
    farmer, headers = _farmer_with_login(migrated_engine, "+254700320009")
    r = client.post(
        f"/v2/farmers/{farmer}/consents",
        json={**_body(), "text_version": "v\x001"},
        headers=headers,
    )
    assert r.status_code == 422


def test_staff_cannot_reverse_a_farmers_own_withdrawal(client, migrated_engine):
    """A data subject's own opt-out is reversible only by the data subject.
    Otherwise an officer re-granting `sms_notifications` silently undoes a
    farmer's "stop texting me" (03 §9), and the ledger would show consent the
    farmer explicitly took back."""
    farmer, farmer_headers = _farmer_with_login(migrated_engine, "+254700320010")
    url = f"/v2/farmers/{farmer}/consents"
    officer = _officer(migrated_engine, "fo10")

    client.post(url, json=_body("sms_notifications", True), headers=officer)
    client.post(url, json=_body("sms_notifications", False), headers=farmer_headers)
    before = _counts(migrated_engine)

    r = client.post(url, json=_body("sms_notifications", True), headers=officer)
    assert r.status_code == 409
    assert r.json()["code"] == "withdrawn_by_farmer"
    assert _counts(migrated_engine) == before

    # The farmer can change their own mind.
    r = client.post(url, json=_body("sms_notifications", True), headers=farmer_headers)
    assert r.status_code == 201


def test_staff_may_correct_a_withdrawal_staff_recorded(client, migrated_engine):
    farmer, _ = _farmer_with_login(migrated_engine, "+254700320011")
    url = f"/v2/farmers/{farmer}/consents"
    officer = _officer(migrated_engine, "fo11")
    client.post(url, json=_body("sms_notifications", False), headers=officer)
    r = client.post(url, json=_body("sms_notifications", True), headers=officer)
    assert r.status_code == 201
