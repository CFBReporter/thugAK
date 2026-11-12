"""The as-of guard: the one place that decides what the model is allowed to see.

Why this exists as its own module rather than a convention: historical power
ratings published by ESPN and Barttorvik are *end-of-season* snapshots. Feeding
one of those into a prediction for a mid-season game means the rating already
knows how that game turned out, and the resulting lookback looks uncannily
accurate while the live model is no better than chance.

Rules enforced here:

1. A prediction for a game is computed as of kickoff minus `LEAD_MINUTES`.
2. Every row feeding it must have started strictly before that cutoff.
3. Published ratings are never model inputs -- `model.ratings` recomputes them
   from the games we can legitimately see. ESPN FPI and Barttorvik stay as
   present-day cross-checks, displayed beside our number but not folded into it.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any

from ..localtime import parse_ts

# How far before kickoff a prediction is pinned. An hour back is late enough
# that lineups and injury reports are public, early enough to be a real
# pre-game decision.
LEAD_MINUTES = 60


class LookaheadError(RuntimeError):
    """A query or dataset exposed data at or after the as-of cutoff."""


# Re-exported from `thugsak.localtime`, which owns timestamp parsing. Cutoffs
# are compared, so they stay UTC: a local-time cutoff is non-monotonic across
# the autumn DST fold, where two different instants render identically.
__all__ = [
    "LEAD_MINUTES",
    "LookaheadError",
    "as_of_for_game",
    "as_of_for_kickoff",
    "assert_before",
    "iso",
    "now_iso",
    "parse_ts",
    "require_as_of",
    "split_by_availability",
]


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def now_iso() -> str:
    return iso(datetime.now(timezone.utc))


def as_of_for_kickoff(start_time: str | datetime, lead_minutes: int = LEAD_MINUTES) -> str:
    """The cutoff for predicting a game that starts at `start_time`.

    Used identically for tonight's games and for a lookback on a game from
    October, so a lookback exercises the same code path as a live prediction.
    """
    return iso(parse_ts(start_time) - timedelta(minutes=lead_minutes))


def as_of_for_game(game: Any, lead_minutes: int = LEAD_MINUTES) -> str:
    """as-of cutoff for a game row/dict carrying `start_time`."""
    st = game["start_time"] if not hasattr(game, "start_time") else game.start_time
    return as_of_for_kickoff(st, lead_minutes)


def assert_before(rows: Iterable[Any], as_of: str, *, field: str = "start_time") -> None:
    """Fail loudly if any row is at or after the cutoff.

    Called on datasets handed to the model. A leak that merely produces a
    slightly-too-good number is far more dangerous than a crash, so this
    raises rather than filtering silently.
    """
    cutoff = parse_ts(as_of)
    bad: list[str] = []
    for r in rows:
        try:
            val = r[field]
        except (TypeError, KeyError, IndexError):
            val = getattr(r, field, None)
        if val is None:
            continue
        if parse_ts(val) >= cutoff:
            bad.append(str(val))
    if bad:
        raise LookaheadError(
            f"{len(bad)} row(s) at or after as_of={as_of}; "
            f"first offenders: {sorted(bad)[:3]}"
        )


def require_as_of(as_of: str | None) -> str:
    """Validate an as-of argument at a public boundary."""
    if not as_of or not isinstance(as_of, str):
        raise LookaheadError(
            f"as_of is mandatory and must be an ISO timestamp, got {as_of!r}"
        )
    parse_ts(as_of)  # raises if unparseable
    return as_of


def split_by_availability(
    rows: Sequence[Any], as_of: str, *, field: str = "start_time"
) -> tuple[list[Any], list[Any]]:
    """Partition rows into (visible, withheld) around the cutoff.

    Used by the lookback view, which legitimately needs both sides: the
    visible rows drive the prediction, the withheld ones hold the result we
    are grading against.
    """
    cutoff = parse_ts(as_of)
    visible, withheld = [], []
    for r in rows:
        try:
            val = r[field]
        except (TypeError, KeyError, IndexError):
            val = getattr(r, field, None)
        (visible if val is not None and parse_ts(val) < cutoff else withheld).append(r)
    return visible, withheld
