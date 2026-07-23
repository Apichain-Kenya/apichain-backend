"""Consent capture + gate (P1-E, 08 D10). Two modes: capture-and-validate
(enrollment, Phase 1) and check-existing (upload, Phase 3)."""

import pytest

from app.errors import APIError
from app.models import ConsentPurpose, ConsentRecord, GrantedVia
from app.services import consent


def test_capture_rejects_when_not_granted(db):
    with pytest.raises(APIError) as ei:
        consent.capture_consent(
            db,
            subject_type="farmer",
            subject_id=1,
            purpose=ConsentPurpose.data_processing,
            granted=False,
            granted_via=GrantedVia.onboarder,
            text_version="v1",
        )
    assert ei.value.code == "consent_required"
    assert db.query(ConsentRecord).count() == 0  # nothing written


def test_capture_writes_row_when_granted(db):
    row = consent.capture_consent(
        db,
        subject_type="farmer",
        subject_id=1,
        purpose=ConsentPurpose.data_processing,
        granted=True,
        granted_via=GrantedVia.onboarder,
        text_version="v1",
    )
    assert row.id is not None
    assert row.granted is True
    assert row.text_version == "v1"


def test_require_existing_raises_when_absent(db):
    with pytest.raises(APIError) as ei:
        consent.require_consent(
            db, subject_type="farmer", subject_id=99, purpose=ConsentPurpose.document_upload
        )
    assert ei.value.code == "consent_required"


def test_require_existing_passes_when_present(db):
    consent.capture_consent(
        db,
        subject_type="farmer",
        subject_id=5,
        purpose=ConsentPurpose.document_upload,
        granted=True,
        granted_via=GrantedVia.farmer_self,
        text_version="v2",
    )
    row = consent.require_consent(
        db, subject_type="farmer", subject_id=5, purpose=ConsentPurpose.document_upload
    )
    assert row.granted is True
