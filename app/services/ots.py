"""The OpenTimestamps boundary (P2-D, 04 §5.9 boundary 4).

**The only module in the anchoring path that touches the network.** Everything
else — the Merkle layer, the range selector, proof verification — is pure. Tests
inject fake calendars here and reach nothing; CI never contacts a calendar
server or Bitcoin.

Publishing a root needs no wallet, no funded account, and no signing key at all
(05 §5.4): submitting a digest to a calendar is an unauthenticated POST. That is
the concrete security win the OpenTimestamps decision bought over the Polygon
fallback — the system has no hot key.

The lifecycle has two steps, hours apart, and that gap is the product's
"anchor pending" state rather than a defect:

1. `stamp(root)` submits the root and returns a proof carrying a *pending*
   attestation — a promise by a calendar to include it in a Bitcoin block.
2. `upgrade(proof)` asks the calendar for the completed path once Bitcoin has
   confirmed it, returning a proof with a `BitcoinBlockHeaderAttestation`.

Retry policy (04 §5.9 requires one per boundary): there is none in here, by
design. A failed stamp raises and the caller writes nothing; the next scheduled
tick recomputes the identical range and tries again. The scheduler *is* the
retry loop, so a calendar outage cannot produce a retry storm (09 D10).
"""

import logging
from collections.abc import Iterable, Sequence
from typing import Protocol

from opentimestamps.core.notary import (
    BitcoinBlockHeaderAttestation,
    PendingAttestation,
)
from opentimestamps.core.op import OpSHA256
from opentimestamps.core.serialize import (
    BytesDeserializationContext,
    BytesSerializationContext,
)
from opentimestamps.core.timestamp import DetachedTimestampFile, Timestamp

from app.config import settings

logger = logging.getLogger("apichain.ots")


class CalendarUnavailable(RuntimeError):
    """No configured calendar accepted the root. Nothing may be recorded."""


class InvalidProof(ValueError):
    """A stored proof blob could not be parsed as a detached .ots file."""


class Calendar(Protocol):
    """The two-method surface of `opentimestamps.calendar.RemoteCalendar`."""

    url: str

    def submit(self, digest: bytes, timeout: float | None = ...) -> Timestamp: ...

    def get_timestamp(self, commitment: bytes, timeout: float | None = ...) -> Timestamp: ...


def _configured_calendars() -> list[Calendar]:
    # Imported lazily so the module can be exercised without the real class
    # ever being constructed.
    from opentimestamps.calendar import RemoteCalendar

    return [RemoteCalendar(url, user_agent="apichain") for url in settings.calendar_urls]


def _parse(proof: bytes) -> DetachedTimestampFile:
    try:
        return DetachedTimestampFile.deserialize(BytesDeserializationContext(proof))
    except Exception as exc:  # the library raises a wide range of low-level errors
        raise InvalidProof("not a valid detached .ots proof") from exc


def _serialize(detached: DetachedTimestampFile) -> bytes:
    ctx = BytesSerializationContext()
    detached.serialize(ctx)
    return ctx.getbytes()


def _sub_timestamps(timestamp: Timestamp) -> dict[bytes, Timestamp]:
    """Every node in the proof tree, keyed by the message it commits to."""
    found: dict[bytes, Timestamp] = {}
    stack = [timestamp]
    while stack:
        node = stack.pop()
        found.setdefault(node.msg, node)
        stack.extend(node.ops.values())
    return found


def stamp(root: bytes, *, calendars: Sequence[Calendar] | None = None) -> bytes:
    """Submit `root` to the calendars and return a serialized pending proof.

    Submitting to several calendars is redundancy, not consensus: one success
    is enough, and a calendar that is down is skipped. If none accepts the
    root, this raises — an anchor row must never claim an anchor that no
    calendar acknowledged (09 D9).
    """
    calendars = calendars if calendars is not None else _configured_calendars()
    timestamp = Timestamp(root)
    accepted = 0

    for calendar in calendars:
        try:
            timestamp.merge(calendar.submit(root, timeout=settings.ots_timeout_seconds))
            accepted += 1
        except Exception as exc:
            logger.warning("calendar %s rejected the root: %s", calendar.url, exc)

    if not accepted:
        raise CalendarUnavailable(f"no calendar accepted the root {root.hex()}")

    # OpSHA256 declares "the anchored digest is a sha256 hash", which is true
    # because the Merkle layer is sha256 (09 D2).
    return _serialize(DetachedTimestampFile(OpSHA256(), timestamp))


def upgrade(proof: bytes, *, calendars: Sequence[Calendar] | None = None) -> bytes | None:
    """Return a Bitcoin-attested proof, or None if it is not confirmed yet.

    None is the expected answer for hours after stamping and means "leave the
    anchor pending" — it is not an error and is not logged as one.
    """
    calendars = calendars if calendars is not None else _configured_calendars()
    detached = _parse(proof)
    if _bitcoin_heights(detached.timestamp):
        return None  # already confirmed; never re-fetch (09 §9)

    # Only ever contact calendars we configured. A proof carries calendar URLs
    # inside it, and following whatever URL a stored blob names would let a
    # tampered proof redirect us to an attacker's server.
    trusted = {calendar.url.rstrip("/"): calendar for calendar in calendars}
    nodes = _sub_timestamps(detached.timestamp)
    upgraded = False

    for message, attestation in list(detached.timestamp.all_attestations()):
        if not isinstance(attestation, PendingAttestation):
            continue
        calendar = trusted.get(attestation.uri.rstrip("/"))
        if calendar is None:
            logger.debug("ignoring pending attestation from unconfigured %s", attestation.uri)
            continue
        try:
            completed = calendar.get_timestamp(message, timeout=settings.ots_timeout_seconds)
        except KeyError:
            continue  # Bitcoin has not confirmed it yet: the normal case
        except Exception as exc:
            logger.warning("calendar %s could not be reached: %s", calendar.url, exc)
            continue
        nodes[message].merge(completed)
        upgraded = True

    if not upgraded or not _bitcoin_heights(detached.timestamp):
        return None
    return _serialize(detached)


# --- pure readers: no network, safe to call on any stored proof ------------


def message(proof: bytes) -> bytes:
    """The digest this proof commits to — must equal the anchor's Merkle root."""
    return _parse(proof).timestamp.msg


def file_hash_op_name(proof: bytes) -> str:
    return str(_parse(proof).file_hash_op)


def _attestations(timestamp: Timestamp) -> Iterable:
    return (attestation for _, attestation in timestamp.all_attestations())


def _bitcoin_heights(timestamp: Timestamp) -> list[int]:
    return sorted(
        a.height for a in _attestations(timestamp) if isinstance(a, BitcoinBlockHeaderAttestation)
    )


def bitcoin_block_heights(proof: bytes) -> list[int]:
    """Bitcoin block heights this proof commits to (empty while pending)."""
    return _bitcoin_heights(_parse(proof).timestamp)


def pending_calendar_urls(proof: bytes) -> list[str]:
    return sorted(
        a.uri for a in _attestations(_parse(proof).timestamp) if isinstance(a, PendingAttestation)
    )


def is_confirmed(proof: bytes) -> bool:
    """True once Bitcoin has confirmed the root — the anchor's `verified_at`."""
    return bool(bitcoin_block_heights(proof))
