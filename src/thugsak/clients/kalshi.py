"""Kalshi public market-data client. No authentication anywhere.

Every endpoint used here is public: markets, events, orderbook, trades,
candlesticks and series all return 200 with no headers. The client never
touches an order or portfolio endpoint, so no API key exists in this project
and a bug cannot place a trade.

Three API details that cause silent wrongness if missed:

1. Prices arrive as decimal-dollar *strings* (``"0.3600"``), not integer cents.
   They are parsed to ``Decimal`` -- a binary float cannot represent 0.36 and
   the errors accumulate once fees are involved.
2. ``period_interval`` on candlesticks accepts only 1, 60 or 1440. Any other
   value returns an empty list rather than an error.
3. ``fee_multiplier`` is per-series and is 0 for fee-free series, so it is
   always read from the API rather than assumed to be 1.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Iterable

import httpx

from .. import cache
from ..config import CACHE_TTL, CANDLE_INTERVALS, KALSHI_BASE

MAX_LIMIT = 1000


def dec(v: Any) -> Decimal | None:
    """Parse a Kalshi decimal-dollar string. Returns None for blank fields."""
    if v is None or v == "":
        return None
    return Decimal(str(v))


def _f(v: Any) -> float | None:
    d = dec(v)
    return float(d) if d is not None else None


@dataclass
class Market:
    """One binary market. For a winner market this is one team's side; for a
    spread or total market it is one strike on the ladder."""

    ticker: str
    event_ticker: str
    title: str
    status: str
    yes_bid: Decimal | None
    yes_ask: Decimal | None
    no_bid: Decimal | None
    no_ask: Decimal | None
    last_price: Decimal | None
    volume: float
    volume_24h: float
    open_interest: float
    close_time: str | None
    yes_sub_title: str = ""
    strike_type: str = ""
    custom_strike: dict[str, Any] = field(default_factory=dict)
    rules_primary: str = ""
    rules_secondary: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def mid(self) -> Decimal | None:
        """Mid price, which for a binary market reads directly as a probability."""
        if self.yes_bid is None or self.yes_ask is None:
            return self.last_price
        return (self.yes_bid + self.yes_ask) / 2

    @property
    def spread_width(self) -> Decimal | None:
        if self.yes_bid is None or self.yes_ask is None:
            return None
        return self.yes_ask - self.yes_bid

    @classmethod
    def parse(cls, m: dict[str, Any]) -> "Market":
        return cls(
            ticker=m["ticker"],
            event_ticker=m.get("event_ticker", ""),
            title=m.get("title", ""),
            status=m.get("status", ""),
            yes_bid=dec(m.get("yes_bid_dollars")),
            yes_ask=dec(m.get("yes_ask_dollars")),
            no_bid=dec(m.get("no_bid_dollars")),
            no_ask=dec(m.get("no_ask_dollars")),
            last_price=dec(m.get("last_price_dollars")),
            volume=float(m.get("volume_fp") or 0),
            volume_24h=float(m.get("volume_24h_fp") or 0),
            open_interest=float(m.get("open_interest_fp") or 0),
            close_time=m.get("close_time"),
            yes_sub_title=m.get("yes_sub_title", "") or "",
            strike_type=m.get("strike_type", "") or "",
            custom_strike=m.get("custom_strike") or {},
            rules_primary=m.get("rules_primary", "") or "",
            rules_secondary=m.get("rules_secondary", "") or "",
            raw=m,
        )


@dataclass
class Trade:
    """A single executed trade. `taker_side` is the aggressor's direction,
    which is the conviction signal; `count` is size, which is the weight."""

    trade_id: str
    ticker: str
    created_time: str
    count: float
    yes_price: Decimal
    no_price: Decimal
    taker_side: str          # "yes" | "no"
    is_block_trade: bool

    @property
    def notional(self) -> float:
        """Dollars at risk on the taker's side -- the right size measure.

        1,000 contracts at 3c is a $30 punt; 1,000 at 90c is a $900 position.
        Ranking whales by contract count alone would confuse the two.
        """
        price = self.yes_price if self.taker_side == "yes" else self.no_price
        return float(price) * self.count

    @classmethod
    def parse(cls, t: dict[str, Any]) -> "Trade":
        return cls(
            trade_id=t["trade_id"],
            ticker=t["ticker"],
            created_time=t["created_time"],
            count=float(t.get("count_fp") or 0),
            yes_price=dec(t.get("yes_price_dollars")) or Decimal(0),
            no_price=dec(t.get("no_price_dollars")) or Decimal(0),
            taker_side=t.get("taker_side", ""),
            is_block_trade=bool(t.get("is_block_trade")),
        )


@dataclass
class Candle:
    """One interval of a market's history.

    `volume` is **per interval**, not cumulative, while `open_interest` is a
    level. Mistaking one for the other is how a pre-game money figure ends up
    including all the in-game trading that followed.

    `yes_ask` and `yes_bid` are the quotes at the interval's close, and they
    are what make an honest lookback possible: a reconstruction priced at the
    last *trade* would be optimistic by the width of the spread, since buying
    costs the ask. `no_ask` follows from the binary identity.
    """

    end_ts: int
    open: float | None
    high: float | None
    low: float | None
    close: float | None
    mean: float | None
    volume: float
    open_interest: float
    yes_ask: Decimal | None = None
    yes_bid: Decimal | None = None

    @property
    def no_ask(self) -> Decimal | None:
        """Cost of buying NO: one minus the resting YES bid."""
        return None if self.yes_bid is None else Decimal(1) - self.yes_bid

    @property
    def no_bid(self) -> Decimal | None:
        return None if self.yes_ask is None else Decimal(1) - self.yes_ask

    @classmethod
    def parse(cls, c: dict[str, Any]) -> "Candle":
        p = c.get("price") or {}
        ask = c.get("yes_ask") or {}
        bid = c.get("yes_bid") or {}
        return cls(
            end_ts=int(c.get("end_period_ts") or 0),
            open=_f(p.get("open_dollars")),
            high=_f(p.get("high_dollars")),
            low=_f(p.get("low_dollars")),
            close=_f(p.get("close_dollars")),
            mean=_f(p.get("mean_dollars")),
            volume=float(c.get("volume_fp") or 0),
            open_interest=float(c.get("open_interest_fp") or 0),
            yes_ask=dec(ask.get("close_dollars")),
            yes_bid=dec(bid.get("close_dollars")),
        )


class KalshiClient:
    def __init__(self, client: httpx.AsyncClient | None = None):
        self._client = client
        self._owns = client is None

    async def __aenter__(self) -> "KalshiClient":
        if self._client is None:
            self._client = cache.make_client()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._owns and self._client is not None:
            await self._client.aclose()

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("use KalshiClient as an async context manager")
        return self._client

    async def _get(
        self, path: str, params: dict[str, Any] | None = None, *, ttl: float | None = 60
    ) -> Any:
        res = await cache.fetch_json(
            self.client, f"{KALSHI_BASE}{path}", params, ttl=ttl
        )
        return res.data

    # ---------------- markets and events ----------------

    async def markets(
        self,
        *,
        series_ticker: str | None = None,
        event_ticker: str | None = None,
        status: str = "open",
        limit: int = 200,
        max_pages: int = 20,
    ) -> list[Market]:
        """All markets for a series or event, following cursor pagination."""
        params: dict[str, Any] = {"limit": min(limit, MAX_LIMIT), "status": status}
        if series_ticker:
            params["series_ticker"] = series_ticker
        if event_ticker:
            params["event_ticker"] = event_ticker

        out: list[Market] = []
        cursor: str | None = None
        for _ in range(max_pages):
            p = dict(params)
            if cursor:
                p["cursor"] = cursor
            data = await self._get("/markets", p, ttl=CACHE_TTL["kalshi_markets"])
            batch = data.get("markets") or []
            out += [Market.parse(m) for m in batch]
            cursor = data.get("cursor")
            if not cursor or not batch:
                break
        return out

    async def events(
        self,
        series_ticker: str,
        *,
        status: str = "open",
        with_nested_markets: bool = True,
        limit: int = 200,
        max_pages: int = 20,
    ) -> list[dict[str, Any]]:
        """Events (games) for a series, each carrying its markets when nested.

        Preferred over /markets for building a slate: one request yields the
        game and both team sides together, already grouped.
        """
        params: dict[str, Any] = {
            "series_ticker": series_ticker,
            "status": status,
            "limit": min(limit, MAX_LIMIT),
            "with_nested_markets": str(with_nested_markets).lower(),
        }
        out: list[dict[str, Any]] = []
        cursor: str | None = None
        for _ in range(max_pages):
            p = dict(params)
            if cursor:
                p["cursor"] = cursor
            data = await self._get("/events", p, ttl=CACHE_TTL["kalshi_markets"])
            batch = data.get("events") or []
            out += batch
            cursor = data.get("cursor")
            if not cursor or not batch:
                break
        return out

    async def series(self, series_ticker: str) -> dict[str, Any]:
        """Series metadata, including the all-important `fee_multiplier`."""
        data = await self._get(
            f"/series/{series_ticker}", ttl=CACHE_TTL["kalshi_series"]
        )
        return data.get("series", data)

    async def fee_multiplier(self, series_ticker: str) -> Decimal:
        """Read the fee multiplier rather than assuming it.

        Kalshi uses 0 for fee-free series, so defaulting to 1 would invent
        fees that do not exist and suppress real edges.
        """
        s = await self.series(series_ticker)
        raw = s.get("fee_multiplier")
        return Decimal(str(raw)) if raw is not None else Decimal(1)

    # ---------------- microstructure ----------------

    async def orderbook(self, ticker: str) -> dict[str, list[tuple[Decimal, float]]]:
        """Full depth as {"yes": [(price, size)...], "no": [...]}, best first."""
        data = await self._get(
            f"/markets/{ticker}/orderbook", ttl=CACHE_TTL["kalshi_orderbook"]
        )
        ob = data.get("orderbook_fp") or data.get("orderbook") or {}
        out: dict[str, list[tuple[Decimal, float]]] = {"yes": [], "no": []}
        for side, key in (("yes", "yes_dollars"), ("no", "no_dollars")):
            levels = ob.get(key) or []
            parsed = [(Decimal(str(p)), float(s)) for p, s in levels]
            # API returns ascending price; best bid is the highest.
            out[side] = sorted(parsed, key=lambda x: x[0], reverse=True)
        return out

    async def trades(
        self,
        ticker: str,
        *,
        limit: int = MAX_LIMIT,
        min_ts: int | None = None,
        max_pages: int = 10,
    ) -> list[Trade]:
        """Executed trades, newest first, following the cursor.

        `min_ts` bounds how far back to walk -- the flow analysis only cares
        about a recent window, and a liquid market has far more history than
        is useful.
        """
        params: dict[str, Any] = {"ticker": ticker, "limit": min(limit, MAX_LIMIT)}
        if min_ts:
            params["min_ts"] = min_ts
        out: list[Trade] = []
        cursor: str | None = None
        for _ in range(max_pages):
            p = dict(params)
            if cursor:
                p["cursor"] = cursor
            data = await self._get(
                "/markets/trades", p, ttl=CACHE_TTL["kalshi_trades"]
            )
            batch = data.get("trades") or []
            out += [Trade.parse(t) for t in batch]
            cursor = data.get("cursor")
            if not cursor or not batch:
                break
        return out

    async def candlesticks(
        self,
        series_ticker: str,
        ticker: str,
        *,
        period_interval: int = 60,
        start_ts: int | None = None,
        end_ts: int | None = None,
        lookback_hours: int = 48,
    ) -> list[Candle]:
        """Price/volume/open-interest history.

        Note the path: candlesticks live under the *series*, not directly under
        the market (``/markets/{t}/candlesticks`` is a 404).
        """
        if period_interval not in CANDLE_INTERVALS:
            raise ValueError(
                f"period_interval must be one of {CANDLE_INTERVALS}; "
                f"got {period_interval}. Other values return an empty list "
                f"rather than an error, which is easy to mistake for no data."
            )
        now = datetime.now(timezone.utc)
        end_ts = end_ts or int(now.timestamp())
        start_ts = start_ts or int((now - timedelta(hours=lookback_hours)).timestamp())
        data = await self._get(
            f"/series/{series_ticker}/markets/{ticker}/candlesticks",
            {
                "period_interval": period_interval,
                "start_ts": start_ts,
                "end_ts": end_ts,
            },
            ttl=CACHE_TTL["kalshi_candles"],
        )
        return [Candle.parse(c) for c in (data.get("candlesticks") or [])]


def cost_to_fill(
    book: Iterable[tuple[Decimal, float]], contracts: float
) -> tuple[Decimal | None, float]:
    """Average price to fill `contracts` by walking the book.

    Top-of-book flatters thin markets: a 2c edge against 50 resting contracts
    is not a 2c edge if you want 500. Returns (avg_price, filled) where
    filled < contracts means the book ran out.
    """
    spent = Decimal(0)
    filled = 0.0
    for price, size in book:
        if filled >= contracts:
            break
        take = min(size, contracts - filled)
        spent += price * Decimal(str(take))
        filled += take
    if filled == 0:
        return None, 0.0
    return spent / Decimal(str(filled)), filled
