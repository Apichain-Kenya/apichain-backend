"""Milestone notifications derived from the audit log (P3b-H, 11 §10, 11 D8).

The worker is called directly with a session factory, as `run_stamp` is. No
scheduler runs in tests. Batches are driven through the real transition
endpoints, so a test here proves the notification follows from the
transition's own audit row with nothing added to the transition.
"""

import ast
import pathlib
import threading

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings
from app.enums import CommChannel, CommStatus, ConsentPurpose, GrantedVia, Role
from app.models import AuditLog, Communication
from app.services import comms_worker, consent
from tests.fakes import RecordingEmail, RecordingSms
from tests.helpers import auth, create_batch, seed_user
from tests.test_transitions_endpoints import HARVEST


@pytest.fixture
def factory(migrated_engine):
    return sessionmaker(bind=migrated_engine, autoflush=False, autocommit=False)


def _consent(engine, farmer_id: int, purpose: ConsentPurpose, granted: bool = True) -> None:
    with Session(engine) as s:
        consent.record_consent(
            s,
            subject_type="farmer",
            subject_id=farmer_id,
            purpose=purpose,
            granted=granted,
            granted_via=GrantedVia.farmer_self,
            text_version="v1",
        )
        s.commit()


def _harvested(client, engine, phone: str, *, email: str | None = None) -> tuple[int, int]:
    """A batch driven to HARVESTED through the real endpoint. Returns
    (farmer_id, harvest audit id)."""
    batch = create_batch(client, engine, phone=phone)
    with Session(engine) as s:
        farmer_id = s.execute(
            text("select farmer_id from honey_batches where id = :b"), {"b": batch["id"]}
        ).scalar_one()
        if email:
            s.execute(
                text("update farmers set email = :e where id = :f"), {"e": email, "f": farmer_id}
            )
            s.commit()
    op = auth(seed_user(engine, Role.operator, f"wop{phone[-6:]}"), Role.operator)
    r = client.post(f"/v2/batches/{batch['id']}/harvest", json=HARVEST, headers=op)
    assert r.status_code == 201, r.text
    return farmer_id, r.json()["audit_id"]


def _rows(engine) -> list[Communication]:
    with Session(engine) as s:
        return list(s.execute(select(Communication).order_by(Communication.id)).scalars())


def _sent_audits(engine) -> list[AuditLog]:
    with Session(engine) as s:
        return list(
            s.execute(select(AuditLog).where(AuditLog.action == "communication.sent")).scalars()
        )


def test_a_harvest_with_consent_sends_once_and_logs(client, migrated_engine, factory):
    farmer, audit_id = _harvested(client, migrated_engine, "+254700370001")
    _consent(migrated_engine, farmer, ConsentPurpose.sms_notifications)
    sms = RecordingSms()

    result = comms_worker.run(factory, sms=sms, email=RecordingEmail())
    assert result.sent == 1
    assert sms.sent[0][0] == "+254700370001"
    assert "has been recorded" in sms.sent[0][1]

    sent = [r for r in _rows(migrated_engine) if r.channel is CommChannel.sms]
    assert len(sent) == 1
    assert sent[0].status is CommStatus.sent and sent[0].source_audit_id == audit_id
    audits = _sent_audits(migrated_engine)
    assert len(audits) == 1 and audits[0].payload["source_audit_id"] == audit_id

    again = comms_worker.run(factory, sms=sms, email=RecordingEmail())
    assert (again.enqueued, again.sent) == (0, 0)
    assert len(sms.sent) == 1


def test_no_consent_no_message_and_no_recipient_stored(client, migrated_engine, factory):
    _harvested(client, migrated_engine, "+254700370002")
    sms = RecordingSms()
    comms_worker.run(factory, sms=sms, email=RecordingEmail())
    assert sms.sent == []
    rows = _rows(migrated_engine)
    assert {r.status for r in rows} == {CommStatus.skipped}
    assert all(r.recipient == "" for r in rows)
    assert _sent_audits(migrated_engine) == []


def test_a_withdrawal_between_enqueue_and_send_is_honoured(client, migrated_engine, factory):
    farmer, _ = _harvested(client, migrated_engine, "+254700370003")
    _consent(migrated_engine, farmer, ConsentPurpose.sms_notifications)
    with factory() as db:
        comms_worker.enqueue(db)
        db.commit()
    _consent(migrated_engine, farmer, ConsentPurpose.sms_notifications, granted=False)

    sms = RecordingSms()
    result = comms_worker.run(factory, sms=sms, email=RecordingEmail())
    assert result.skipped == 1 and sms.sent == []
    assert _sent_audits(migrated_engine) == []


def test_email_milestones_go_to_the_address_on_file(client, migrated_engine, factory):
    farmer, _ = _harvested(client, migrated_engine, "+254700370004", email="bee@example.test")
    _consent(migrated_engine, farmer, ConsentPurpose.email_notifications)
    email = RecordingEmail()
    comms_worker.run(factory, sms=RecordingSms(), email=email)
    assert email.sent[0][0] == "bee@example.test"


def test_failures_retry_up_to_the_bound(client, migrated_engine, factory):
    farmer, _ = _harvested(client, migrated_engine, "+254700370005")
    _consent(migrated_engine, farmer, ConsentPurpose.sms_notifications)
    sms = RecordingSms()
    sms.fail_next = 2

    comms_worker.run(factory, sms=sms, email=RecordingEmail())
    comms_worker.run(factory, sms=sms, email=RecordingEmail())
    third = comms_worker.run(factory, sms=sms, email=RecordingEmail())
    assert third.sent == 1
    row = next(r for r in _rows(migrated_engine) if r.channel is CommChannel.sms)
    assert (row.status, row.attempts) == (CommStatus.sent, 3)


def test_a_permanent_failure_stops_at_the_bound_without_an_audit_row(
    client, migrated_engine, factory
):
    farmer, _ = _harvested(client, migrated_engine, "+254700370006")
    _consent(migrated_engine, farmer, ConsentPurpose.sms_notifications)
    sms = RecordingSms()
    sms.fail_next = 99
    for _ in range(settings.comms_max_attempts + 2):
        comms_worker.run(factory, sms=sms, email=RecordingEmail())
    row = next(r for r in _rows(migrated_engine) if r.channel is CommChannel.sms)
    assert (row.status, row.attempts) == (CommStatus.failed, settings.comms_max_attempts)
    assert _sent_audits(migrated_engine) == []


def test_an_unsendable_number_fails_once_and_is_never_retried(client, migrated_engine, factory):
    farmer, _ = _harvested(client, migrated_engine, "12345")
    _consent(migrated_engine, farmer, ConsentPurpose.sms_notifications)
    sms = RecordingSms()
    comms_worker.run(factory, sms=sms, email=RecordingEmail())
    comms_worker.run(factory, sms=sms, email=RecordingEmail())
    row = next(r for r in _rows(migrated_engine) if r.channel is CommChannel.sms)
    assert (row.status, row.error) == (CommStatus.failed, "invalid_recipient")
    assert sms.sent == []


def test_a_stale_claim_is_reclaimed_and_a_fresh_one_is_not(client, migrated_engine, factory):
    farmer, _ = _harvested(client, migrated_engine, "+254700370007")
    _consent(migrated_engine, farmer, ConsentPurpose.sms_notifications)
    with factory() as db:
        comms_worker.enqueue(db)
        db.commit()
    with factory() as db:
        assert comms_worker.claim(db)  # a worker claims, then "dies"
        db.commit()

    sms = RecordingSms()
    assert comms_worker.run(factory, sms=sms, email=RecordingEmail()).sent == 0  # fresh claim

    with Session(migrated_engine) as s:
        s.execute(update(Communication).values(claimed_at=text("claimed_at - interval '1 hour'")))
        s.commit()
    assert comms_worker.run(factory, sms=sms, email=RecordingEmail()).sent == 1


def test_two_workers_send_each_message_once(client, migrated_engine, factory):
    farmer, _ = _harvested(client, migrated_engine, "+254700370008")
    _consent(migrated_engine, farmer, ConsentPurpose.sms_notifications)
    sms = RecordingSms()
    barrier = threading.Barrier(2)

    def tick() -> None:
        barrier.wait()
        comms_worker.run(factory, sms=sms, email=RecordingEmail())

    threads = [threading.Thread(target=tick) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert len(sms.sent) == 1
    assert len(_sent_audits(migrated_engine)) == 1


def test_a_milestone_outside_the_lookback_is_ignored(client, migrated_engine, factory):
    farmer, _ = _harvested(client, migrated_engine, "+254700370009")
    _consent(migrated_engine, farmer, ConsentPurpose.sms_notifications)
    with Session(migrated_engine) as s:
        # Only created_at moves; the chain does not commit to it being recent.
        s.execute(text("update audit_log set created_at = created_at - interval '10 days'"))
        s.commit()
    assert comms_worker.run(factory, sms=RecordingSms(), email=RecordingEmail()).enqueued == 0
    assert _rows(migrated_engine) == []


def test_a_late_committing_lower_id_is_still_notified(client, migrated_engine, factory):
    """The case a cursor would miss: audit row N+1 is processed, then row N
    appears. The anti-join finds it anyway."""
    first, _ = _harvested(client, migrated_engine, "+254700370010")
    second, second_audit = _harvested(client, migrated_engine, "+254700370011")
    for farmer in (first, second):
        _consent(migrated_engine, farmer, ConsentPurpose.sms_notifications)
    # Hide the *higher* id's row from the first tick by pretending the lower
    # one is the late arrival: process with the lower row filtered out.
    with Session(migrated_engine) as s:
        low = (
            s.execute(
                select(AuditLog.id)
                .where(AuditLog.action == "batch.harvest_recorded")
                .order_by(AuditLog.id)
            )
            .scalars()
            .first()
        )
        s.execute(text("update audit_log set action = 'held' where id = :i"), {"i": low})
        s.commit()
    sms = RecordingSms()
    comms_worker.run(factory, sms=sms, email=RecordingEmail())
    assert len(sms.sent) == 1
    with Session(migrated_engine) as s:
        s.execute(
            text("update audit_log set action = 'batch.harvest_recorded' where id = :i"),
            {"i": low},
        )
        s.commit()
    comms_worker.run(factory, sms=sms, email=RecordingEmail())
    assert len(sms.sent) == 2


def test_the_transition_handlers_know_nothing_about_consent_or_comms():
    """11 D2 / D8: nothing was added to the transition path."""
    source = pathlib.Path("app/services/stage_writer.py").read_text(encoding="utf-8")
    imported = {
        alias.name
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }
    assert not {"consent", "comms", "comms_worker", "sms", "mailer"} & imported
