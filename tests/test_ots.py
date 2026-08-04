"""The OpenTimestamps boundary (P2-D, 09 §7).

This is the only module in the phase that touches the network, so it is the
only one that needs a fake. Every test here injects calendars explicitly; none
reaches a calendar server or Bitcoin, and none ever will.
"""

import hashlib

import pytest

from app.services import ots
from tests.fakes import FakeCalendar

ROOT = hashlib.sha256(b"a-merkle-root").digest()


def test_stamp_commits_to_the_root_and_declares_sha256():
    proof = ots.stamp(ROOT, calendars=[FakeCalendar()])

    # The .ots file declares the anchored digest as a sha256 hash. That is a
    # true statement only because the Merkle layer is sha256 (09 D2) — it is
    # what lets stock OTS tooling reproduce the proof.
    assert ots.message(proof) == ROOT
    assert ots.file_hash_op_name(proof) == "sha256"


def test_a_freshly_stamped_proof_is_pending_not_confirmed():
    proof = ots.stamp(ROOT, calendars=[FakeCalendar(url="https://cal.test")])

    assert ots.is_confirmed(proof) is False
    assert ots.pending_calendar_urls(proof) == ["https://cal.test"]
    assert ots.bitcoin_block_heights(proof) == []


def test_one_calendar_is_enough_when_another_is_down():
    # Submitting to several calendars is redundancy, not consensus (09 §7).
    up = FakeCalendar(url="https://up.test")
    down = FakeCalendar(url="https://down.test", down=True)

    proof = ots.stamp(ROOT, calendars=[down, up])

    assert ots.message(proof) == ROOT
    assert up.submitted == [ROOT]


def test_stamp_raises_when_every_calendar_is_down():
    # No anchor row may be written claiming an anchor no calendar accepted
    # (09 D9), so this has to be an exception, not a partial success.
    with pytest.raises(ots.CalendarUnavailable):
        ots.stamp(ROOT, calendars=[FakeCalendar(down=True), FakeCalendar(down=True)])


def test_upgrade_returns_none_while_bitcoin_has_not_confirmed():
    # The normal case for hours after stamping — not an error, not a warning.
    calendar = FakeCalendar()
    proof = ots.stamp(ROOT, calendars=[calendar])

    assert ots.upgrade(proof, calendars=[calendar]) is None


def test_upgrade_attaches_the_bitcoin_attestation_once_confirmed():
    calendar = FakeCalendar(url="https://cal.test")
    proof = ots.stamp(ROOT, calendars=[calendar])

    calendar.confirm_at_height = 912_345
    upgraded = ots.upgrade(proof, calendars=[calendar])

    assert upgraded is not None
    assert ots.is_confirmed(upgraded) is True
    assert ots.bitcoin_block_heights(upgraded) == [912_345]
    # The upgraded proof still commits to the same root.
    assert ots.message(upgraded) == ROOT


def test_upgrade_returns_none_when_the_calendar_is_down():
    calendar = FakeCalendar()
    proof = ots.stamp(ROOT, calendars=[calendar])

    calendar.down = True
    assert ots.upgrade(proof, calendars=[calendar]) is None


def test_upgrade_ignores_calendars_the_proof_names_but_we_do_not_trust():
    # A proof carries calendar URLs inside it. Fetching from whatever URL a
    # blob names would let a tampered proof point us at an attacker's server,
    # so only configured calendars are ever contacted.
    stamping = FakeCalendar(url="https://attacker.test", confirm_at_height=1)
    proof = ots.stamp(ROOT, calendars=[stamping])

    ours = FakeCalendar(url="https://ours.test", confirm_at_height=1)
    assert ots.upgrade(proof, calendars=[ours]) is None
    assert ours.get_timestamp_calls == []


def test_an_already_confirmed_proof_is_not_re_fetched():
    calendar = FakeCalendar(url="https://cal.test", confirm_at_height=900_000)
    proof = ots.stamp(ROOT, calendars=[calendar])
    upgraded = ots.upgrade(proof, calendars=[calendar])
    assert upgraded is not None

    calls_before = len(calendar.get_timestamp_calls)
    assert ots.upgrade(upgraded, calendars=[calendar]) is None
    assert len(calendar.get_timestamp_calls) == calls_before


@pytest.mark.parametrize("blob", [b"", b"not-an-ots-file", bytes(64)])
def test_a_corrupt_proof_raises_a_typed_error(blob):
    # A garbage blob must not surface as a bare deserialization exception from
    # deep inside the library.
    with pytest.raises(ots.InvalidProof):
        ots.message(blob)
    with pytest.raises(ots.InvalidProof):
        ots.upgrade(blob, calendars=[FakeCalendar()])


def test_reading_a_proof_never_touches_a_calendar():
    calendar = FakeCalendar()
    proof = ots.stamp(ROOT, calendars=[calendar])
    calls = len(calendar.get_timestamp_calls)

    ots.message(proof)
    ots.is_confirmed(proof)
    ots.pending_calendar_urls(proof)
    ots.bitcoin_block_heights(proof)

    assert len(calendar.get_timestamp_calls) == calls
