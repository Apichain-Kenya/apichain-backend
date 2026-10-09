"""The communications core: frozen templates and the one send path (P3b-F, 11 §8).

The properties that matter: consent is checked with the right purpose for the
right kind of message; a send writes `communications` **and** one
`communication.sent` audit row; a skip or a failure writes no audit row and
burns no audit id; and the verification code reaches the provider without
reaching the database.
"""

import json

import pytest
from sqlalchemy import select, text

from app.enums import CommChannel, CommPurpose, CommStatus, ConsentPurpose, GrantedVia
from app.models import AuditLog, Communication, Farmer
from app.services import comms, comms_templates, consent
from tests.fakes import RecordingEmail, RecordingSms

# GSM 03.38 basic character set: one character outside it and an SMS is
# billed as UCS-2, whose single-part limit is 70, not 160.
_GSM7 = set(
    "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?"
    "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà"
)

# --- templates ------------------------------------------------------------------


@pytest.mark.parametrize(
    "template", [t for t in comms_templates.TEMPLATES.values() if t.channel is CommChannel.sms]
)
def test_every_sms_fits_one_gsm7_part_at_its_longest(template):
    longest = {name: "W" * limit for name, limit in template.variables.items()}
    _, body = comms_templates.render(template, longest)
    assert len(body) <= comms_templates.SMS_MAX_CHARS, body
    assert set(body) <= _GSM7, set(body) - _GSM7


def test_an_overlong_variable_is_truncated_with_a_gsm7_marker():
    template = comms_templates.get("milestone.distributed", CommChannel.sms)
    _, body = comms_templates.render(template, {"batch_code": "B" * 500})
    assert "B" * 29 + "..." in body
    assert set(body) <= _GSM7


def test_render_takes_exactly_the_declared_variables():
    template = comms_templates.get("verification_code", CommChannel.sms)
    with pytest.raises(KeyError):
        comms_templates.render(template, {"code": "123456"})
    with pytest.raises(KeyError):
        comms_templates.render(template, {"code": "1", "minutes": "10", "phone": "x"})


def test_an_unknown_template_is_a_key_error():
    with pytest.raises(KeyError):
        comms_templates.get("milestone.packaged", CommChannel.sms)
    with pytest.raises(KeyError):
        comms_templates.get("verification_code", CommChannel.sms, "sw")


def test_email_templates_have_subjects_and_sms_ones_do_not():
    for template in comms_templates.TEMPLATES.values():
        assert (template.subject is not None) == (template.channel is CommChannel.email)


def test_only_english_ships():
    assert {t.locale for t in comms_templates.TEMPLATES.values()} == {"en"}


def test_the_lab_milestone_never_states_the_verdict():
    for channel in CommChannel:
        template = comms_templates.get("milestone.lab_verified", channel)
        words = (template.body + (template.subject or "")).lower()
        assert not {"pass", "fail", "verdict", "incomplete"} & set(words.split())


def test_every_purpose_and_channel_pair_has_a_consent_rule():
    assert set(comms.CONSENT_FOR) == {(p, c) for p in CommPurpose for c in CommChannel}
    assert comms.CONSENT_FOR[(CommPurpose.verification, CommChannel.sms)] is (
        ConsentPurpose.data_processing
    )


# --- dispatch -------------------------------------------------------------------


def _farmer(db) -> int:
    farmer = Farmer(first_name="A", last_name="B", phone="+254700350001")
    db.add(farmer)
    db.flush()
    return farmer.id


def _grant(db, farmer_id: int, purpose: ConsentPurpose, granted: bool = True) -> None:
    consent.record_consent(
        db,
        subject_type="farmer",
        subject_id=farmer_id,
        purpose=purpose,
        granted=granted,
        granted_via=GrantedVia.onboarder,
        text_version="v1",
    )


def _comm(db, farmer_id: int, purpose: CommPurpose, channel=CommChannel.sms) -> Communication:
    if purpose is CommPurpose.verification:
        key, payload = "verification_code", {"minutes": "10"}
    else:
        key, payload = "milestone.harvest_recorded", {"batch_code": "B-1"}
    comm = Communication(
        channel=channel,
        purpose=purpose,
        subject_type="farmer",
        subject_id=farmer_id,
        recipient="+254712345678" if channel is CommChannel.sms else "a@b.test",
        template_key=key,
        template_version=1,
        locale="en",
        payload=payload,
        source_audit_id=None,
        status=CommStatus.sending,
    )
    db.add(comm)
    db.flush()
    return comm


def _send(db, comm, sms=None, email=None, secret=None):
    return comms.dispatch(
        db,
        comm,
        sms=sms or RecordingSms(),
        email=email or RecordingEmail(),
        actor_id=None,
        actor_role="system",
        secret_vars=secret,
    )


def _audit_rows(db) -> list[AuditLog]:
    return db.execute(select(AuditLog).order_by(AuditLog.id)).scalars().all()


def test_a_send_logs_and_audits_without_the_code(db):
    farmer = _farmer(db)
    _grant(db, farmer, ConsentPurpose.data_processing)
    comm = _comm(db, farmer, CommPurpose.verification)
    sms = RecordingSms()

    assert _send(db, comm, sms=sms, secret={"code": "482913"}) is CommStatus.sent
    assert "482913" in sms.sent[0][1]  # the provider got it ...
    rows = _audit_rows(db)
    assert [r.action for r in rows] == ["communication.sent"]
    stored = json.dumps(comm.payload) + json.dumps(rows[0].payload)
    assert "482913" not in stored  # ... the database did not
    assert "+254712345678" not in json.dumps(rows[0].payload)
    assert comm.provider == "fake" and comm.provider_message_id == "sms-1"
    assert comm.attempts == 1 and comm.sent_at is not None


def test_a_milestone_needs_the_notification_consent_not_enrolment(db):
    farmer = _farmer(db)
    _grant(db, farmer, ConsentPurpose.data_processing)
    comm = _comm(db, farmer, CommPurpose.milestone)
    sms = RecordingSms()
    assert _send(db, comm, sms=sms) is CommStatus.skipped
    assert comm.error == "no_consent"
    assert sms.sent == [] and _audit_rows(db) == []


def test_a_withdrawn_notification_consent_skips(db):
    farmer = _farmer(db)
    _grant(db, farmer, ConsentPurpose.sms_notifications)
    _grant(db, farmer, ConsentPurpose.sms_notifications, granted=False)
    assert _send(db, _comm(db, farmer, CommPurpose.milestone)) is CommStatus.skipped


def test_email_milestones_use_the_email_consent(db):
    farmer = _farmer(db)
    _grant(db, farmer, ConsentPurpose.sms_notifications)
    comm = _comm(db, farmer, CommPurpose.milestone, CommChannel.email)
    assert _send(db, comm) is CommStatus.skipped
    _grant(db, farmer, ConsentPurpose.email_notifications)
    email = RecordingEmail()
    assert _send(db, comm, email=email) is CommStatus.sent
    assert email.sent[0][1] == "Harvest recorded: batch B-1"


@pytest.mark.parametrize("mode", ["send_failed", "unexpected"])
def test_a_provider_failure_writes_no_audit_and_burns_no_id(db, mode):
    farmer = _farmer(db)
    _grant(db, farmer, ConsentPurpose.sms_notifications)
    comm = _comm(db, farmer, CommPurpose.milestone)
    sms = RecordingSms()
    if mode == "send_failed":
        sms.fail_next = 1
    else:
        sms.raise_unexpected = True
    before = db.execute(text("select last_value from audit_log_id_seq")).scalar_one()

    assert _send(db, comm, sms=sms) is CommStatus.failed
    assert comm.error == ("provider_unreachable" if mode == "send_failed" else "provider_error")
    assert comm.attempts == 1
    assert _audit_rows(db) == []
    after = db.execute(text("select last_value from audit_log_id_seq")).scalar_one()
    assert after == before
