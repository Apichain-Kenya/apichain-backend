"""Logging and error tracking for every process: the API now, the worker
from P5-D (14 P5-C, D10, D11).

**Logs.** The code keeps plain `logging.getLogger(...)` calls. One handler on
the root logger renders every record through structlog: the request id bound
by `middleware.request_id`, the level, the logger name, a UTC timestamp and
any `extra=` fields, then the redaction (`services/redaction.py`), then JSON
in production or a console line in development. Redaction runs after the
traceback is formatted, so a phone inside an exception message is masked too.
Uvicorn's own loggers are routed to the same handler; its access log is
switched off, because `RequestId` writes one without the client's address or
the query string.

**Sentry** starts only when `SENTRY_DSN` is set. Nothing personal is sent on
purpose (`DATA_COLLECTION`: no user, cookies, bodies, query strings or frame
locals, and only four request headers), and
every event and breadcrumb goes through the same redaction before it leaves.
"""

import logging
import sys
from typing import TYPE_CHECKING, Any, TextIO

import sentry_sdk
import structlog
from structlog.typing import EventDict, WrappedLogger

from app.config import settings
from app.services.redaction import scrub

if TYPE_CHECKING:
    from sentry_sdk._types import DataCollectionUserOptions

_HANDLER_MARK = "_apichain_handler"


class _Stderr(logging.StreamHandler):  # type: ignore[type-arg]
    """Writes to whatever `sys.stderr` is at the time of the write, so a test
    runner that swaps the stream never leaves the handler holding a closed one."""

    def __init__(self) -> None:
        super().__init__()

    @property  # type: ignore[override]
    def stream(self) -> TextIO:
        return sys.stderr

    @stream.setter
    def stream(self, _value: TextIO) -> None:
        pass


def _redact(_logger: WrappedLogger, _method: str, event_dict: EventDict) -> EventDict:
    return scrub(event_dict)  # type: ignore[no-any-return]


def _drop_color(_logger: WrappedLogger, _method: str, event_dict: EventDict) -> EventDict:
    # Uvicorn passes an ANSI-coloured copy of each message as an extra.
    event_dict.pop("color_message", None)
    return event_dict


def _json() -> bool:
    if settings.log_format is not None:
        return settings.log_format == "json"
    return settings.app_env == "production"


def configure_logging(stream: TextIO | None = None) -> None:
    """Install the one root handler (replacing an earlier one, so calling this
    twice is harmless). `stream` is for tests that read the rendered output."""
    pre_chain: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.stdlib.ExtraAdder(),
        _drop_color,
    ]
    renderer: Any = (
        structlog.processors.JSONRenderer()
        if _json()
        else structlog.dev.ConsoleRenderer(colors=False)
    )
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=pre_chain,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.format_exc_info,
            _redact,
            renderer,
        ],
    )
    handler: logging.Handler = logging.StreamHandler(stream) if stream is not None else _Stderr()
    handler.setFormatter(formatter)
    setattr(handler, _HANDLER_MARK, True)

    root = logging.getLogger()
    for old in [h for h in root.handlers if getattr(h, _HANDLER_MARK, False)]:
        root.removeHandler(old)
    root.addHandler(handler)
    root.setLevel(settings.log_level.upper())

    for name in ("uvicorn", "uvicorn.error"):
        uv = logging.getLogger(name)
        uv.handlers.clear()
        uv.propagate = True
    logging.getLogger("uvicorn.access").disabled = True
    # One INFO line per job run per tick is noise; failures still show.
    logging.getLogger("apscheduler.executors.default").setLevel(logging.WARNING)

    # Code that uses structlog directly renders through the same handler.
    structlog.configure(
        processors=[*pre_chain[:4], structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )


def _before_send(event: Any, _hint: Any) -> Any:
    return scrub(event)


def _before_breadcrumb(crumb: Any, _hint: Any) -> Any:
    return scrub(crumb)


# What the SDK may collect, every field stated: an omitted one defaults to
# "collect". `send_default_pii` is deprecated in sentry-sdk 2.7x in favour of
# this, and stating it all also turns off what that switch left on (request
# headers, query strings, SQL parameters). Setting this drops the SDK's own
# event scrubber; `_before_send` takes its place.
DATA_COLLECTION: "DataCollectionUserOptions" = {
    "user_info": False,
    "cookies": {"mode": "off"},
    "http_headers": {
        "request": {
            "mode": "allowlist",
            "terms": ["content-type", "content-length", "user-agent", "x-request-id"],
        }
    },
    "http_bodies": [],
    "url_query_params": {"mode": "off"},
    "graphql": {"document": False, "variables": False},
    "gen_ai": {"inputs": False, "outputs": False},
    "database_query_data": False,
    "queues": False,
    "stack_frame_variables": False,
}


def init_sentry(**overrides: Any) -> bool:
    """Start Sentry if a DSN is configured; answer whether it started.
    `overrides` lets a test supply a capturing transport."""
    dsn = overrides.pop("dsn", settings.sentry_dsn)
    if not dsn:
        return False
    sentry_sdk.init(
        dsn=dsn,
        environment=settings.app_env,
        release=settings.app_release,
        data_collection=DATA_COLLECTION,
        max_request_body_size="never",
        traces_sample_rate=0.0,
        before_send=_before_send,
        before_breadcrumb=_before_breadcrumb,
        **overrides,
    )
    return True
