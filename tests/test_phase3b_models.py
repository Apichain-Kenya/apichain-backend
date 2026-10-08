"""P3b-C: the media and communications tables (11 §6).

Two constraints are the point of this file:

- `documents` is unique per **(subject, content_hash)**, not per hash (11 D3).
  A global unique would refuse the second farmer to upload a common file, and
  the "already uploaded" answer would tell farmer B what farmer A holds.
  Storage still deduplicates fully: both rows share one `object_key`.
- `communications` is unique per **(source_audit_id, channel)**, the
  milestone worker's idempotency backstop (11 D8). Verification rows carry no
  source audit id, and Postgres treats NULLs as distinct, so they never clash.
"""

import datetime as dt

import pytest
from sqlalchemy.exc import IntegrityError

from app.enums import CommChannel, CommPurpose, CommStatus, ScanStatus
from app.models import Communication, Document, Farmer, VerificationCode

_HASH = bytes.fromhex("ab" * 32)


def _farmer(db, phone: str) -> int:
    farmer = Farmer(first_name="A", last_name="B", phone=phone)
    db.add(farmer)
    db.flush()
    return farmer.id


def _document(subject_id: int, content_hash: bytes = _HASH) -> Document:
    return Document(
        subject_type="farmer",
        subject_id=subject_id,
        doc_type="national_id",
        content_hash=content_hash,
        content_type="image/png",
        size_bytes=1024,
        original_filename="id.png",
        object_key=f"sha256/{content_hash.hex()}",
        scan_status=ScanStatus.clean,
        scan_engine="fake",
        scanned_at=dt.datetime.now(dt.UTC),
        uploaded_by=1,
    )


def _milestone(subject_id: int, audit_id: int | None, channel: CommChannel) -> Communication:
    return Communication(
        channel=channel,
        purpose=CommPurpose.milestone if audit_id else CommPurpose.verification,
        subject_type="farmer",
        subject_id=subject_id,
        recipient="+254700000001",
        template_key="milestone.harvest_recorded",
        template_version=1,
        locale="en",
        payload={"batch_code": "B-1"},
        source_audit_id=audit_id,
        status=CommStatus.queued,
    )


def test_one_farmer_cannot_hold_the_same_bytes_twice(db):
    farmer = _farmer(db, "+254700330001")
    db.add(_document(farmer))
    db.flush()
    db.add(_document(farmer))
    with pytest.raises(IntegrityError):
        db.flush()


def test_two_farmers_may_hold_the_same_bytes_under_one_object(db):
    a, b = _farmer(db, "+254700330002"), _farmer(db, "+254700330003")
    first, second = _document(a), _document(b)
    db.add_all([first, second])
    db.flush()
    assert first.object_key == second.object_key


def test_a_milestone_is_recorded_once_per_channel(db):
    farmer = _farmer(db, "+254700330004")
    db.add_all([_milestone(farmer, 41, CommChannel.sms), _milestone(farmer, 41, CommChannel.email)])
    db.flush()
    db.add(_milestone(farmer, 41, CommChannel.sms))
    with pytest.raises(IntegrityError):
        db.flush()


def test_rows_without_a_source_audit_id_never_clash(db):
    farmer = _farmer(db, "+254700330005")
    db.add_all(
        [_milestone(farmer, None, CommChannel.sms), _milestone(farmer, None, CommChannel.sms)]
    )
    db.flush()


def test_a_tz_aware_expiry_is_stored_as_naive_utc(db):
    """Expiry is compared in Python on one clock (11 §3); the column must
    store the instant it was given, whatever offset it arrived in."""
    farmer = _farmer(db, "+254700330006")
    nairobi = dt.timezone(dt.timedelta(hours=3))
    code = VerificationCode(
        farmer_id=farmer,
        channel=CommChannel.sms,
        code_hmac=_HASH,
        expires_at=dt.datetime(2026, 10, 8, 12, 10, tzinfo=nairobi),
    )
    db.add(code)
    db.flush()
    db.expire(code)
    assert code.expires_at == dt.datetime(2026, 10, 8, 9, 10)
    assert code.attempts == 0


def test_farmers_carry_contact_verification_timestamps(db):
    farmer = db.get(Farmer, _farmer(db, "+254700330007"))
    assert farmer.phone_verified_at is None and farmer.email_verified_at is None


def test_contact_columns_are_tagged_as_pii():
    assert Communication.__table__.c.recipient.info == {"pii": "contact"}
    assert Document.__table__.c.original_filename.info == {"pii": "identity"}


def test_the_migrations_match_the_models(migrated_engine):
    """The 3b migration is hand-written (no scratch DB to autogenerate
    against), so this is what proves it says the same thing as the models:
    autogenerate's comparison, run against the migrated test database, must
    find nothing to do."""
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    from app.database import Base

    with migrated_engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    assert diff == []
