"""Phase 5a, P5-D: the worker process, `worker_status` and `GET /health/ops`
(14 §1 Acceptance, D8, D9).

These tests own their sessions and commit for real: the status is written in
committed sessions of its own, and a burned audit id cannot be reproduced
inside a transaction that rolls back anyway. Time moves through one fake
clock shared by the anchoring job, the status writer and the alarms, so the
jobs run on the same timeline the alarms judge.
"""

import datetime as dt
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text, update
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings
from app.enums import CommChannel, CommPurpose, CommStatus
from app.models import AuditLog, Communication, RateLimitEvent, WorkerStatus
from app.services import (
    anchoring,
    audit_log,
    integrity,
    jobs,
    ops_health,
    rate_limit,
    worker_status,
)
from app.services.worker_status import (
    ANCHOR_STAMP,
    COMMS,
    INTEGRITY,
    Outcome,
    track,
)
from tests.fakes import FakeCalendar

INTERVAL = 60  # anchor interval for these tests, seconds


@pytest.fixture
def sessions(migrated_engine, monkeypatch):
    monkeypatch.setattr(settings, "anchor_interval_seconds", INTERVAL)
    anchoring.reset()
    integrity.reset()
    factory = sessionmaker(bind=migrated_engine, autoflush=False, autocommit=False)
    try:
        yield factory
    finally:
        anchoring.reset()
        integrity.reset()
        with migrated_engine.begin() as conn:
            conn.execute(
                text(
                    "truncate table worker_status, communications, rate_limit_events, "
                    "merkle_anchor, audit_log restart identity cascade"
                )
            )


@pytest.fixture
def clock(monkeypatch):
    """Moves every clock P5-D reads: the anchoring job's, the status
    writer's and the alarms'. Rows already written keep their real times."""
    offset = {"by": dt.timedelta(0)}
    real = dt.datetime

    class Shifted(dt.datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            return real.now(tz) + offset["by"]

    def naive_now() -> dt.datetime:
        return real.now(dt.UTC).replace(tzinfo=None) + offset["by"]

    monkeypatch.setattr(anchoring, "datetime", Shifted)
    monkeypatch.setattr(worker_status, "_now", naive_now)
    monkeypatch.setattr(ops_health, "_now", naive_now)

    def advance(**delta) -> None:
        offset["by"] += dt.timedelta(**delta)

    return advance


def _commit_row(factory, subject: str) -> int:
    with factory() as s:
        row = audit_log.append(
            s,
            actor_id=None,
            actor_role="admin",
            subject_type="batch",
            subject_id=subject,
            action="batch.created",
            payload={"s": subject},
        )
        s.commit()
        return row.id


def _burn_an_id(factory) -> int:
    with factory() as s:
        row = audit_log.append(
            s,
            actor_id=None,
            actor_role="admin",
            subject_type="batch",
            subject_id="burned",
            action="batch.created",
            payload={},
        )
        burned = row.id
        s.rollback()
        return burned


def _stamp(factory, *, down: bool = False) -> Outcome:
    calendar = FakeCalendar(down=down)
    return track(factory, ANCHOR_STAMP, lambda: jobs.anchor_stamp(factory, calendars=[calendar]))


def _failing(factory) -> list[str]:
    worker_status.heartbeat(factory)
    with factory() as db:
        return ops_health.failing(db)


def _row(factory, job: str) -> WorkerStatus:
    with factory() as db:
        row = db.get(WorkerStatus, job)
        assert row is not None
        return row


@pytest.fixture
def ops(client):
    def get():
        res = client.get("/health/ops")
        return res.status_code, res.json()

    return get


# --- The endpoint ---------------------------------------------------------------


def test_a_worker_that_never_ran_reads_red(ops):
    status, body = ops()
    assert status == 503
    assert body == {"ok": False, "failing": ["worker_down"]}


def test_a_beating_worker_with_nothing_to_do_reads_green(sessions, ops):
    worker_status.heartbeat(sessions)
    assert ops() == (200, {"ok": True, "failing": []})


def test_a_stopped_heartbeat_reads_red(sessions, clock):
    worker_status.heartbeat(sessions)
    clock(seconds=3 * settings.worker_heartbeat_seconds + 1)
    with sessions() as db:
        assert ops_health.failing(db) == ["worker_down"]


def test_the_public_body_names_checks_and_nothing_else(sessions, ops):
    _commit_row(sessions, "1")
    for _ in range(3):
        _stamp(sessions, down=True)
    status, body = ops()
    assert status == 503
    assert set(body) == {"ok", "failing"}
    assert "unreachable" not in json.dumps(body)


# --- Anchoring --------------------------------------------------------------------


def test_three_failed_anchor_runs_turn_red_with_one_sentry_event(sessions, sentry_events):
    _commit_row(sessions, "1")
    for run in range(1, 6):
        _stamp(sessions, down=True)
        red = "anchor_failing" in _failing(sessions)
        assert red is (run >= 3), run
    alarms = [e for e in sentry_events if e.get("tags", {}).get("job") == ANCHOR_STAMP]
    assert len(alarms) == 1

    _stamp(sessions)  # the calendars are back
    assert "anchor_failing" not in _failing(sessions)
    assert _row(sessions, ANCHOR_STAMP).alarmed is False

    for _ in range(3):
        _commit_row(sessions, "more")
        _stamp(sessions, down=True)
    alarms = [e for e in sentry_events if e.get("tags", {}).get("job") == ANCHOR_STAMP]
    assert len(alarms) == 2, "a second outage is a second transition"


def test_a_tick_that_could_not_reach_the_calendars_does_not_count(sessions, migrated_engine):
    """A tick that found the lock held elsewhere (an old instance still
    running during a deploy) did not fail; it must not count towards 3."""
    _commit_row(sessions, "1")
    _stamp(sessions, down=True)
    _stamp(sessions, down=True)

    with Session(migrated_engine) as holder:
        assert anchoring.try_stamp_lock(holder)
        _stamp(sessions, down=True)  # cannot take the lock: neutral
        holder.rollback()

    assert _row(sessions, ANCHOR_STAMP).consecutive_failures == 2
    assert "anchor_failing" not in _failing(sessions)
    _stamp(sessions, down=True)
    assert "anchor_failing" in _failing(sessions)


def test_one_burned_id_never_trips_the_lag_alarm(sessions, clock):
    _commit_row(sessions, "1")
    assert _stamp(sessions).ok is True
    burned = _burn_an_id(sessions)
    live = _commit_row(sessions, "2")

    # The job waits on the gap for two intervals, then skips it. Every tick
    # on the way, the waiting ones included, reads green.
    for step in range(5):
        outcome = _stamp(sessions)
        detail = _row(sessions, ANCHOR_STAMP).detail
        assert _failing(sessions) == [], (step, detail)
        if outcome.ok is True:
            break
        assert outcome.ok is None  # waiting is neutral, not a failure
        assert detail["waiting_on_audit_id"] == burned
        clock(seconds=INTERVAL)
    else:
        pytest.fail("the burned id was never skipped")

    with sessions() as db:
        assert anchoring.backlog(db).last_anchored_audit_id == live
    assert _row(sessions, ANCHOR_STAMP).detail["waiting_since"] is None


def test_rows_left_unanchored_past_four_intervals_turn_red(sessions, clock):
    _commit_row(sessions, "1")
    clock(seconds=4 * INTERVAL - 5)
    assert "anchor_lag" not in _failing(sessions)
    clock(seconds=10)
    assert "anchor_lag" in _failing(sessions)


def test_an_anchor_unconfirmed_after_a_day_turns_red(sessions, clock):
    _commit_row(sessions, "1")
    _stamp(sessions)
    clock(hours=23)
    assert "upgrade_stale" not in _failing(sessions)
    clock(hours=1, minutes=1)
    assert "upgrade_stale" in _failing(sessions)


def test_with_anchoring_off_no_anchor_check_runs(sessions, clock, monkeypatch):
    monkeypatch.setattr(settings, "anchor_enabled", False)
    _commit_row(sessions, "1")
    clock(days=2)
    assert _failing(sessions) == []


# --- The chain, messages, a job that raises ------------------------------------------


def test_a_broken_chain_turns_red_with_a_sentry_event(sessions, sentry_events):
    first = _commit_row(sessions, "1")
    _commit_row(sessions, "2")
    with sessions() as s:
        s.execute(update(AuditLog).where(AuditLog.id == first).values(payload={"s": "EVIL"}))
        s.commit()

    for _ in range(3):
        track(sessions, INTEGRITY, lambda: jobs.integrity_check(sessions))

    assert "chain_broken" in _failing(sessions)
    alarms = [e for e in sentry_events if e.get("tags", {}).get("job") == INTEGRITY]
    assert len(alarms) == 1
    assert str(first) in alarms[0]["message"]


def test_audit_health_reads_the_workers_verdict(sessions, client):
    first = _commit_row(sessions, "1")
    track(sessions, INTEGRITY, lambda: jobs.integrity_check(sessions))
    with sessions() as s:
        s.execute(update(AuditLog).where(AuditLog.id == first).values(payload={"s": "EVIL"}))
        s.commit()
    integrity.reset()
    # The worker has not run since the tamper, so the API reports its last
    # verdict instead of checking again itself.
    assert client.get("/v2/audit/health").json()["chain_ok"] is True
    track(sessions, INTEGRITY, lambda: jobs.integrity_check(sessions))
    assert client.get("/v2/audit/health").json()["chain_ok"] is False


def _message(factory, status: CommStatus) -> None:
    with factory() as s:
        s.add(
            Communication(
                channel=CommChannel.sms,
                purpose=CommPurpose.milestone,
                subject_type="farmer",
                subject_id=1,
                recipient="+254700000001",
                template_key="milestone.harvest_recorded",
                template_version=1,
                locale="en",
                payload={},
                source_audit_id=None,
                status=status,
            )
        )
        s.commit()


def test_an_undelivered_message_after_an_hour_turns_red(sessions, clock):
    _message(sessions, CommStatus.queued)
    clock(minutes=59)
    assert "comms_backlog" not in _failing(sessions)
    clock(minutes=2)
    assert "comms_backlog" in _failing(sessions)


def test_a_message_that_failed_for_good_is_not_backlog(sessions, clock):
    _message(sessions, CommStatus.failed)
    clock(hours=3)
    assert "comms_backlog" not in _failing(sessions)


def test_a_job_that_raises_is_recorded_redacted_and_alarmed_once(sessions, sentry_events):
    def boom() -> Outcome:
        raise RuntimeError("could not send to +254712345678")

    track(sessions, COMMS, boom)
    track(sessions, COMMS, boom)

    row = _row(sessions, COMMS)
    assert row.consecutive_failures == 2
    assert row.last_ok is False
    assert "RuntimeError" in row.last_error
    assert "712345678" not in row.last_error
    alarms = [e for e in sentry_events if e.get("tags", {}).get("job") == COMMS]
    assert len(alarms) == 1
    assert "712345678" not in json.dumps(alarms)


def test_anchor_health_never_shows_a_jobs_exception_text(sessions, client):
    """`/v2/audit/anchor-health` is public: it shows the calendar's refusal,
    as it always has, but never an arbitrary exception from the job."""

    def boom() -> Outcome:
        raise RuntimeError("connection to db-internal.example:25060 refused")

    track(sessions, ANCHOR_STAMP, boom)
    body = client.get("/v2/audit/anchor-health").json()
    assert "db-internal" not in json.dumps(body)
    assert "db-internal" in _row(sessions, ANCHOR_STAMP).last_error


def test_a_failed_status_write_never_stops_the_job(migrated_engine):
    def broken_factory():
        raise RuntimeError("database unavailable")

    ran = []
    outcome = track(broken_factory, COMMS, lambda: ran.append(1) or Outcome(ok=True))
    assert ran == [1]
    assert outcome.ok is True


# --- The processes ------------------------------------------------------------------


def test_the_api_process_no_longer_schedules(monkeypatch):
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.schedulers.blocking import BlockingScheduler

    from app.main import app

    def refuse(*_args, **_kwargs):
        raise AssertionError("the API process started a scheduler")

    monkeypatch.setattr(BackgroundScheduler, "start", refuse)
    monkeypatch.setattr(BlockingScheduler, "start", refuse)
    with TestClient(app) as client:  # runs the lifespan, if there is one
        assert client.get("/health").status_code == 200


def test_the_worker_schedules_every_job(monkeypatch):
    from app.worker import build_scheduler

    ids = {job.id for job in build_scheduler(sessionmaker()).get_jobs()}
    assert ids == {
        "worker",
        "audit_integrity",
        "anchor_stamp",
        "anchor_upgrade",
        "comms_milestones",
        "rate_limit_prune",
    }

    monkeypatch.setattr(settings, "anchor_enabled", False)
    monkeypatch.setattr(settings, "comms_enabled", False)
    ids = {job.id for job in build_scheduler(sessionmaker()).get_jobs()}
    assert ids == {"worker", "audit_integrity", "rate_limit_prune"}


def test_the_prune_removes_only_expired_rate_limit_events(sessions, migrated_engine):
    now = dt.datetime.now(dt.UTC).replace(tzinfo=None)
    key = b"k" * 32
    with sessions() as s:
        s.add_all(
            [
                RateLimitEvent(scope="upload.ip", key_hash=key, at=now - dt.timedelta(days=2)),
                RateLimitEvent(scope="upload.ip", key_hash=key, at=now),
            ]
        )
        s.commit()
    assert rate_limit.prune(migrated_engine) == 1
    with sessions() as s:
        assert s.query(RateLimitEvent).count() == 1
