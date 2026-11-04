"""The worker's jobs, each reduced to an `Outcome` for `worker_status`
(P5-D). The job code itself is unchanged; this only says what a run meant.
"""

from collections.abc import Sequence

from sqlalchemy.orm import sessionmaker

from app.services import anchoring, comms_worker, integrity, ots, rate_limit
from app.services.worker_status import Outcome, iso


def integrity_check(session_factory: sessionmaker) -> Outcome:
    status = integrity.run_check(session_factory)
    return Outcome(
        ok=status.chain_ok,
        error=None
        if status.chain_ok
        else f"audit chain diverges at id {status.first_divergent_id}",
        detail={
            "chain_ok": status.chain_ok,
            "checked_at": iso(status.checked_at),
            "rows_checked": status.rows_checked,
            "first_divergent_id": status.first_divergent_id,
        },
    )


def anchor_stamp(
    session_factory: sessionmaker, *, calendars: Sequence[ots.Calendar] | None = None
) -> Outcome:
    anchoring.run_stamp(session_factory, calendars=calendars)
    status = anchoring.current_status()
    # Counted from what this tick did, never from `anchor_ok`, which a tick
    # that found nothing to do (or the lock held elsewhere) leaves as it was.
    ok = {"anchored": True, "failed": False}.get(status.last_outcome)
    return Outcome(
        ok=ok,
        error=status.last_error if ok is False else None,
        detail={
            "anchor_ok": status.anchor_ok,
            "last_run_at": iso(status.last_run_at),
            "last_anchor_id": status.last_anchor_id,
            "waiting_on_audit_id": status.waiting_on_audit_id,
            # The calendar's refusal, as `/anchor-health` has always shown it.
            "last_error": status.last_error,
        },
    )


def anchor_upgrade(
    session_factory: sessionmaker, *, calendars: Sequence[ots.Calendar] | None = None
) -> Outcome:
    confirmed = anchoring.run_upgrade(session_factory, calendars=calendars)
    return Outcome(ok=True, detail={"confirmed": confirmed})


def comms(session_factory: sessionmaker) -> Outcome:
    tick = comms_worker.run(session_factory)
    return Outcome(
        ok=True,
        detail={
            "enqueued": tick.enqueued,
            "sent": tick.sent,
            "failed": tick.failed,
            "skipped": tick.skipped,
        },
    )


def prune_rate_limits(session_factory: sessionmaker) -> Outcome:
    with session_factory() as db:
        bind = db.get_bind()
    return Outcome(ok=True, detail={"deleted": rate_limit.prune(bind)})
