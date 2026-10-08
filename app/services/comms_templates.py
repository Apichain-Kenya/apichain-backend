"""Message templates, frozen in code and versioned (P3b-F, 11 D9).

The same discipline as `codex_scoring.RULES`: a template's wording is never
edited in place. A wording change is a new `version`, and every
`communications` row records `template_key`, `template_version` and `locale`,
so the exact text a farmer received is reproducible from the log without the
log ever storing a message body (or the verification code inside one).

`04` §5.7 puts templates in a `comms_templates` table. Nothing in 3b edits copy
at runtime, a migration-seeded table would be wiped by the test truncate, and
frozen code cannot drift under a logged message. The table returns with a copy
editor; that is a one-revision addition. English only for v2 (03 §10, §13),
with `locale` in the key so Swahili is a translation task, not a schema change.

**SMS length is a cost, not a style point.** A Kenyan SMS over 160 GSM-7
characters is billed as two (03 §4). Every SMS template is tested at its
longest: each variable declares a maximum length, and `render` truncates to it
(a client-supplied `batch_code` is otherwise unbounded).
"""

from collections.abc import Mapping
from dataclasses import dataclass

from app.enums import CommChannel

DEFAULT_LOCALE = "en"
SMS_MAX_CHARS = 160


@dataclass(frozen=True)
class Template:
    key: str
    channel: CommChannel
    locale: str
    version: int
    body: str
    # Variable name -> maximum rendered length.
    variables: Mapping[str, int]
    subject: str | None = None  # email only


def _t(
    key: str,
    channel: CommChannel,
    version: int,
    body: str,
    variables: Mapping[str, int],
    subject: str | None = None,
) -> Template:
    return Template(key, channel, DEFAULT_LOCALE, version, body, variables, subject)


_CODE = {"code": 6, "minutes": 2}
_BATCH = {"batch_code": 32}

_ALL: tuple[Template, ...] = (
    _t(
        "verification_code",
        CommChannel.sms,
        1,
        "ApiChain: your code is {code}. It expires in {minutes} minutes. Do not share it.",
        _CODE,
    ),
    _t(
        "verification_code",
        CommChannel.email,
        1,
        "Your ApiChain verification code is {code}.\n\n"
        "It expires in {minutes} minutes. If you did not expect this, ignore this email "
        "and tell your field officer.",
        _CODE,
        subject="Your ApiChain verification code",
    ),
    _t(
        "milestone.harvest_recorded",
        CommChannel.sms,
        1,
        "ApiChain: your harvest for batch {batch_code} has been recorded.",
        _BATCH,
    ),
    _t(
        "milestone.harvest_recorded",
        CommChannel.email,
        1,
        "Your harvest for batch {batch_code} has been recorded on ApiChain.",
        _BATCH,
        subject="Harvest recorded: batch {batch_code}",
    ),
    # The verdict is deliberately not in the message: it says a result exists
    # and where to read it, not what it was.
    _t(
        "milestone.lab_verified",
        CommChannel.sms,
        1,
        "ApiChain: lab results for batch {batch_code} are ready. Open ApiChain to view them.",
        _BATCH,
    ),
    _t(
        "milestone.lab_verified",
        CommChannel.email,
        1,
        "Lab results for batch {batch_code} have been recorded. Open ApiChain to view them.",
        _BATCH,
        subject="Lab results ready: batch {batch_code}",
    ),
    _t(
        "milestone.distributed",
        CommChannel.sms,
        1,
        "ApiChain: batch {batch_code} has been distributed. Thank you.",
        _BATCH,
    ),
    _t(
        "milestone.distributed",
        CommChannel.email,
        1,
        "Batch {batch_code} has been distributed. Thank you for supplying it.",
        _BATCH,
        subject="Distributed: batch {batch_code}",
    ),
)


def _index(templates: tuple[Template, ...]) -> dict[tuple[str, CommChannel, str], Template]:
    index: dict[tuple[str, CommChannel, str], Template] = {}
    for template in templates:
        slot = (template.key, template.channel, template.locale)
        if slot in index:
            raise ValueError(f"duplicate template {slot}")
        if (template.channel is CommChannel.email) != (template.subject is not None):
            raise ValueError(f"{slot}: email templates need a subject, SMS ones must not")
        index[slot] = template
    return index


TEMPLATES: Mapping[tuple[str, CommChannel, str], Template] = _index(_ALL)


def get(key: str, channel: CommChannel, locale: str = DEFAULT_LOCALE) -> Template:
    try:
        return TEMPLATES[(key, channel, locale)]
    except KeyError as exc:
        raise KeyError(f"no template {key!r} for {channel}/{locale}") from exc


def render(template: Template, variables: Mapping[str, str]) -> tuple[str | None, str]:
    """(subject, body). Exactly the declared variables, each bounded."""
    if set(variables) != set(template.variables):
        raise KeyError(
            f"{template.key}: expected {sorted(template.variables)}, got {sorted(variables)}"
        )
    bounded = {
        name: _truncate(str(value), template.variables[name]) for name, value in variables.items()
    }
    subject = template.subject.format(**bounded) if template.subject is not None else None
    return subject, template.body.format(**bounded)


def _truncate(value: str, limit: int) -> str:
    # "..." rather than an ellipsis character: one non-GSM-7 character turns
    # the whole SMS into UCS-2, whose single-part limit is 70, not 160.
    return value if len(value) <= limit else value[: limit - 3] + "..."
