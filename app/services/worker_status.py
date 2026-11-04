"""What each background job did, kept in `worker_status` (P5-D, 14 D9).

`track()` wraps one run of one job. It writes the start, runs the job, and
writes the finish, each in a short committed session of its own: a status
kept in the job's transaction would vanish whenever the job rolled back (the
P5-B lesson). A failed status write is logged and never stops the worker.

**A run's verdict is the job's own:** `Outcome.ok` is True (it worked), False
(it failed) or None (nothing to judge: no work, the lock held elsewhere,
waiting on a gap). Only False adds to `consecutive_failures`; True resets it;
None leaves it alone. A job that raises is a False with the exception as its
error.

**One Sentry event per transition into red.** Each job has a red rule
(`_red`). The row's `alarmed` flag remembers the last state, so the event
fires when a job turns red and not again until it has recovered, a
restarted worker included. The event is sent after the status commits.
"""

import datetime as dt
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import sentry_sdk
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings
from app.models import WorkerStatus
from app.services.redaction import scrub_text

logger = logging.getLogger("apichain.worker")

# Job names: the `worker_status.job` keys.
WORKER = "worker"  # the heartbeat row
INTEGRITY = "audit_integrity"
ANCHOR_STAMP = "anchor_stamp"
ANCHOR_UPGRADE = "anchor_upgrade"
COMMS = "comms_milestones"
RATE_LIMIT_PRUNE = "rate_limit_prune"

_ERROR_MAX = 500


@dataclass(frozen=True)
class Outcome:
    ok: bool | None
    error: str | None = None
    detail: dict[str, Any] | None = None


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC).replace(tzinfo=None)


def iso(value: dt.datetime | None) -> str | None:
    """A naive-UTC datetime as JSON-safe text for `detail`."""
    return value.replace(tzinfo=None).isoformat() if value is not None else None


def _red(job: str, row: WorkerStatus) -> bool:
    if job == ANCHOR_STAMP:
        # Calendars blip; three failed runs in a row is an outage (14 §1).
        return row.consecutive_failures >= settings.ops_anchor_failure_runs
    if job == INTEGRITY and (row.detail or {}).get("chain_ok") is False:
        return True
    return row.consecutive_failures >= 1


def _write[T](
    session_factory: sessionmaker, job: str, change: Callable[[WorkerStatus], T]
) -> T | None:
    try:
        with session_factory() as db:
            row = _locked_row(db, job)
            result = change(row)
            db.commit()
            return result
    except Exception:
        logger.exception("could not record the status of job %s", job)
        return None


def _locked_row(db: Session, job: str) -> WorkerStatus:
    row = db.get(WorkerStatus, job, with_for_update=True)
    if row is None:
        row = WorkerStatus(job=job, consecutive_failures=0, alarmed=False)
        db.add(row)
    return row


def _merge_detail(old: dict[str, Any] | None, new: dict[str, Any], now: dt.datetime) -> dict:
    """The anchor job's gap: when the id it waits on first appeared, so the
    alarm can tell a fresh wait from a stuck one."""
    detail = dict(new)
    if "waiting_on_audit_id" in detail:
        waiting = detail["waiting_on_audit_id"]
        before = old or {}
        if waiting is None:
            detail["waiting_since"] = None
        elif before.get("waiting_on_audit_id") == waiting and before.get("waiting_since"):
            detail["waiting_since"] = before["waiting_since"]
        else:
            detail["waiting_since"] = iso(now)
    return detail


def _finish(job: str, outcome: Outcome) -> Callable[[WorkerStatus], str | None]:
    def change(row: WorkerStatus) -> str | None:
        now = _now()
        row.last_finished_at = now
        if outcome.ok is True:
            row.last_ok = True
            row.last_ok_at = now
            row.consecutive_failures = 0
            row.last_error = None
        elif outcome.ok is False:
            row.last_ok = False
            row.consecutive_failures = (row.consecutive_failures or 0) + 1
            row.last_error = scrub_text(outcome.error or "failed")[:_ERROR_MAX]
        if outcome.detail is not None:
            row.detail = _merge_detail(row.detail, outcome.detail, now)
        red = _red(job, row)
        alert = red and not row.alarmed
        row.alarmed = red
        return (row.last_error or "failing") if alert else None

    return change


def _alert(job: str, error: str) -> None:
    logger.error("job %s turned red: %s", job, error)
    with sentry_sdk.new_scope() as scope:
        scope.set_tag("job", job)
        scope.capture_message(f"worker job {job} is failing: {error}", level="error")


def track(session_factory: sessionmaker, job: str, run: Callable[[], Outcome]) -> Outcome:
    """Run one tick of `job` and record what it did."""

    def started(row: WorkerStatus) -> None:
        row.last_started_at = _now()

    _write(session_factory, job, started)
    try:
        outcome = run()
    except Exception as exc:
        logger.exception("job %s raised", job)
        outcome = Outcome(ok=False, error=f"{type(exc).__name__}: {exc}")
    alert = _write(session_factory, job, _finish(job, outcome))
    if alert is not None:
        _alert(job, alert)
    return outcome


def heartbeat(session_factory: sessionmaker) -> None:
    """The worker is alive. `/health/ops` reads this row's age."""

    def beat(row: WorkerStatus) -> None:
        row.heartbeat_at = _now()

    _write(session_factory, WORKER, beat)


def rows(db: Session) -> dict[str, WorkerStatus]:
    return {row.job: row for row in db.execute(select(WorkerStatus)).scalars()}
