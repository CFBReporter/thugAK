"""Kalshi trading fees.

Fee-blindness is the single most common way a tool like this manufactures
edges that do not exist, so every price comparison in the project runs through
here.

    taker fee = ceil_to_cent( fee_multiplier * 0.07 * contracts * P * (1-P) )
    maker fee = ceil_to_cent( fee_multiplier * 0.0175 * contracts * P * (1-P) )

The quadratic term peaks at P = 0.50, where 100 contracts cost $1.75 -- the
published reference point the unit tests assert against.

`fee_multiplier` is per-series and read from the API, never assumed. Kalshi
uses 0 for fee-free series, so hardcoding 1 would invent costs and suppress
genuine opportunities; assuming 1 on a series that charges more would do the
reverse.
"""

from __future__ import annotations

from decimal import ROUND_CEILING, Decimal
from typing import Iterable

from ..config import FEE_RATE_MAKER, FEE_RATE_TAKER

CENT = Decimal("0.01")


def _to_dec(v: Decimal | float | int | str) -> Decimal:
    return v if isinstance(v, Decimal) else Decimal(str(v))


def ceil_cent(amount: Decimal) -> Decimal:
    """Round up to the next whole cent, as Kalshi does per order."""
    return amount.quantize(CENT, rounding=ROUND_CEILING)


def trade_fee(
    contracts: Decimal | float,
    price: Decimal | float,
    *,
    fee_multiplier: Decimal | float = 1,
    maker: bool = False,
) -> Decimal:
    """Total fee in dollars for one order.

    `price` is the dollar price of the contract (0.36, not 36).
    """
    c = _to_dec(contracts)
    p = _to_dec(price)
    mult = _to_dec(fee_multiplier)
    if c <= 0 or mult == 0:
        return Decimal("0.00")
    # Prices outside (0,1) carry no risk and no fee.
    if p <= 0 or p >= 1:
        return Decimal("0.00")
    rate = FEE_RATE_MAKER if maker else FEE_RATE_TAKER
    return ceil_cent(mult * rate * c * p * (Decimal(1) - p))


def fee_per_contract(
    contracts: Decimal | float,
    price: Decimal | float,
    *,
    fee_multiplier: Decimal | float = 1,
    maker: bool = False,
) -> Decimal:
    """Per-contract fee at a given order size.

    Size-aware on purpose: the fee is rounded up once per order, so a
    one-contract order pays a disproportionate rounding penalty. Charging that
    rate against a realistic position would understate the edge.
    """
    c = _to_dec(contracts)
    if c <= 0:
        return Decimal("0.00")
    total = trade_fee(c, price, fee_multiplier=fee_multiplier, maker=maker)
    return total / c


def effective_cost(
    price: Decimal | float,
    contracts: Decimal | float = 100,
    *,
    fee_multiplier: Decimal | float = 1,
    maker: bool = False,
) -> Decimal:
    """All-in per-contract cost to buy at `price`: the real breakeven.

    This, not the quoted ask, is what a model probability must beat.
    """
    p = _to_dec(price)
    return p + fee_per_contract(
        contracts, p, fee_multiplier=fee_multiplier, maker=maker
    )


def breakeven_probability(
    price: Decimal | float,
    contracts: Decimal | float = 100,
    *,
    fee_multiplier: Decimal | float = 1,
    maker: bool = False,
) -> Decimal:
    """The win probability needed to break even after fees."""
    return effective_cost(
        price, contracts, fee_multiplier=fee_multiplier, maker=maker
    )


def expected_value(
    model_prob: Decimal | float,
    price: Decimal | float,
    contracts: Decimal | float = 100,
    *,
    fee_multiplier: Decimal | float = 1,
    maker: bool = False,
) -> Decimal:
    """Expected profit in dollars for buying YES at `price`.

    A binary contract settles at $1 or $0, so per contract the expectation is
    ``prob * (1 - price) - (1 - prob) * price`` and the fee comes off the top.
    """
    q = _to_dec(model_prob)
    p = _to_dec(price)
    c = _to_dec(contracts)
    gross = c * (q * (Decimal(1) - p) - (Decimal(1) - q) * p)
    fee = trade_fee(c, p, fee_multiplier=fee_multiplier, maker=maker)
    return gross - fee


def max_fee_per_100(fee_multiplier: Decimal | float = 1) -> Decimal:
    """The fee ceiling: 100 contracts at $0.50, where the quadratic peaks."""
    return trade_fee(100, Decimal("0.50"), fee_multiplier=fee_multiplier)


def arbitrage_check(
    yes_price: Decimal | float,
    other_side_price: Decimal | float,
    contracts: Decimal | float = 100,
    *,
    fee_multiplier: Decimal | float = 1,
) -> tuple[bool, Decimal]:
    """Whether buying both sides locks a profit, and by how much per contract.

    Two complementary binaries that together cost less than $1 after fees pay
    $1 whatever happens. Reported separately from probabilistic edges because
    it needs no model to be true.
    """
    a, b = _to_dec(yes_price), _to_dec(other_side_price)
    c = _to_dec(contracts)
    fees = (
        trade_fee(c, a, fee_multiplier=fee_multiplier)
        + trade_fee(c, b, fee_multiplier=fee_multiplier)
    )
    cost_each = a + b + (fees / c if c else Decimal(0))
    profit = Decimal(1) - cost_each
    return profit > 0, profit
