"""The worker process: `python -m app.worker` (P5-D, 14 D9).

Every scheduled job runs here and nowhere else. Before Phase 5 the API
process started a scheduler in its lifespan, so two API instances ran every
job twice and each reported its own view of their health. The jobs are safe
to run twice (advisory locks, `SKIP LOCKED`), but the health was not; it now
lives in `worker_status`, which every API instance reads.

One worker instance is the intended deployment. A second one is harmless for
the jobs and for the status rows (each write locks its row).

Jobs: the heartbeat, the chain integrity check, the anchor stamp and upgrade
(when `ANCHOR_ENABLED`), the milestone messages (when `COMMS_ENABLED`), and
the hourly prune of expired rate-limit events.
"""

import datetime as dt
import logging
import signal
from collections.abc import Callable
from types import FrameType

from apscheduler.schedulers.blocking import BlockingScheduler
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.observability import configure_logging, init_sentry
from app.services import jobs, worker_status
from app.services.worker_status import Outcome

logger = logging.getLogger("apichain.worker")


def _tracked(
    session_factory: sessionmaker, name: str, job: Callable[[sessionmaker], Outcome]
) -> Callable[[], None]:
    def run() -> None:
        worker_status.track(session_factory, name, lambda: job(session_factory))

    return run


def build_scheduler(session_factory: sessionmaker) -> BlockingScheduler:
    scheduler = BlockingScheduler(
        job_defaults={"coalesce": True, "max_instances": 1}, timezone=dt.UTC
    )
    now = dt.datetime.now(dt.UTC)

    def every(seconds: int, name: str, run: Callable[[], None], *, at_start: bool = False) -> None:
        # The heartbeat and the chain check run at once, so a fresh worker
        # reads green within seconds rather than after its first interval.
        # (An explicit `next_run_time=None` would add the job paused.)
        start = {"next_run_time": now} if at_start else {}
        scheduler.add_job(run, "interval", seconds=seconds, id=name, **start)

    every(
        settings.worker_heartbeat_seconds,
        worker_status.WORKER,
        lambda: worker_status.heartbeat(session_factory),
        at_start=True,
    )
    every(
        settings.integrity_check_interval_seconds,
        worker_status.INTEGRITY,
        _tracked(session_factory, worker_status.INTEGRITY, jobs.integrity_check),
        at_start=True,
    )
    if settings.anchor_enabled:
        every(
            settings.anchor_interval_seconds,
            worker_status.ANCHOR_STAMP,
            _tracked(session_factory, worker_status.ANCHOR_STAMP, jobs.anchor_stamp),
        )
        # Slower: Bitcoin confirmation takes hours.
        every(
            settings.anchor_upgrade_interval_seconds,
            worker_status.ANCHOR_UPGRADE,
            _tracked(session_factory, worker_status.ANCHOR_UPGRADE, jobs.anchor_upgrade),
        )
    if settings.comms_enabled:
        every(
            settings.comms_interval_seconds,
            worker_status.COMMS,
            _tracked(session_factory, worker_status.COMMS, jobs.comms),
        )
    every(
        settings.rate_limit_prune_interval_seconds,
        worker_status.RATE_LIMIT_PRUNE,
        _tracked(session_factory, worker_status.RATE_LIMIT_PRUNE, jobs.prune_rate_limits),
    )
    return scheduler


def main() -> None:
    configure_logging()
    init_sentry()

    from app.database import SessionLocal

    scheduler = build_scheduler(SessionLocal)

    def stop(signum: int, _frame: FrameType | None) -> None:
        logger.info("worker stopping on signal %s", signum)
        scheduler.shutdown(wait=False)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    logger.info(
        "worker started: %s",
        ", ".join(job.id for job in scheduler.get_jobs()),
    )
    scheduler.start()


if __name__ == "__main__":
    main()
