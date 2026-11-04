"""The alarms behind `GET /health/ops` (P5-D, 14 §1 Acceptance, D8).

Each check names one way the system can be failing without anyone noticing.
The endpoint answers with the names only; the numbers stay on the admin
endpoints and in `worker_status`.

- `worker_down`: no heartbeat, or one older than `ops_heartbeat_intervals`
  beats. A worker that never ran reads red.
- `chain_broken`: the last integrity check found a divergence.
- `anchor_failing`: the stamp job has failed `ops_anchor_failure_runs` runs
  in a row (the calendars unreachable).
- `anchor_lag`: the oldest unanchored row, or the gap the stamp job is
  waiting on, is older than `ops_anchor_lag_intervals` anchor intervals. The
  job itself waits up to two intervals on a gap before skipping a burned id,
  and a run can take one more, so four never trips on a single burned id.
- `upgrade_stale`: an anchor still unconfirmed after `ops_pending_upgrade_hours`.
- `comms_backlog`: a milestone message still queued or being sent after
  `ops_comms_backlog_minutes`. A message that failed for good is not backlog:
  it will never drain, and counting it would hold the alarm red forever.

The anchor checks are skipped when `ANCHOR_ENABLED` is off, and the comms
check when `COMMS_ENABLED` is off: a worker told not to do a job cannot be
behind on it. A job row that does not exist yet, beside a fresh heartbeat, is
a worker that has just started, not a failure.
"""

import datetime as dt

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.enums import CommPurpose, CommStatus
from app.models import Communication
from app.services import anchoring, worker_status
from app.services.worker_status import ANCHOR_STAMP, INTEGRITY, WORKER


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC).replace(tzinfo=None)


def _older(at: dt.datetime | None, limit: dt.timedelta, now: dt.datetime) -> bool:
    return at is not None and now - at > limit


def failing(db: Session) -> list[str]:
    now = _now()
    rows = worker_status.rows(db)
    out: list[str] = []

    beat = rows.get(WORKER)
    beat_limit = dt.timedelta(
        seconds=settings.ops_heartbeat_intervals * settings.worker_heartbeat_seconds
    )
    if beat is None or beat.heartbeat_at is None or _older(beat.heartbeat_at, beat_limit, now):
        out.append("worker_down")

    integrity = rows.get(INTEGRITY)
    if integrity is not None and (integrity.detail or {}).get("chain_ok") is False:
        out.append("chain_broken")

    if settings.anchor_enabled:
        stamp = rows.get(ANCHOR_STAMP)
        if stamp is not None and stamp.consecutive_failures >= settings.ops_anchor_failure_runs:
            out.append("anchor_failing")

        lag = dt.timedelta(
            seconds=settings.ops_anchor_lag_intervals * settings.anchor_interval_seconds
        )
        backlog = anchoring.backlog(db)
        waiting_since = (stamp.detail or {}).get("waiting_since") if stamp else None
        if _older(backlog.oldest_unanchored_at, lag, now) or (
            waiting_since and _older(dt.datetime.fromisoformat(waiting_since), lag, now)
        ):
            out.append("anchor_lag")

        upgrade_limit = dt.timedelta(hours=settings.ops_pending_upgrade_hours)
        if _older(backlog.oldest_pending_anchor_at, upgrade_limit, now):
            out.append("upgrade_stale")

    if settings.comms_enabled:
        oldest_message = db.execute(
            select(func.min(Communication.created_at)).where(
                # Milestones only: the worker drains nothing else. A verification
                # code is sent inline by its route, and a row of one left in
                # `sending` by a crash would hold this red forever.
                Communication.purpose == CommPurpose.milestone,
                Communication.status.in_([CommStatus.queued, CommStatus.sending]),
            )
        ).scalar_one_or_none()
        if _older(oldest_message, dt.timedelta(minutes=settings.ops_comms_backlog_minutes), now):
            out.append("comms_backlog")

    return out
