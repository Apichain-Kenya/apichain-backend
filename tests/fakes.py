"""Fakes for the external boundaries (04 §5.9: every boundary gets a fake).

CI must never reach a calendar server or Bitcoin. `FakeCalendar` stands in for
`opentimestamps.calendar.RemoteCalendar` and can be told to behave like a
calendar that has accepted a digest but has no Bitcoin attestation yet (the
normal case for hours), one that has been confirmed, or one that is simply
down.
"""

import time

from opentimestamps.core.notary import (
    BitcoinBlockHeaderAttestation,
    PendingAttestation,
)
from opentimestamps.core.op import OpAppend, OpSHA256
from opentimestamps.core.timestamp import Timestamp


class CalendarDown(Exception):
    """Stands in for any transport failure from a calendar server."""


class FakeCalendar:
    """A calendar that never touches the network.

    `submit` mirrors the real contract: it returns a Timestamp committing to the
    digest, carrying a pending attestation. `get_timestamp` raises KeyError
    until `confirm_at_height` is set — which is exactly how the real calendar
    reports "Bitcoin has not confirmed this yet".
    """

    def __init__(
        self,
        url: str = "https://calendar.test",
        *,
        down: bool = False,
        confirm_at_height: int | None = None,
        submit_delay: float = 0.0,
    ) -> None:
        self.url = url
        self.down = down
        self.confirm_at_height = confirm_at_height
        # A real calendar submission is a network round-trip. That latency IS
        # the race window between two replicas, so a concurrency test needs to
        # be able to reproduce it; with a zero-latency fake the window is too
        # narrow to collide and the test passes whether the lock exists or not.
        self.submit_delay = submit_delay
        self.submitted: list[bytes] = []
        self.get_timestamp_calls: list[bytes] = []

    def submit(self, digest: bytes, timeout: float | None = None) -> Timestamp:
        if self.down:
            raise CalendarDown(f"{self.url} is unreachable")
        if self.submit_delay:
            time.sleep(self.submit_delay)
        self.submitted.append(digest)
        timestamp = Timestamp(digest)
        # A real calendar aggregates the digest with other submissions before
        # committing; one append op is a faithful stand-in for that shape.
        sub = timestamp.ops.add(OpAppend(self.url.encode()[:8].ljust(8, b"\x00")))
        sub.attestations.add(PendingAttestation(self.url))
        return timestamp

    def get_timestamp(self, commitment: bytes, timeout: float | None = None) -> Timestamp:
        if self.down:
            raise CalendarDown(f"{self.url} is unreachable")
        self.get_timestamp_calls.append(commitment)
        if self.confirm_at_height is None:
            # The real client raises KeyError while the commitment is still
            # waiting for a Bitcoin block. Not an error condition.
            raise KeyError(commitment)
        timestamp = Timestamp(commitment)
        sub = timestamp.ops.add(OpSHA256())
        sub.attestations.add(BitcoinBlockHeaderAttestation(self.confirm_at_height))
        return timestamp
