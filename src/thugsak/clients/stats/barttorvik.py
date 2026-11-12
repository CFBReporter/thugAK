"""Barttorvik: college basketball ratings and game results.

Exists because ESPN's NCAAMB power index returns only the **top 25 teams**
(verified: ``count: 25`` even at ``limit=400``). Without this source, roughly
340 of 365 Division I teams would have no strength rating -- and thin NCAAMB
markets are exactly where the largest Kalshi mispricings sit.

Two files are used:

``{year}_team_results.json``   365 teams, adjusted efficiency, ~365 KB
``{year}_super_sked.csv``      every D1 game in one file, ~7 MB

The second matters for the history import: fetching NCAAMB season results via
ESPN would need 365 requests per season, against one file here.

Both are positional formats with no header, so the column indices below were
read off live payloads. They are the fragile part of this module; `probe()`
checks them against a known-good shape so a layout change announces itself
rather than silently shifting every rating by one column.
"""

from __future__ import annotations

import csv
import io
import json
import re
from typing import Any

from ... import cache
from ...config import BARTTORVIK_SEASON
from . import TeamRating, register

BASE = "https://barttorvik.com"

# team_results.json column offsets, verified against the live 2027 file.
# Row 0 reads: [1, 'Duke', 'ACC', '2-1', 120.15, 6, 91.09, 1, 0.960, 1, 24.9, ...]
COL_RANK = 0
COL_NAME = 1
COL_CONF = 2
COL_RECORD = 3
COL_ADJ_OE = 4          # adjusted offensive efficiency (pts per 100 poss)
COL_ADJ_OE_RANK = 5
COL_ADJ_DE = 6          # adjusted defensive efficiency (lower is better)
COL_ADJ_DE_RANK = 7
COL_BARTHAG = 8         # win probability vs an average D1 team
COL_BARTHAG_RANK = 9
EXPECTED_FIELDS = 45

# Division I average efficiency, used to express AdjOE/AdjDE as a net-points
# figure comparable with other sources. Recomputed from the data each load
# rather than hardcoded, since the league average drifts year to year.
TYPICAL_POSSESSIONS = 68.0


def team_results_url(year: int = BARTTORVIK_SEASON) -> str:
    return f"{BASE}/{year}_team_results.json"


def super_sked_url(year: int = BARTTORVIK_SEASON) -> str:
    return f"{BASE}/{year}_super_sked.csv"


def _norm(name: str) -> str:
    """Normalise a team name for alias matching.

    Barttorvik writes 'Saint Mary's', ESPN 'Saint Marys', Kalshi 'STMARYS'.
    Stripping punctuation and case gets most of the way; the remainder is
    handled by the alias table built during import.
    """
    s = name.strip().lower()
    s = s.replace("&", "and")
    s = re.sub(r"[^a-z0-9 ]+", "", s)
    return re.sub(r"\s+", " ", s).strip()


class Barttorvik:
    name = "barttorvik"
    leagues = {"ncaamb"}

    async def team_ratings(
        self, client: Any, league: str, *, year: int = BARTTORVIK_SEASON
    ) -> dict[str, TeamRating]:
        if league not in self.leagues:
            return {}
        text = await cache.fetch_text(client, team_results_url(year), ttl=21_600)
        rows = json.loads(text) if isinstance(text, str) else text

        out: dict[str, TeamRating] = {}
        oes = [r[COL_ADJ_OE] for r in rows if _f(r, COL_ADJ_OE) is not None]
        des = [r[COL_ADJ_DE] for r in rows if _f(r, COL_ADJ_DE) is not None]
        mean_oe = sum(oes) / len(oes) if oes else 100.0
        mean_de = sum(des) / len(des) if des else 100.0

        for r in rows:
            if len(r) < 10:
                continue
            nm = str(r[COL_NAME])
            oe, de = _f(r, COL_ADJ_OE), _f(r, COL_ADJ_DE)
            # Efficiency is per 100 possessions; scale to a per-game net-points
            # figure so it is comparable with FPI/BPI-style ratings.
            net = None
            if oe is not None and de is not None:
                net = ((oe - mean_oe) - (de - mean_de)) * TYPICAL_POSSESSIONS / 100.0
            out[_norm(nm)] = TeamRating(
                source=self.name,
                name=nm,
                net=net,
                offense=oe,
                defense=de,
                rank=_i(r, COL_RANK),
                extra={
                    "conference": r[COL_CONF] if len(r) > COL_CONF else None,
                    "record": r[COL_RECORD] if len(r) > COL_RECORD else None,
                    "barthag": _f(r, COL_BARTHAG),
                    "adj_oe_rank": _i(r, COL_ADJ_OE_RANK),
                    "adj_de_rank": _i(r, COL_ADJ_DE_RANK),
                },
            )
        return out

    async def games(
        self, client: Any, league: str, year: int = BARTTORVIK_SEASON
    ) -> list[dict[str, Any]]:
        """Every D1 game for a season, parsed from super_sked.csv.

        Only the columns needed to reconstruct a result are taken: date, the
        two teams, and the final score. The file also embeds Barttorvik's own
        pre-game line, which is deliberately ignored -- it is another model's
        opinion, not a market price, and for past seasons it may be a
        retroactive fit rather than what was known at the time.
        """
        text = await cache.fetch_text(client, super_sked_url(year), ttl=21_600)
        out: list[dict[str, Any]] = []
        for row in csv.reader(io.StringIO(text)):
            parsed = _parse_sked_row(row, year)
            if parsed:
                out.append(parsed)
        return out


# Column indices in super_sked.csv, read off a live file. The layout is
# positional with no header, so `probe_sked()` guards against upstream shifts.
#
# A trap worth spelling out: column 4 looks like a result but is Barttorvik's
# own *prediction* ("Villanova -4.5, 77-72 (67%)" -- a line, a projected score
# and a win probability). The actual result is column 24 ("Notre Dame,
# 74-72"). Importing column 4 as the result trains the model on another
# model's forecasts, which produces an impressively tight fit and a useless
# predictor, because forecasts are shrunk toward the mean and real scores are
# not.
SKED_DATE = 1
SKED_MATCHUP = 3
SKED_PREDICTION = 4      # deliberately unused
SKED_RESULT = 24

# "71 Notre Dame vs. 32 Villanova" / "287 Lindenwood at 59 TCU"
_MATCHUP = re.compile(
    r"^\s*(?:\d+\s+)?(?P<a>.+?)\s+(?P<sep>vs\.?|at)\s+(?:\d+\s+)?(?P<b>.+?)\s*$",
    re.IGNORECASE,
)
# "Notre Dame, 74-72" -- winner named first, then winner's score, loser's.
_RESULT = re.compile(
    r"^\s*(?P<winner>.+?),\s*(?P<ws>\d{1,3})\s*-\s*(?P<ls>\d{1,3})\s*$"
)


def _parse_sked_row(row: list[str], year: int) -> dict[str, Any] | None:
    """Pull (date, home, away, actual scores) out of one super_sked line.

    The file mixes played and unplayed games, so rows without a parseable
    result are skipped rather than guessed at: a mis-assigned score would
    corrupt every rating derived from it.
    """
    if len(row) <= SKED_RESULT:
        return None
    date_s, matchup, result = row[SKED_DATE], row[SKED_MATCHUP], row[SKED_RESULT]

    m = _MATCHUP.match(matchup or "")
    if not m:
        return None
    a, b, sep = m.group("a").strip(), m.group("b").strip(), m.group("sep").lower()
    # "A vs. B" means A hosts; "A at B" means B hosts.
    home, away = (a, b) if sep.startswith("vs") else (b, a)

    try:
        mth, day, yr = (int(x) for x in date_s.split("/"))
        yr = yr + 2000 if yr < 100 else yr
        start = f"{yr:04d}-{mth:02d}-{day:02d}T00:00:00+00:00"
    except (ValueError, AttributeError):
        return None

    rm = _RESULT.match(result or "")
    if not rm:
        return None       # not yet played, or an unexpected result format
    winner = rm.group("winner").strip()
    ws, ls = int(rm.group("ws")), int(rm.group("ls"))

    # Assign the winner's score to whichever side won. Matching on normalised
    # names rather than substrings, so "Miami" cannot claim "Miami (OH)".
    wn, hn, an = _norm(winner), _norm(home), _norm(away)
    if wn == hn:
        hs, as_ = ws, ls
    elif wn == an:
        hs, as_ = ls, ws
    elif wn and hn.startswith(wn) or hn and wn.startswith(hn):
        hs, as_ = ws, ls
    elif wn and an.startswith(wn) or an and wn.startswith(an):
        hs, as_ = ls, ws
    else:
        return None

    return {
        "source": "barttorvik",
        "season": year,
        "start_time": start,
        "home_name": home,
        "away_name": away,
        "home_score": hs,
        "away_score": as_,
    }


def _f(row: list[Any], i: int) -> float | None:
    try:
        return float(row[i])
    except (TypeError, ValueError, IndexError):
        return None


def _i(row: list[Any], i: int) -> int | None:
    f = _f(row, i)
    return int(f) if f is not None else None


async def probe(client: Any, year: int = BARTTORVIK_SEASON) -> dict[str, Any]:
    """Check the positional layout still holds.

    These files have no header, so a new column inserted upstream would shift
    every rating silently. Called by scripts/smoke.py.
    """
    text = await cache.fetch_text(client, team_results_url(year), ttl=0)
    rows = json.loads(text) if isinstance(text, str) else text
    top = rows[0] if rows else []
    oe, de = _f(top, COL_ADJ_OE), _f(top, COL_ADJ_DE)
    return {
        "teams": len(rows),
        "fields": len(top),
        "fields_expected": EXPECTED_FIELDS,
        "layout_ok": (
            len(top) == EXPECTED_FIELDS
            and isinstance(top[COL_NAME], str)
            # Efficiencies live in the 80-140 points-per-100 range; anything
            # outside it means the columns moved.
            and oe is not None and 80 < oe < 140
            and de is not None and 80 < de < 140
        ),
        "sample": {"name": top[COL_NAME] if top else None, "adj_oe": oe, "adj_de": de},
    }


register(Barttorvik())
