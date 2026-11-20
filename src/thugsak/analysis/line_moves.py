"""Significant line moves: where a sportsbook's line now sits far from where
it was first seen (by the hourly job, not necessarily the book's true open).

Thresholds, chosen so a flag means something a bettor would notice:
  moneyline  implied win chance moved >= MONEYLINE_PROB_POINTS points
             (-110 -> +210 is 52% -> 32%, a 20-point move)
  spread     the number moved >= SPREAD_POINTS points
  total      the number moved >= TOTAL_POINTS points
"""

from __future__ import annotations

from typing import Any, Sequence

MONEYLINE_PROB_POINTS = 8.0
SPREAD_POINTS = 2.0
TOTAL_POINTS = 3.0

BOOK_NAMES = {
    "draftkings": "DraftKings", "fanduel": "FanDuel", "betmgm": "BetMGM", "caesars": "Caesars",
    "pinnacle": "Pinnacle", "bet365": "bet365", "espnbet": "ESPN BET", "fanatics": "Fanatics",
    "hardrock": "Hard Rock", "betrivers": "BetRivers",
}


def implied(odds: float) -> float:
    """American odds -> implied probability (with the book's margin)."""
    return 100.0 / (odds + 100.0) if odds > 0 else -odds / (-odds + 100.0)


def _american(o: float) -> str:
    return f"+{o:.0f}" if o > 0 else f"{o:.0f}"


def _book(b: str) -> str:
    return BOOK_NAMES.get(b, b.replace("_", " ").title())


def significant(rows: Sequence[Any], home_label: str, away_label: str) -> list[dict[str, Any]]:
    """The rows (from store.line_history) whose move clears its threshold,
    biggest first, each with a one-line note."""
    out = []
    for r in rows:
        market, side, book = r["market"], r["side"], _book(r["sportsbook"])
        team = home_label if side == "home" else away_label
        if market == "moneyline":
            o, n = r["open_odds"], r["last_odds"]
            if o is None or n is None:
                continue
            po, pn = implied(o) * 100, implied(n) * 100
            size = abs(pn - po)
            if size < MONEYLINE_PROB_POINTS:
                continue
            note = (f"{team} moneyline at {book}: first seen {_american(o)}, now {_american(n)} "
                    f"({po:.0f}% -> {pn:.0f}% implied, {pn - po:+.0f} pts)")
            start, end, change = _american(o), _american(n), f"{pn - po:+.0f} pts implied"
            score = size / MONEYLINE_PROB_POINTS
        else:
            o, n = r["open_line"], r["last_line"]
            if o is None or n is None:
                continue
            size = abs(n - o)
            limit = SPREAD_POINTS if market == "spread" else TOTAL_POINTS
            if size < limit:
                continue
            if market == "spread":
                note = (f"{home_label} spread at {book}: first seen {o:+.1f}, now {n:+.1f} "
                        f"({n - o:+.1f} pts)")
                start, end = f"{o:+.1f}", f"{n:+.1f}"
            else:
                note = f"Total at {book}: first seen {o:.1f}, now {n:.1f} ({n - o:+.1f} pts)"
                start, end = f"{o:.1f}", f"{n:.1f}"
            change = f"{n - o:+.1f} pts"
            score = size / limit
        out.append({
            "sportsbook": book, "market": market,
            "selection": team if market != "total" else "Total",
            "start": start, "end": end, "change": change, "note": note,
            "first_seen_at": r["open_at"], "last_seen_at": r["last_at"], "_score": score,
        })
    out.sort(key=lambda m: -m["_score"])
    for m in out:
        m.pop("_score")
    return out
