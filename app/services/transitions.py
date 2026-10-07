"""The six-state batch lifecycle machine (P3-D, 04 §5.2).

`CREATED -> HARVESTED -> PROCESSED -> LAB_VERIFIED -> PACKAGED -> DISTRIBUTED`.

Linear, so the table is a mapping from each state to its single successor
rather than a set of edges: that shape makes "no state skipping" structural
instead of a rule someone has to enforce. `DISTRIBUTED` is terminal and simply
absent from the mapping.

Pure — no session, no batch, no request. The handler owns serialization (a
`SELECT ... FOR UPDATE` on the batch row before this check) and the database
owns the final word (`UNIQUE(batch_id)` on every stage table). This module only
answers whether one state may become another.
"""

from collections.abc import Mapping

from app.enums import BatchState
from app.errors import APIError

LEGAL: Mapping[BatchState, BatchState] = {
    BatchState.CREATED: BatchState.HARVESTED,
    BatchState.HARVESTED: BatchState.PROCESSED,
    BatchState.PROCESSED: BatchState.LAB_VERIFIED,
    BatchState.LAB_VERIFIED: BatchState.PACKAGED,
    BatchState.PACKAGED: BatchState.DISTRIBUTED,
}


def next_state(current: BatchState) -> BatchState | None:
    """The one state `current` may become, or None if it is terminal."""
    return LEGAL.get(current)


def assert_transition(current: BatchState, target: BatchState) -> None:
    """Raise 409 `invalid_transition` unless `current -> target` is the one
    legal step.

    The error carries both states in `details` so a client can tell a
    double-submit ("already harvested") from a genuine ordering mistake
    ("cannot package before the lab has reported") without parsing prose.
    """
    if LEGAL.get(current) is not target:
        raise APIError(
            409,
            "invalid_transition",
            f"A batch in {current} cannot move to {target}",
            {"current_state": str(current), "attempted_state": str(target)},
        )
