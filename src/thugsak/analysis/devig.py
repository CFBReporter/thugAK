"""Convert sportsbook odds into fair probabilities by removing the vig.

A book's quoted prices sum to more than 100% -- that excess is its margin, and
comparing a raw implied probability against a Kalshi price would treat the
book's commission as a real disagreement.

Kalshi charges no vig at all (just a fee, handled in `fees`), which is the
structural reason a genuine edge can exist here: a no-vig venue and a vigged
one disagree by construction, and the comparison is only meaningful once the
vig is stripped out.

Two methods are provided because they disagree in a way worth seeing:

- **multiplicative** divides out the overround evenly. Simple and standard.
- **Shin** assumes part of the margin exists to protect the book against
  informed money, which concentrates more of the vig on the longshot. It is
  usually the better model for lopsided games.

Where the two diverge on a heavy favourite, that gap is a real uncertainty in
the fair price and the UI shows both rather than picking one silently.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence


def american_to_prob(odds: float | int) -> float:
    """Raw implied probability of American odds, vig included."""
    o = float(odds)
    if o == 0:
        raise ValueError("American odds cannot be 0")
    if o < 0:
        return (-o) / ((-o) + 100.0)
    return 100.0 / (o + 100.0)


def prob_to_american(p: float) -> int:
    """Inverse of `american_to_prob`, for displaying a model price as odds."""
    if not 0 < p < 1:
        raise ValueError(f"probability must be in (0,1), got {p}")
    if p >= 0.5:
        return int(round(-100.0 * p / (1.0 - p)))
    return int(round(100.0 * (1.0 - p) / p))


def decimal_to_prob(dec: float) -> float:
    if dec <= 1.0:
        raise ValueError(f"decimal odds must exceed 1.0, got {dec}")
    return 1.0 / float(dec)


@dataclass
class FairOdds:
    """Devigged probabilities plus the margin that was removed."""

    probs: tuple[float, ...]
    overround: float          # e.g. 0.041 for a 4.1% book margin
    method: str

    @property
    def vig_pct(self) -> float:
        return self.overround * 100.0

    def __getitem__(self, i: int) -> float:
        return self.probs[i]


def devig_multiplicative(raw: Sequence[float]) -> FairOdds:
    """Scale every probability by the same factor so they sum to 1."""
    total = sum(raw)
    if total <= 0:
        raise ValueError("probabilities must be positive")
    return FairOdds(
        probs=tuple(p / total for p in raw),
        overround=total - 1.0,
        method="multiplicative",
    )


def devig_additive(raw: Sequence[float]) -> FairOdds:
    """Subtract the overround equally from each outcome.

    Included for comparison; it can drive a heavy longshot negative, so it is
    not used as a default.
    """
    total = sum(raw)
    n = len(raw)
    excess = (total - 1.0) / n
    probs = tuple(max(p - excess, 1e-6) for p in raw)
    s = sum(probs)
    return FairOdds(
        probs=tuple(p / s for p in probs),
        overround=total - 1.0,
        method="additive",
    )


def devig_shin(raw: Sequence[float], *, max_iter: int = 60) -> FairOdds:
    """Shin's method: solve for the insider-trading proportion z.

    For two outcomes the implied fair probability given z is

        p_i = ( sqrt(z^2 + 4(1-z) * pi_i^2 / S) - z ) / (2(1-z))

    where pi_i are the quoted probabilities and S their sum. z is found by
    bisection on the constraint that the fair probabilities sum to 1, which is
    robust and needs no derivative.
    """
    total = sum(raw)
    if total <= 0:
        raise ValueError("probabilities must be positive")
    if len(raw) < 2:
        return devig_multiplicative(raw)

    def implied(z: float) -> list[float]:
        if z >= 1.0:
            return [1.0 / len(raw)] * len(raw)
        out = []
        for pi in raw:
            inner = z * z + 4.0 * (1.0 - z) * (pi * pi) / total
            out.append((inner**0.5 - z) / (2.0 * (1.0 - z)))
        return out

    lo, hi = 0.0, 0.99
    # z=0 reduces to the multiplicative case; if even that already sums below
    # 1 there is no overround to remove.
    if sum(implied(lo)) <= 1.0:
        return devig_multiplicative(raw)

    z = 0.0
    for _ in range(max_iter):
        z = (lo + hi) / 2.0
        s = sum(implied(z))
        if abs(s - 1.0) < 1e-10:
            break
        if s > 1.0:
            lo = z
        else:
            hi = z

    probs = implied(z)
    s = sum(probs)
    return FairOdds(
        probs=tuple(p / s for p in probs),
        overround=total - 1.0,
        method=f"shin(z={z:.4f})",
    )


def fair_from_american(
    *odds: float | int, method: str = "shin"
) -> FairOdds:
    """Devig a set of American odds. Pass both sides of a moneyline."""
    raw = [american_to_prob(o) for o in odds]
    if method == "multiplicative":
        return devig_multiplicative(raw)
    if method == "additive":
        return devig_additive(raw)
    if method == "shin":
        return devig_shin(raw)
    raise ValueError(f"unknown devig method {method!r}")


def fair_two_way(
    home_ml: float | int | None,
    away_ml: float | int | None,
    *,
    method: str = "shin",
) -> tuple[float, float, float] | None:
    """(home_prob, away_prob, overround) from a two-way moneyline.

    Returns None when either side is missing -- a one-sided quote cannot be
    devigged, and guessing would be worse than reporting no benchmark.
    """
    if home_ml is None or away_ml is None:
        return None
    fo = fair_from_american(home_ml, away_ml, method=method)
    return fo.probs[0], fo.probs[1], fo.overround


def consensus_fair(
    quotes: Sequence[tuple[float | int, float | int]], *, method: str = "shin"
) -> tuple[float, float] | None:
    """Average fair probability across several books.

    Each book is devigged before averaging -- averaging vigged prices first
    would bake the margins into the consensus.
    """
    fairs = [fair_two_way(h, a, method=method) for h, a in quotes]
    good = [f for f in fairs if f is not None]
    if not good:
        return None
    n = len(good)
    return sum(f[0] for f in good) / n, sum(f[1] for f in good) / n
