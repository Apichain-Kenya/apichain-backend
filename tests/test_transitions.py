"""The six-state lifecycle machine (P3-D, 10 §7).

`CREATED -> HARVESTED -> PROCESSED -> LAB_VERIFIED -> PACKAGED -> DISTRIBUTED`,
linear, no skipping, `DISTRIBUTED` terminal. Pure: the machine knows nothing
about batches, sessions or requests, so every pair can be tested exhaustively.

The exhaustive test matters more than it looks. "No state skipping" is the
project's headline traceability claim; a table with one wrong entry would let a
batch jump from `CREATED` to `DISTRIBUTED` and every gate would stay green.
"""

import itertools

import pytest

from app.enums import BatchState
from app.errors import APIError
from app.services import transitions

_LEGAL = {
    (BatchState.CREATED, BatchState.HARVESTED),
    (BatchState.HARVESTED, BatchState.PROCESSED),
    (BatchState.PROCESSED, BatchState.LAB_VERIFIED),
    (BatchState.LAB_VERIFIED, BatchState.PACKAGED),
    (BatchState.PACKAGED, BatchState.DISTRIBUTED),
}


@pytest.mark.parametrize(("current", "target"), sorted(_LEGAL))
def test_each_legal_step_is_allowed(current, target):
    transitions.assert_transition(current, target)  # does not raise


@pytest.mark.parametrize(
    ("current", "target"),
    sorted(set(itertools.product(BatchState, BatchState)) - _LEGAL),
)
def test_every_other_pair_of_states_is_refused(current, target):
    """31 of the 36 pairs, including every skip, every rewind, and self-loops."""
    with pytest.raises(APIError) as excinfo:
        transitions.assert_transition(current, target)

    error = excinfo.value
    assert error.status_code == 409
    assert error.code == "invalid_transition"
    assert error.details == {"current_state": str(current), "attempted_state": str(target)}


def test_a_created_batch_cannot_skip_straight_to_distributed():
    """Named on its own because it is the claim the whole project rests on."""
    with pytest.raises(APIError):
        transitions.assert_transition(BatchState.CREATED, BatchState.DISTRIBUTED)


def test_distributed_is_terminal():
    assert transitions.next_state(BatchState.DISTRIBUTED) is None
    for target in BatchState:
        with pytest.raises(APIError):
            transitions.assert_transition(BatchState.DISTRIBUTED, target)


def test_next_state_agrees_with_the_legal_set():
    derived = {(s, t) for s in BatchState if (t := transitions.next_state(s)) is not None}

    assert derived == _LEGAL


def test_the_machine_covers_every_state_in_the_enum():
    """Adding a state to BatchState without wiring it here fails here, rather
    than silently creating a state no batch can ever leave."""
    reachable = set(transitions.LEGAL) | set(transitions.LEGAL.values())

    assert reachable == set(BatchState)
