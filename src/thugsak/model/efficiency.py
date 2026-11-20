"""Tempo-free efficiency ratings for college basketball, in the KenPom style.

The method is KenPom's, published and widely reproduced; the numbers are our
own, computed from stored results (KenPom's ratings are a paid service whose
data we do not use). For every team:

    AdjO  points scored per 100 possessions against an average defence
    AdjD  points allowed per 100 possessions against an average offence
    AdjT  possessions per 40 minutes against an average-tempo opponent

found by iterating: a game's raw efficiency is rescaled by how good the
opponent's matching unit is, and the team's rating is the weighted average
of those rescaled games. Home court is worth HOME_EFF on efficiency each way.
Recent games count more (half-life RECENCY_HALF_LIFE_DAYS), and every team
carries PRIOR_GAMES pseudo-games at its prior -- last season's rating pulled
halfway to average, or the average itself -- so a team three games into a
season is not judged on three games alone.

A projected score follows directly:

    possessions = AdjT_home * AdjT_away / avg_tempo
    home points = AdjO_home * AdjD_away / avg_eff * (1 + HOME_EFF) * possessions / 100
    away points = AdjO_away * AdjD_home / avg_eff * (1 - HOME_EFF) * possessions / 100

Strictly as-of: only games completed before the cutoff are read.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from .. import store
from ..localtime import parse_ts

HOME_EFF = 0.014              # ~1.4% efficiency edge each way at home
RECENCY_HALF_LIFE_DAYS = 60.0
PRIOR_GAMES = 4.0
PRIOR_CARRYOVER = 0.5         # share of last season's gap from average kept
ITERATIONS = 40
LEAGUES = ("ncaamb",)

_CACHE: dict[tuple[str, str, int, str], tuple[float, "Ratings | None"]] = {}
# Another process (the hourly jobs) may add finals; an hour bounds staleness.
CACHE_TTL = 3600.0


def _on_write(sql: str) -> None:
    if "games" in sql.lower():
        _CACHE.clear()


from .. import db as _db  # noqa: E402

_db.on_write(_on_write)


@dataclass
class Ratings:
    season: int
    cutoff: str
    avg_eff: float = 100.0
    avg_tempo: float = 68.0
    adj_o: dict[str, float] = field(default_factory=dict)
    adj_d: dict[str, float] = field(default_factory=dict)
    adj_t: dict[str, float] = field(default_factory=dict)
    games: dict[str, int] = field(default_factory=dict)
    n_games: int = 0
    # Strength of schedule: the average net rating (AdjO - AdjD) of the
    # opponents a team has played, in points per 100 possessions.
    sos: dict[str, float] = field(default_factory=dict)
    # National ranks (1 = fastest pace / toughest schedule / best unit).
    ranks: dict[str, dict[str, int]] = field(default_factory=dict)

    def has(self, team: str) -> bool:
        return team in self.adj_o

    def project(self, home: str, away: str, *, neutral: bool = False) -> dict[str, Any] | None:
        if not self.n_games:
            return None
        ho, hd, ht = (self.adj_o.get(home, self.avg_eff), self.adj_d.get(home, self.avg_eff),
                      self.adj_t.get(home, self.avg_tempo))
        ao, ad, at = (self.adj_o.get(away, self.avg_eff), self.adj_d.get(away, self.avg_eff),
                      self.adj_t.get(away, self.avg_tempo))
        h = 0.0 if neutral else HOME_EFF
        poss = ht * at / self.avg_tempo
        hp = ho * ad / self.avg_eff * (1 + h) * poss / 100
        ap = ao * hd / self.avg_eff * (1 - h) * poss / 100
        return {
            "home_points": round(hp, 2), "away_points": round(ap, 2),
            "possessions": round(poss, 1),
            "home": self.profile(home),
            "away": self.profile(away),
            "avg_tempo": round(self.avg_tempo, 1),
        }

    def profile(self, team: str) -> dict[str, Any]:
        """One team's ratings with national ranks, for display."""
        o = self.adj_o.get(team, self.avg_eff)
        d = self.adj_d.get(team, self.avg_eff)
        return {
            "adj_o": round(o, 1), "adj_d": round(d, 1), "net": round(o - d, 1),
            "adj_t": round(self.adj_t.get(team, self.avg_tempo), 1),
            "sos": round(self.sos.get(team, 0.0), 1),
            "games": self.games.get(team, 0),
            "ranks": self.ranks.get(team, {}),
            "of": len(self.ranks),
        }


def _solve(rows: list[Any], season: int, cutoff: str, prior: "Ratings | None") -> Ratings:
    r = Ratings(season=season, cutoff=cutoff)
    if not rows:
        return r
    end = parse_ts(cutoff)
    obs: list[tuple[str, str, float, float, float, float, float]] = []
    pts = poss_sum = 0.0
    for g in rows:
        poss = float(g["possessions"])
        hs, as_ = float(g["home_score"]), float(g["away_score"])
        h = 0.0 if g["neutral_site"] else HOME_EFF
        age = max(0.0, (end - parse_ts(g["start_time"])).total_seconds() / 86400.0)
        w = 0.5 ** (age / RECENCY_HALF_LIFE_DAYS)
        # Home offence is inflated by home court; remove it before rating.
        oe_home = hs / poss * 100 / (1 + h)
        oe_away = as_ / poss * 100 / (1 - h)
        obs.append((g["home_team"], g["away_team"], oe_home, oe_away, poss, w, 0.0))
        pts += hs + as_
        poss_sum += 2 * poss
    r.avg_eff = pts / poss_sum * 100
    r.avg_tempo = poss_sum / 2 / len(obs)
    r.n_games = len(obs)

    teams = {t for o in obs for t in o[:2]}
    for o in obs:
        r.games[o[0]] = r.games.get(o[0], 0) + 1
        r.games[o[1]] = r.games.get(o[1], 0) + 1

    def prior_of(t: str, table: str, avg: float) -> float:
        if prior and prior.n_games and t in getattr(prior, table):
            base = prior.avg_eff if table != "adj_t" else prior.avg_tempo
            gap = getattr(prior, table)[t] - base
            return avg + PRIOR_CARRYOVER * gap
        return avg

    p_o = {t: prior_of(t, "adj_o", r.avg_eff) for t in teams}
    p_d = {t: prior_of(t, "adj_d", r.avg_eff) for t in teams}
    p_t = {t: prior_of(t, "adj_t", r.avg_tempo) for t in teams}
    o, d, tt = dict(p_o), dict(p_d), dict(p_t)
    for _ in range(ITERATIONS):
        so = {t: PRIOR_GAMES * p_o[t] for t in teams}
        sd = {t: PRIOR_GAMES * p_d[t] for t in teams}
        st = {t: PRIOR_GAMES * p_t[t] for t in teams}
        sw = {t: PRIOR_GAMES for t in teams}
        for home, away, oe_h, oe_a, poss, w, _ in obs:
            so[home] += w * oe_h * r.avg_eff / d[away]
            sd[away] += w * oe_h * r.avg_eff / o[home]
            so[away] += w * oe_a * r.avg_eff / d[home]
            sd[home] += w * oe_a * r.avg_eff / o[away]
            st[home] += w * poss * r.avg_tempo / tt[away]
            st[away] += w * poss * r.avg_tempo / tt[home]
            sw[home] += w
            sw[away] += w
        new_o = {t: so[t] / sw[t] for t in teams}
        new_d = {t: sd[t] / sw[t] for t in teams}
        new_t = {t: st[t] / sw[t] for t in teams}
        delta = max(abs(new_o[t] - o[t]) + abs(new_d[t] - d[t]) for t in teams)
        o, d, tt = new_o, new_d, new_t
        if delta < 1e-4:
            break
    r.adj_o, r.adj_d, r.adj_t = o, d, tt

    # Strength of schedule, from the converged ratings: the mean net rating
    # of each team's opponents (every meeting counts, unweighted).
    opp_net: dict[str, list[float]] = {t: [] for t in teams}
    for home, away, *_ in obs:
        opp_net[home].append(o[away] - d[away])
        opp_net[away].append(o[home] - d[home])
    r.sos = {t: sum(v) / len(v) for t, v in opp_net.items() if v}

    def rank(values: dict[str, float], *, high_first: bool = True) -> dict[str, int]:
        order = sorted(values, key=lambda t: -values[t] if high_first else values[t])
        return {t: i + 1 for i, t in enumerate(order)}

    ranked = [t for t in teams if r.games.get(t, 0) > 0]
    r_o = rank({t: o[t] for t in ranked})
    r_d = rank({t: d[t] for t in ranked}, high_first=False)
    r_n = rank({t: o[t] - d[t] for t in ranked})
    r_t = rank({t: tt[t] for t in ranked})
    r_s = rank({t: r.sos.get(t, 0.0) for t in ranked})
    r.ranks = {t: {"off": r_o[t], "def": r_d[t], "net": r_n[t], "pace": r_t[t], "sos": r_s[t]}
               for t in ranked}
    return r


def _season_rows(conn: Any, league: str, season: int, cutoff: str) -> list[Any]:
    return conn.execute(
        """SELECT home_team, away_team, home_score, away_score, possessions,
                  neutral_site, start_time
             FROM games
            WHERE league=? AND season=? AND completed=1 AND possessions IS NOT NULL
              AND home_score IS NOT NULL AND start_time < ?""",
        (league, season, cutoff),
    ).fetchall()


def ratings_as_of(conn: Any, league: str, season: int, as_of: str) -> Ratings | None:
    """Ratings from games before the start (UTC) of `as_of`'s day.

    Cut at the day rather than the minute so one solve serves a whole slate,
    live and in replay alike; games earlier the same day are left out.
    """
    if league not in LEAGUES:
        return None
    day = parse_ts(as_of).replace(hour=0, minute=0, second=0, microsecond=0)
    cutoff = day.isoformat()
    key = (store.db_ident(conn), league, int(season), cutoff)
    import time as _time

    hit = _CACHE.get(key)
    if hit is not None and _time.monotonic() - hit[0] < CACHE_TTL:
        return hit[1]
    prior = _final_ratings(conn, league, int(season) - 1)
    r = _solve(_season_rows(conn, league, int(season), cutoff), int(season), cutoff, prior)
    if len(_CACHE) > 2000:
        _CACHE.clear()
    _CACHE[key] = (_time.monotonic(), r)
    return r


def _final_ratings(conn: Any, league: str, season: int) -> Ratings | None:
    """A completed season's end-of-season ratings (no prior of their own)."""
    import time as _time

    key = (store.db_ident(conn), league, season, "final")
    if key in _CACHE:
        return _CACHE[key][1]
    rows = _season_rows(conn, league, season, "9999-12-31")
    r = _solve(rows, season, (parse_ts(max(g["start_time"] for g in rows)) + timedelta(days=1)).isoformat()
               if rows else "9999-12-31", None) if rows else None
    _CACHE[key] = (_time.monotonic(), r)
    return r


def clear_cache() -> None:
    _CACHE.clear()
