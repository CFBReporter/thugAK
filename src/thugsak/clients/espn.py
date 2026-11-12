"""ESPN client. Undocumented endpoints, free, no key.

Covers every stated analysis input: scores and schedules, DraftKings lines,
injuries with athlete ids, player game logs, depth charts, and FPI/BPI.

Three traps these endpoints set, all hit during research:

1. ``/teams/{id}/depthchart`` on the site API returns **HTTP 200 with an empty
   ``{}`` body**. It looks like it works and silently yields nothing. The core
   API's ``/seasons/{y}/teams/{id}/depthcharts`` is the real one.
2. A single player game log arrives roughly 40x larger than the stats it
   carries, padded with ``links`` blocks. Everything is projected to the fields
   actually used before it is cached or shown to a model.
3. The power-index payload nests its numbers under ``predictives`` for football
   and ``stats`` for basketball, and NBA's season is labelled by the season's
   *end* year. Both are declared per-league in ``config``.

Because these endpoints are undocumented and can change without notice,
parsing is defensive: a missing field yields None rather than an exception, so
one upstream change degrades a panel instead of breaking a game view.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

import httpx

from .. import cache
from ..config import CACHE_TTL, ESPN_CORE, ESPN_SITE, ESPN_WEB, League, league as get_league


def _num(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _int(v: Any) -> int | None:
    f = _num(v)
    return int(f) if f is not None else None


@dataclass
class TeamRef:
    espn_id: str
    abbrev: str
    display_name: str
    logo: str | None = None
    # `location` is the school/city without the mascot ("Alabama State"),
    # which is how Barttorvik and most other sources name college teams. It is
    # the highest-value alias for cross-source matching, since `displayName`
    # ("Alabama State Hornets") carries a mascot nobody else uses.
    location: str = ""
    nickname: str = ""
    short_name: str = ""

    @classmethod
    def parse(cls, t: dict[str, Any]) -> "TeamRef":
        return cls(
            espn_id=str(t.get("id", "")),
            abbrev=(t.get("abbreviation") or "").upper(),
            display_name=t.get("displayName") or t.get("name") or "",
            logo=t.get("logo"),
            location=t.get("location") or "",
            nickname=t.get("nickname") or "",
            short_name=t.get("shortDisplayName") or "",
        )


@dataclass
class BookLine:
    """A sportsbook's line. ESPN embeds DraftKings free of charge."""

    provider: str
    spread: float | None          # home-relative: negative = home favoured
    total: float | None
    home_ml: int | None
    away_ml: int | None

    @classmethod
    def parse(cls, o: dict[str, Any]) -> "BookLine":
        ml = o.get("moneyline") or {}

        def side(key: str) -> int | None:
            node = (ml.get(key) or {}).get("close") or (ml.get(key) or {}).get("open") or {}
            return _int(node.get("odds"))

        home_ml, away_ml = side("home"), side("away")
        if home_ml is None:
            home_ml = _int((o.get("homeTeamOdds") or {}).get("moneyLine"))
        if away_ml is None:
            away_ml = _int((o.get("awayTeamOdds") or {}).get("moneyLine"))
        return cls(
            provider=(o.get("provider") or {}).get("name", "unknown"),
            spread=_num(o.get("spread")),
            total=_num(o.get("overUnder")) or _num(o.get("total")),
            home_ml=home_ml,
            away_ml=away_ml,
        )


@dataclass
class Game:
    espn_id: str
    league: str
    start_time: str
    home: TeamRef
    away: TeamRef
    home_score: int | None = None
    away_score: int | None = None
    completed: bool = False
    neutral_site: bool = False
    season: int | None = None
    season_type: int | None = None
    week: int | None = None
    status: str = ""
    lines: list[BookLine] = field(default_factory=list)

    @property
    def best_line(self) -> BookLine | None:
        """Prefer a line that actually carries a moneyline."""
        for ln in self.lines:
            if ln.home_ml is not None:
                return ln
        return self.lines[0] if self.lines else None

    @property
    def total_points(self) -> int | None:
        if self.home_score is None or self.away_score is None:
            return None
        return self.home_score + self.away_score

    @classmethod
    def parse(cls, ev: dict[str, Any], league_key: str) -> "Game | None":
        comps = ev.get("competitions") or []
        if not comps:
            return None
        c = comps[0]
        home = away = None
        hs = as_ = None
        for comp in c.get("competitors") or []:
            tr = TeamRef.parse(comp.get("team") or {})
            sc = comp.get("score")
            if isinstance(sc, dict):
                sc = sc.get("displayValue") or sc.get("value")
            score = _int(sc)
            if comp.get("homeAway") == "home":
                home, hs = tr, score
            else:
                away, as_ = tr, score
        if home is None or away is None:
            return None

        st = (c.get("status") or ev.get("status") or {}).get("type") or {}
        lines = [BookLine.parse(o) for o in (c.get("odds") or [])]
        season = (ev.get("season") or {}).get("year") or (c.get("season") or {}).get("year")

        return cls(
            espn_id=str(ev.get("id", "")),
            league=league_key,
            start_time=c.get("date") or ev.get("date") or "",
            home=home,
            away=away,
            home_score=hs,
            away_score=as_,
            completed=bool(st.get("completed")),
            neutral_site=bool(c.get("neutralSite")),
            season=_int(season),
            season_type=_int((ev.get("season") or {}).get("type")),
            week=_int((ev.get("week") or {}).get("number")),
            status=st.get("name", "") or "",
            lines=lines,
        )


@dataclass
class Injury:
    athlete_id: str
    name: str
    position: str
    status: str            # Out | Questionable | Doubtful | Injured Reserve | Active
    detail: str = ""
    team_abbrev: str = ""

    # Probability the player is available. Drives the injury adjustment's
    # weighting; shown in the UI rather than applied invisibly.
    AVAILABILITY = {
        "out": 0.0,
        "injured reserve": 0.0,
        "suspension": 0.0,
        "doubtful": 0.15,
        "questionable": 0.60,
        "probable": 0.90,
        "active": 1.0,
        "day-to-day": 0.70,
    }

    @property
    def availability(self) -> float:
        return self.AVAILABILITY.get(self.status.strip().lower(), 0.75)

    @property
    def is_definitely_out(self) -> bool:
        return self.availability == 0.0

    @property
    def matters(self) -> bool:
        """Active players appear in these feeds too; they are not news."""
        return self.availability < 1.0


@dataclass
class PlayerGame:
    """One game from a player's log, projected to what the model uses."""

    event_id: str
    date: str
    opponent: str
    home_away: str         # "vs" | "@"
    result: str            # W | L | T
    stats: dict[str, float]

    @property
    def is_home(self) -> bool:
        return self.home_away == "vs"


@dataclass
class PowerRating:
    team_espn_id: str
    rating: float | None          # net points vs average opponent, neutral site
    rank: int | None = None
    offense: float | None = None
    defense: float | None = None


class EspnClient:
    def __init__(self, client: httpx.AsyncClient | None = None):
        self._client = client
        self._owns = client is None

    async def __aenter__(self) -> "EspnClient":
        if self._client is None:
            self._client = cache.make_client()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._owns and self._client is not None:
            await self._client.aclose()

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("use EspnClient as an async context manager")
        return self._client

    async def _get(
        self, url: str, params: dict[str, Any] | None = None, *, ttl: float | None = 300
    ) -> Any:
        res = await cache.fetch_json(self.client, url, params, ttl=ttl)
        return res.data

    # ---------------- slate and results ----------------

    async def scoreboard(
        self, league_key: str, *, date: str | None = None, limit: int = 400
    ) -> list[Game]:
        """Games for a date (YYYYMMDD) or today when omitted.

        Carries the embedded DraftKings spread/total/moneyline, so one request
        gives both the slate and the market benchmark.
        """
        lg = get_league(league_key)
        params: dict[str, Any] = {"limit": limit}
        if lg.scoreboard_group is not None:
            # Without this the college scoreboard silently returns a featured
            # subset -- 7 of 67 basketball games on a typical night.
            params["groups"] = lg.scoreboard_group
        if date:
            params["dates"] = date.replace("-", "")
        data = await self._get(
            f"{ESPN_SITE}/{lg.espn_path}/scoreboard",
            params,
            ttl=CACHE_TTL["espn_scoreboard"],
        )
        out = []
        for ev in data.get("events") or []:
            g = Game.parse(ev, league_key)
            if g:
                out.append(g)
        return out

    async def teams(self, league_key: str) -> list[TeamRef]:
        lg = get_league(league_key)
        data = await self._get(
            f"{ESPN_SITE}/{lg.espn_path}/teams",
            {"limit": 1000},
            ttl=CACHE_TTL["espn_schedule"],
        )
        out: list[TeamRef] = []
        for sport in data.get("sports") or []:
            for lea in sport.get("leagues") or []:
                for t in lea.get("teams") or []:
                    out.append(TeamRef.parse(t.get("team") or {}))
        return out

    async def d1_team_ids(self, league_key: str) -> set[str]:
        """ESPN ids of the league's Division I teams.

        The site /teams endpoint returns every division: 763 for college
        football, including D2 and D3 schools. That is not just noise -- a D3
        branch campus ("Ohio State Newark Titans") shares the OSU
        abbreviation with Ohio State and displaced it from the team table.
        The core API's conference groups give a clean D1 list instead.

        Returns an empty set for leagues that need no filter, which callers
        read as "no restriction".
        """
        lg = get_league(league_key)
        if not lg.d1_groups:
            return set()
        ids: set[str] = set()
        for group in lg.d1_groups:
            try:
                data = await self._get(
                    f"{ESPN_CORE}/{lg.espn_core_path}/seasons/{lg.fpi_season}"
                    f"/types/2/groups/{group}/teams",
                    {"limit": 500},
                    ttl=CACHE_TTL["espn_schedule"],
                )
            except Exception:
                continue
            for item in data.get("items") or []:
                ref = item.get("$ref") or ""
                tid = ref.rstrip("/").split("/")[-1].split("?")[0] if ref else ""
                if tid:
                    ids.add(tid)
        return ids

    async def team_schedule(
        self, league_key: str, team_espn_id: str, *, season: int | None = None
    ) -> list[Game]:
        """A team's full season game log. `season` reaches past years.

        This is the source for the last-7 window and the home/away splits, and
        the bulk history loader for NBA and NCAAF.
        """
        lg = get_league(league_key)
        params = {"season": season} if season else None
        data = await self._get(
            f"{ESPN_SITE}/{lg.espn_path}/teams/{team_espn_id}/schedule",
            params,
            ttl=CACHE_TTL["espn_schedule"],
        )
        out = []
        for ev in data.get("events") or []:
            g = Game.parse(ev, league_key)
            if g:
                if g.season is None and season:
                    g.season = season
                out.append(g)
        return out

    # ---------------- per-game detail ----------------

    async def summary(self, league_key: str, event_id: str) -> dict[str, Any]:
        """The richest single endpoint: injuries, lastFiveGames, predictor,
        againstTheSpread, standings, pickcenter, boxscore."""
        lg = get_league(league_key)
        return await self._get(
            f"{ESPN_SITE}/{lg.espn_path}/summary",
            {"event": event_id},
            ttl=CACHE_TTL["espn_summary"],
        )

    @staticmethod
    def parse_injuries(summary: dict[str, Any]) -> dict[str, list[Injury]]:
        """Injuries from a summary payload, keyed by team abbreviation."""
        out: dict[str, list[Injury]] = {}
        for block in summary.get("injuries") or []:
            abbrev = ((block.get("team") or {}).get("abbreviation") or "").upper()
            items: list[Injury] = []
            for i in block.get("injuries") or []:
                ath = i.get("athlete") or {}
                pos = ((ath.get("position") or {}) or {}).get("abbreviation") or ""
                det = i.get("details") or {}
                items.append(
                    Injury(
                        athlete_id=str(ath.get("id", "")),
                        name=ath.get("displayName") or "",
                        position=pos,
                        status=i.get("status") or "",
                        detail=(det.get("type") or i.get("longComment") or "") or "",
                        team_abbrev=abbrev,
                    )
                )
            out[abbrev] = items
        return out

    @staticmethod
    def parse_predictor(summary: dict[str, Any]) -> dict[str, float]:
        """ESPN's own matchup predictor, used only as a cross-check anchor."""
        p = summary.get("predictor") or {}
        out: dict[str, float] = {}
        for side in ("homeTeam", "awayTeam"):
            node = p.get(side) or {}
            v = _num(node.get("gameProjection"))
            if v is not None:
                out[side] = v / 100.0
        return out

    @staticmethod
    def parse_pickcenter(summary: dict[str, Any]) -> list[BookLine]:
        return [BookLine.parse(o) for o in (summary.get("pickcenter") or [])]

    @staticmethod
    def parse_last_games(summary: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
        """`lastFiveGames`, projected and keyed by team abbreviation."""
        out: dict[str, list[dict[str, Any]]] = {}
        for block in summary.get("lastFiveGames") or []:
            abbrev = ((block.get("team") or {}).get("abbreviation") or "").upper()
            games = []
            for e in block.get("events") or []:
                games.append(
                    {
                        "date": e.get("gameDate"),
                        "opponent": ((e.get("opponent") or {}).get("abbreviation") or ""),
                        "home_away": e.get("atVs"),
                        "result": e.get("gameResult"),
                        "home_score": _int(e.get("homeTeamScore")),
                        "away_score": _int(e.get("awayTeamScore")),
                    }
                )
            out[abbrev] = games
        return out

    async def injuries(self, league_key: str) -> dict[str, list[Injury]]:
        """League-wide injury report, keyed by team abbreviation."""
        lg = get_league(league_key)
        data = await self._get(
            f"{ESPN_SITE}/{lg.espn_path}/injuries", ttl=CACHE_TTL["espn_injuries"]
        )
        out: dict[str, list[Injury]] = {}
        for block in data.get("injuries") or []:
            abbrev = (block.get("abbreviation") or block.get("displayName") or "").upper()
            items = []
            for i in block.get("injuries") or []:
                ath = i.get("athlete") or {}
                items.append(
                    Injury(
                        athlete_id=str(ath.get("id", "")),
                        name=ath.get("displayName") or "",
                        position=((ath.get("position") or {}) or {}).get("abbreviation") or "",
                        status=i.get("status") or "",
                        team_abbrev=abbrev,
                    )
                )
            out[abbrev] = items
        return out

    # ---------------- player level ----------------

    async def player_gamelog(
        self, league_key: str, athlete_id: str, *, season: int | None = None
    ) -> list[PlayerGame]:
        """A player's per-game stats, projected to the fields that matter.

        The raw payload repeats a large ``links`` block per game; only the
        stat columns, opponent, venue and result are kept. Games the player
        missed simply do not appear, which is what makes the with/without
        split computable.
        """
        lg = get_league(league_key)
        params = {"season": season} if season else None
        data = await self._get(
            f"{ESPN_WEB}/{lg.espn_path}/athletes/{athlete_id}/gamelog",
            params,
            ttl=CACHE_TTL["espn_gamelog"],
        )
        names: list[str] = data.get("names") or []
        events: dict[str, Any] = data.get("events") or {}
        out: list[PlayerGame] = []

        for st in data.get("seasonTypes") or []:
            for cat in st.get("categories") or []:
                for e in cat.get("events") or []:
                    eid = str(e.get("eventId", ""))
                    meta = events.get(eid) or {}
                    raw = e.get("stats") or []
                    stats: dict[str, float] = {}
                    for i, nm in enumerate(names):
                        if i < len(raw):
                            v = _num(raw[i])
                            if v is not None:
                                stats[nm] = v
                    out.append(
                        PlayerGame(
                            event_id=eid,
                            date=(meta.get("gameDate") or "")[:19],
                            opponent=((meta.get("opponent") or {}).get("abbreviation") or ""),
                            home_away=meta.get("atVs") or "",
                            result=meta.get("gameResult") or "",
                            stats=stats,
                        )
                    )
        out.sort(key=lambda g: g.date, reverse=True)
        return out

    async def depth_chart(
        self, league_key: str, team_espn_id: str, *, season: int | None = None
    ) -> dict[str, list[str]]:
        """Position -> athlete ids ordered by depth (starter first).

        Uses the core API deliberately: the site API's depthchart path returns
        200 with an empty body, which looks like success.
        """
        lg = get_league(league_key)
        yr = season or lg.fpi_season
        data = await self._get(
            f"{ESPN_CORE}/{lg.espn_core_path}/seasons/{yr}/teams/{team_espn_id}/depthcharts",
            ttl=CACHE_TTL["espn_depthchart"],
        )
        out: dict[str, list[str]] = {}
        for item in data.get("items") or []:
            for pos, node in (item.get("positions") or {}).items():
                ranked = sorted(
                    node.get("athletes") or [],
                    key=lambda a: (a.get("rank") or 99),
                )
                ids = []
                for a in ranked:
                    # Athlete arrives as a $ref URL; take the id from the path
                    # rather than firing a request per player.
                    ref = (a.get("athlete") or {}).get("$ref") or ""
                    aid = ref.rstrip("/").split("/")[-1].split("?")[0] if ref else ""
                    if aid:
                        ids.append(aid)
                if ids:
                    out.setdefault(pos.lower(), []).extend(ids)
        return out

    async def backup_for(
        self, league_key: str, team_espn_id: str, position: str, starter_id: str
    ) -> str | None:
        """The next player at a position after the injured starter.

        Returns None for leagues with no published depth chart (ESPN serves
        these only for the pro leagues; college paths 400), so the caller can
        report the signal as unavailable instead of absent.
        """
        if not get_league(league_key).has_depth_chart:
            return None
        chart = await self.depth_chart(league_key, team_espn_id)
        for pos, ids in chart.items():
            if position.lower() not in pos:
                continue
            if starter_id in ids:
                i = ids.index(starter_id)
                if i + 1 < len(ids):
                    return ids[i + 1]
            elif ids:
                return ids[0]
        return None

    # ---------------- power index ----------------

    async def power_index(self, league_key: str) -> dict[str, PowerRating]:
        """FPI (football) or BPI (basketball) by ESPN team id.

        Present-day cross-check only -- never a model input, because the
        published history is an end-of-season snapshot. For NCAAMB this covers
        only the top 25, so Barttorvik supplies the rest.
        """
        lg = get_league(league_key)
        data = await self._get(
            f"{ESPN_CORE}/{lg.espn_core_path}/seasons/{lg.fpi_season}/powerindex",
            {"limit": 400},
            ttl=CACHE_TTL["espn_power"],
        )
        out: dict[str, PowerRating] = {}
        stat = lg.power_stat
        for item in data.get("items") or []:
            # Regular season only; the payload can carry several season types.
            if item.get("seasonType") not in (None, 2):
                continue
            ref = (item.get("team") or {}).get("$ref") or ""
            tid = ref.rstrip("/").split("/")[-1].split("?")[0] if ref else ""
            if not tid:
                continue
            vals = item.get(lg.predictives_key) or item.get("stats") or item.get("predictives") or []
            by_name = {s.get("name"): s for s in vals}

            def val(name: str) -> float | None:
                return _num((by_name.get(name) or {}).get("value"))

            out[tid] = PowerRating(
                team_espn_id=tid,
                rating=val(stat),
                rank=_int(val(f"{stat}rank")),
                offense=val(f"{stat}offense") or val("epaoffense"),
                defense=val(f"{stat}defense") or val("epadefense"),
            )
        return out
