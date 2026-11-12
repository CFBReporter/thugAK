"""League registry and project-wide constants.

Every per-league quirk lives here. The leagues are genuinely inconsistent in
ways that bite silently, so they are declared once and read everywhere:

- NBA's FPI/BPI season is labelled by the season's *end* year (2027 for the
  2026-27 season), unlike every other league.
- NCAAMB's ESPN power index only covers the top 25 teams, so its strength
  ratings come from Barttorvik instead.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = PROJECT_ROOT / "data"
DB_PATH = DATA_DIR / "thugsak.db"

KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
ESPN_SITE = "https://site.api.espn.com/apis/site/v2/sports"
ESPN_WEB = "https://site.web.api.espn.com/apis/common/v3/sports"
ESPN_CORE = "https://sports.core.api.espn.com/v2/sports"

# Kalshi's taker fee: ceil(fee_multiplier * 0.07 * contracts * P * (1-P)).
# The multiplier is read per-series from the API and must never be hardcoded --
# Kalshi uses 0 for fee-free series, so assuming 1 invents fees that don't exist.
FEE_RATE_TAKER = Decimal("0.07")
FEE_RATE_MAKER = Decimal("0.0175")

# Everything a person or Claude reads is rendered in this zone, and "today"
# means today here -- not in UTC, which has already rolled over by the time
# evening games tip off. Storage stays UTC; see `thugsak.localtime`.
LOCAL_TZ_NAME = "America/Chicago"

# Candlestick period_interval accepts ONLY these values. Anything else returns
# an empty list rather than an error, which is a silent failure mode.
CANDLE_INTERVALS = (1, 60, 1440)


@dataclass(frozen=True)
class League:
    key: str
    name: str
    espn_path: str          # e.g. "football/nfl"
    espn_core_path: str     # e.g. "football/leagues/nfl"
    kalshi_game: str        # winner market series
    kalshi_spread: str
    kalshi_total: str
    fpi_season: int         # season label for the power index endpoint
    power_stat: str         # "fpi" or "bpi" -- the net-points rating field
    power_complete: bool    # False when ESPN only covers a subset of teams
    # Typical points scored by one team; used only as a sanity bound, never as
    # a prediction input.
    sane_team_points: tuple[int, int]
    ratings_source: str = "espn"   # "espn" or "barttorvik"
    predictives_key: str = "stats"  # NFL/NCAAF use "predictives", NBA "stats"
    # ESPN publishes depth charts only for the pro leagues; the college
    # endpoints return 400. Without one, the injury model cannot identify a
    # replacement, so the starter-minus-backup signal is unavailable and the
    # adjustment says so rather than failing quietly.
    has_depth_chart: bool = True
    # College injury reporting is far sparser than the pro leagues'.
    reliable_injuries: bool = True
    # ESPN group ids that define Division I for a college league. Its /teams
    # endpoint returns every division -- 763 for football, which drags in D2
    # and D3 branch campuses. One of those ("Ohio State Newark Titans") shares
    # an abbreviation with Ohio State and displaced it from the team table.
    # Empty means the league needs no filter.
    d1_groups: tuple[int, ...] = ()
    # ESPN's college scoreboard returns only a *featured subset* unless a
    # conference group is named: 7 of 67 NCAAMB games, 20 of 130 NCAAF. The
    # omission is silent -- the response looks complete -- so this parameter
    # is the difference between most of the slate being visible and not.
    # 90 = all D1 football, 50 = D1 basketball. None for the pro leagues.
    scoreboard_group: int | None = None


LEAGUES: dict[str, League] = {
    "nfl": League(
        key="nfl",
        name="NFL",
        espn_path="football/nfl",
        espn_core_path="football/leagues/nfl",
        kalshi_game="KXNFLGAME",
        kalshi_spread="KXNFLSPREAD",
        kalshi_total="KXNFLTOTAL",
        fpi_season=2026,
        power_stat="fpi",
        power_complete=True,
        sane_team_points=(0, 70),
        predictives_key="predictives",
    ),
    "nba": League(
        key="nba",
        name="NBA",
        espn_path="basketball/nba",
        espn_core_path="basketball/leagues/nba",
        kalshi_game="KXNBAGAME",
        kalshi_spread="KXNBASPREAD",
        kalshi_total="KXNBATOTAL",
        # The 2026-27 NBA season is labelled 2027 by ESPN's power index.
        fpi_season=2027,
        power_stat="bpi",
        power_complete=True,
        sane_team_points=(60, 180),
        predictives_key="stats",
    ),
    "ncaaf": League(
        key="ncaaf",
        name="NCAA Football",
        espn_path="football/college-football",
        espn_core_path="football/leagues/college-football",
        kalshi_game="KXNCAAFGAME",
        kalshi_spread="KXNCAAFSPREAD",
        kalshi_total="KXNCAAFTOTAL",
        fpi_season=2026,
        power_stat="fpi",
        power_complete=True,
        sane_team_points=(0, 100),
        predictives_key="predictives",
        has_depth_chart=False,
        reliable_injuries=False,
        # 80 = FBS (148 teams), 81 = FCS (130). Together these are D1.
        d1_groups=(80, 81),
        # 90 covers FBS and FCS in one scoreboard call.
        scoreboard_group=90,
    ),
    "ncaamb": League(
        key="ncaamb",
        name="NCAA Men's Basketball",
        espn_path="basketball/mens-college-basketball",
        espn_core_path="basketball/leagues/mens-college-basketball",
        kalshi_game="KXNCAAMBGAME",
        kalshi_spread="KXNCAAMBSPREAD",
        kalshi_total="KXNCAAMBTOTAL",
        fpi_season=2026,
        power_stat="bpi",
        # ESPN's NCAAMB index returns only 25 teams, so Barttorvik supplies
        # ratings for the other ~340.
        power_complete=False,
        sane_team_points=(30, 140),
        ratings_source="barttorvik",
        predictives_key="stats",
        has_depth_chart=False,
        reliable_injuries=False,
        # 50 = D1 (366 teams). The plain /teams endpoint already returns 365
        # for this league, so the filter is a guard rather than a correction.
        d1_groups=(50,),
        scoreboard_group=50,
    ),
}

# Barttorvik labels the 2026-27 college basketball season 2027, same as the NBA.
BARTTORVIK_SEASON = 2027


def league(key: str) -> League:
    try:
        return LEAGUES[key.lower()]
    except KeyError:
        raise KeyError(f"unknown league {key!r}; known: {sorted(LEAGUES)}") from None


# Cache TTLs in seconds. Completed games are immutable and never re-fetched.
CACHE_TTL = {
    "kalshi_markets": 30,
    "kalshi_trades": 60,
    "kalshi_orderbook": 30,
    "kalshi_candles": 300,
    "kalshi_series": 21_600,
    "espn_scoreboard": 300,
    "espn_summary": 300,
    "espn_injuries": 900,
    "espn_gamelog": 3_600,
    "espn_schedule": 3_600,
    "espn_power": 21_600,
    "espn_depthchart": 43_200,
    "barttorvik": 21_600,
    "nflverse": 21_600,
}
