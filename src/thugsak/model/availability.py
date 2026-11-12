"""Derived player availability: who has actually stopped playing.

Why this exists: **there is no usable college injury feed.** Verified against
the live endpoints --

    ESPN /injuries (NCAAF)        3 teams, 3 players, league-wide
    ESPN /injuries (NCAAMB)       empty
    ESPN summary (college)        no `injuries` block at all
    ESPN roster status (college)  every athlete reports "Active"

and the paid aggregators that do cover it are not free. The NCAA publishes
availability reports only for the championship tournaments.

So instead of asking *why* a player is absent, this asks whether they are
absent, by reading who appeared in the team's recent box scores. For betting
purposes availability is the question that matters; the diagnosis is only a
means to it. A guard who played 34 minutes a game and then vanished from the
last two box scores is unavailable, whatever the reason.

**This is backward-looking, and the output says so.** "Missed the last two
games" is evidence about the next game, not a report on it; a player can
return without warning. It is labelled as absence, never as injury.

It is also worth running for the NFL and NBA, where it cross-checks the injury
report: a player listed Questionable who has in fact not played in three weeks
is a different proposition from one who played last Sunday.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any, Sequence

from . import asof

# How many of the team's recent completed games to read.
DEFAULT_LOOKBACK = 4
# A player must have appeared in at least this many of the window's games to
# count as an established contributor; below that, an absence is unremarkable.
MIN_APPEARANCES = 2

# Absence only matters for players who were actually carrying load. Without a
# relative filter the report fills with fourth receivers who caught one pass a
# game, burying the one name that matters. Both tests must pass:
#   - inside the top N of the team by usage (a rotation, not a depth chart)
#   - at least this share of the team's total measured usage
TOP_CONTRIBUTORS = {"nba": 10, "ncaamb": 10, "nfl": 12, "ncaaf": 12}
MIN_USAGE_SHARE = 0.05

# Basketball box scores report minutes for everyone who dressed, so usage is
# measured directly. Football box scores list only players who recorded a
# stat, so usage is inferred from production and absence is only meaningful
# for the positions that accumulate stats.
MINUTES_LABELS = ("MIN",)
PRODUCTION_GROUPS = ("passing", "rushing", "receiving")


@dataclass
class PlayerSpell:
    """One player's appearance pattern across the recent window."""

    athlete_id: str
    name: str
    position: str = ""
    appearances: int = 0
    window: int = 0
    # Most recent games, newest first: True where the player appeared.
    pattern: list[bool] = field(default_factory=list)
    avg_usage: float = 0.0          # minutes, or a production proxy
    usage_unit: str = ""
    consecutive_missed: int = 0
    dressed_but_unused: int = 0     # listed with didNotPlay set
    usage_share: float = 0.0        # share of the team's measured usage
    usage_rank: int = 0             # 1 = the team's heaviest-used player
    # "absent"    the position's output fell with them -- a genuine loss
    # "replaced"  someone else absorbed the role, so output held up
    # "stand_in"  they were themselves covering for a returning starter
    kind: str = "absent"
    position_retained: float | None = None   # share of position usage kept

    @property
    def currently_absent(self) -> bool:
        return self.consecutive_missed >= 1

    @property
    def established(self) -> bool:
        return self.appearances >= MIN_APPEARANCES

    @property
    def confidence(self) -> str:
        """How much to trust 'absent' as a forecast for the next game.

        Two consecutive misses by an established contributor is a pattern; one
        miss could be rest, a coach's decision, or a one-game suspension.
        """
        if not self.established:
            return "none"
        if self.kind == "stand_in":
            return "stood in for a starter who has returned"
        if self.kind == "replaced":
            return "out, but the role has been absorbed"
        if self.consecutive_missed >= 2:
            return "likely out"
        if self.consecutive_missed == 1:
            return "possibly out"
        return "playing"

    def describe(self, unit: str = "") -> str:
        unit = unit or self.usage_unit
        played = "".join("x" if p else "." for p in self.pattern)
        return (
            f"{self.name} ({self.position or '?'}): missed last "
            f"{self.consecutive_missed} of {self.window} "
            f"[{played}], averaged {self.avg_usage:.1f} {unit} when playing"
        )


@dataclass
class AvailabilityReport:
    team_id: str
    league: str
    as_of: str
    games_read: int = 0
    absent: list[PlayerSpell] = field(default_factory=list)
    roster_usage: dict[str, float] = field(default_factory=dict)
    usage_unit: str = ""
    note: str = ""
    source: str = "derived from recent box scores"
    # Absences that were seen but judged immaterial, kept as a count so the
    # report cannot be mistaken for "nobody is missing".
    filtered_out: int = 0

    @property
    def total_usage_missing(self) -> float:
        """Combined usage of the absent contributors, in the same unit."""
        return sum(p.avg_usage for p in self.absent)

    @property
    def share_missing(self) -> float:
        """Absent usage as a share of the team's total.

        This is the figure that matters: two absent bench players are noise,
        while one absent player carrying a quarter of the minutes is not.
        """
        total = sum(self.roster_usage.values())
        return self.total_usage_missing / total if total else 0.0

    def summary(self) -> str:
        if not self.games_read:
            return "No recent box scores available to read."
        if not self.absent:
            return (
                f"No established contributor is missing from the last "
                f"{self.games_read} box scores."
            )
        bits = [p.describe(self.usage_unit) for p in self.absent[:4]]
        return (
            f"{len(self.absent)} established contributor(s) absent recently, "
            f"together {self.share_missing:.0%} of team {self.usage_unit}. "
            + " | ".join(bits)
        )


def _usage_from_basketball(group: dict[str, Any], stats: Sequence[str]) -> float:
    labels = group.get("labels") or []
    for i, lab in enumerate(labels):
        if lab in MINUTES_LABELS and i < len(stats):
            try:
                return float(stats[i])
            except (TypeError, ValueError):
                return 0.0
    return 0.0


def _usage_from_football(group_name: str, stats: Sequence[str]) -> float:
    """A rough production proxy for football.

    Attempts, carries and receptions are the closest thing to minutes that a
    football box score exposes, since it lists only players who recorded a
    stat. Crude by design -- it ranks contributors within one position group,
    nothing more.
    """
    if group_name not in PRODUCTION_GROUPS or not stats:
        return 0.0
    first = str(stats[0])
    if "/" in first:              # "20/39" completions/attempts
        first = first.split("/")[-1]
    try:
        return float(first)
    except (TypeError, ValueError):
        return 0.0


def parse_boxscore(summary: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Who appeared in this game, keyed by team abbreviation.

    Returns {abbrev: {athlete_id: {name, position, usage, dnp}}}. Basketball
    reports minutes for everyone who dressed; football only lists players who
    recorded a stat, so a lineman's absence is invisible there.
    """
    out: dict[str, dict[str, Any]] = {}
    for block in ((summary.get("boxscore") or {}).get("players") or []):
        abbrev = ((block.get("team") or {}).get("abbreviation") or "").upper()
        players: dict[str, Any] = {}
        for group in block.get("statistics") or []:
            gname = (group.get("name") or "").lower()
            for entry in group.get("athletes") or []:
                ath = entry.get("athlete") or {}
                aid = str(ath.get("id") or "")
                if not aid:
                    continue
                stats = entry.get("stats") or []
                dnp = bool(entry.get("didNotPlay"))
                usage = (
                    _usage_from_basketball(group, stats)
                    if any(lab in MINUTES_LABELS for lab in (group.get("labels") or []))
                    else _usage_from_football(gname, stats)
                )
                rec = players.setdefault(
                    aid,
                    {
                        "name": ath.get("displayName") or "",
                        "position": ((ath.get("position") or {}) or {}).get(
                            "abbreviation"
                        ) or "",
                        "usage": 0.0,
                        "dnp": dnp,
                    },
                )
                # A football player appears in several groups; keep the
                # largest single figure rather than summing incommensurable
                # stats.
                rec["usage"] = max(rec["usage"], usage)
                rec["dnp"] = rec["dnp"] and dnp
        if players:
            out[abbrev] = players
    return out


async def team_availability(
    client: Any,
    conn: sqlite3.Connection,
    league: str,
    team_id: str,
    espn_team_id: str,
    as_of: str,
    *,
    lookback: int = DEFAULT_LOOKBACK,
) -> AvailabilityReport:
    """Read the team's recent box scores and report who has stopped appearing.

    Uses the ESPN schedule to obtain event ids, because our own game ids come
    from whichever source supplied the history (nflverse or Barttorvik for
    two of the leagues) and carry no ESPN id.
    """
    from ..clients import espn as espn_mod

    asof.require_as_of(as_of)
    rep = AvailabilityReport(team_id=team_id, league=league, as_of=as_of)
    if not espn_team_id:
        rep.note = "No ESPN team id, so box scores cannot be located."
        return rep

    async with espn_mod.EspnClient(client) as ec:
        try:
            schedule = await ec.team_schedule(league, espn_team_id)
        except Exception as e:
            rep.note = f"Schedule unavailable: {type(e).__name__}"
            return rep

        # Completed games strictly before the cutoff, most recent first.
        past = [
            g for g in schedule
            if g.completed and g.start_time and g.start_time < as_of
        ]
        past.sort(key=lambda g: g.start_time, reverse=True)
        past = past[:lookback]

        # Box score entries omit position, so take it from the roster -- one
        # cheap cached request rather than one per player.
        roster_positions: dict[str, str] = {}
        try:
            rr = await ec._get(
                f"{espn_mod.ESPN_SITE}/{espn_mod.get_league(league).espn_path}"
                f"/teams/{espn_team_id}/roster",
                ttl=43_200,
            )
            groups = rr.get("athletes") or []
            flat = groups if (groups and "id" in groups[0]) else [
                a for g in groups for a in (g.get("items") or [])
            ]
            for a in flat:
                roster_positions[str(a.get("id") or "")] = (
                    ((a.get("position") or {}) or {}).get("abbreviation") or ""
                )
        except Exception:
            pass
        if not past:
            rep.note = "No completed games before the cutoff."
            return rep

        # abbrev -> which of this team's sides to read from each box score
        appearances: dict[str, list[tuple[float, bool]]] = {}
        meta: dict[str, dict[str, Any]] = {}
        order: list[set[str]] = []

        for g in past:
            try:
                summary = await ec.summary(league, g.espn_id)
            except Exception:
                continue
            boxes = parse_boxscore(summary)
            if not boxes:
                continue
            # Pick whichever side of this box score is our team.
            ours: dict[str, Any] | None = None
            for abbrev, players in boxes.items():
                tid = _resolve(conn, league, abbrev)
                if tid == team_id:
                    ours = players
                    break
            if ours is None:
                continue
            rep.games_read += 1
            order.append(set(ours))
            for aid, rec in ours.items():
                meta.setdefault(aid, rec)
                appearances.setdefault(aid, []).append(
                    (float(rec["usage"]), bool(rec["dnp"]))
                )

    if not rep.games_read:
        rep.note = "No readable box scores in the window."
        return rep

    rep.usage_unit = "minutes" if league in ("nba", "ncaamb") else "touches"

    spells: dict[str, PlayerSpell] = {}
    for aid, rec in meta.items():
        seen = appearances.get(aid, [])
        active = [u for u, dnp in seen if not dnp]
        spell = PlayerSpell(
            athlete_id=aid,
            name=rec["name"],
            position=rec["position"] or roster_positions.get(aid, ""),
            appearances=len(active),
            window=rep.games_read,
            usage_unit=rep.usage_unit,
            dressed_but_unused=sum(1 for _, dnp in seen if dnp),
        )
        spell.avg_usage = sum(active) / len(active) if active else 0.0
        # Newest game first, so a leading run of False is a current absence.
        spell.pattern = [aid in s for s in order]
        for appeared in spell.pattern:
            if appeared:
                break
            spell.consecutive_missed += 1
        rep.roster_usage[aid] = spell.avg_usage
        spells[aid] = spell

    # Rank the squad by usage, then keep only absences among the genuine
    # contributors. An absent fourth receiver with one catch a game is not a
    # finding, and listing them buries the name that is.
    total_usage = sum(rep.roster_usage.values()) or 1.0
    ranked = sorted(spells.values(), key=lambda p: -p.avg_usage)
    top_n = TOP_CONTRIBUTORS.get(league, 10)
    for rank, spell in enumerate(ranked):
        spell.usage_share = spell.avg_usage / total_usage
        spell.usage_rank = rank + 1
        if not (spell.currently_absent and spell.established):
            continue
        if rank >= top_n or spell.usage_share < MIN_USAGE_SHARE:
            rep.filtered_out += 1
            continue
        rep.absent.append(spell)

    _classify_absences(rep, spells, order, appearances)
    rep.absent.sort(key=lambda p: (p.kind != "absent", -p.avg_usage))
    if league in ("ncaaf", "nfl"):
        rep.note = (
            "Football box scores list only players who recorded a stat, so "
            "this detects absent ball-carriers and passers but cannot see "
            "linemen or most defenders."
        )
    return rep


def _classify_absences(
    rep: AvailabilityReport,
    spells: dict[str, PlayerSpell],
    order: list[set[str]],
    appearances: dict[str, list[tuple[float, bool]]],
) -> None:
    """Separate a genuine loss from a mere change of personnel.

    A backup quarterback who starts while the first choice is hurt racks up
    usage, then "disappears" the moment the starter returns. Read naively that
    is a 30%-usage player going missing, which is exactly backwards -- the team
    got stronger.

    The test that distinguishes them is whether the *position* kept producing.
    If the position's usage held up across the games the player missed,
    somebody absorbed the role and the team's output did not fall; if it
    dropped, the absence cost something. That is also the quantity the points
    model actually needs, rather than the player's own raw usage.
    """
    if not rep.absent or len(order) < 2:
        return

    # Which window games each player appeared in, newest first.
    by_pos: dict[str, list[PlayerSpell]] = {}
    for sp in spells.values():
        if sp.position:
            by_pos.setdefault(sp.position, []).append(sp)

    for sp in rep.absent:
        peers = [q for q in by_pos.get(sp.position, []) if q.athlete_id != sp.athlete_id]
        if not sp.position or not peers:
            continue

        missed_idx = [i for i, present in enumerate(sp.pattern) if not present]
        played_idx = [i for i, present in enumerate(sp.pattern) if present]
        if not missed_idx or not played_idx:
            continue

        def position_usage(indices: list[int]) -> float:
            """Total usage at this position across the given games."""
            total = 0.0
            for q in by_pos.get(sp.position, []):
                for i in indices:
                    if i < len(q.pattern) and q.pattern[i]:
                        total += q.avg_usage
            return total / max(len(indices), 1)

        with_them = position_usage(played_idx)
        without_them = position_usage(missed_idx)
        if with_them <= 0:
            continue
        retained = without_them / with_them
        sp.position_retained = round(retained, 2)

        # A peer who only played while this player was absent is a deputy;
        # one who played throughout and now plays more is the incumbent, which
        # makes this player the deputy.
        peer_took_over = any(
            q.avg_usage >= sp.avg_usage * 0.6
            and all(i < len(q.pattern) and q.pattern[i] for i in missed_idx)
            and not all(i < len(q.pattern) and q.pattern[i] for i in played_idx)
            for q in peers
        )
        if peer_took_over and retained >= 0.85:
            # The position is producing as before, and the player who took
            # over was not playing while this one was: this one was the fill-in.
            sp.kind = "stand_in"
        elif retained >= 0.85:
            sp.kind = "replaced"
        else:
            sp.kind = "absent"


def _resolve(conn: sqlite3.Connection, league: str, abbrev: str) -> str | None:
    from .. import matching

    return matching.resolve_team(conn, league, abbrev, source="espn")


def merge_with_injuries(
    rep: AvailabilityReport, injury_ids: set[str]
) -> dict[str, Any]:
    """Combine derived absence with whatever the injury report said.

    The interesting cases are the disagreements:

    - absent but not on the report -- which is the whole point for college,
      where no report exists;
    - on the report but still playing, where a Questionable tag has not
      actually kept the player off the floor.
    """
    unreported = [p for p in rep.absent if p.athlete_id not in injury_ids]
    confirmed = [p for p in rep.absent if p.athlete_id in injury_ids]
    return {
        "games_read": rep.games_read,
        "usage_unit": rep.usage_unit,
        "share_of_usage_missing": round(rep.share_missing, 4),
        "absent_and_unreported": [
            {
                "name": p.name,
                "position": p.position,
                "consecutive_missed": p.consecutive_missed,
                "avg_usage": round(p.avg_usage, 1),
                "confidence": p.confidence,
                "pattern_newest_first": p.pattern,
            }
            for p in unreported
        ],
        "absent_and_reported": [
            {"name": p.name, "consecutive_missed": p.consecutive_missed,
             "avg_usage": round(p.avg_usage, 1)}
            for p in confirmed
        ],
        "summary": rep.summary(),
        "note": rep.note,
        "caveat": (
            "Derived from who appeared in recent box scores, not from an "
            "injury report. It establishes that a player has been absent, "
            "which is evidence about the next game rather than a statement "
            "about it -- players return without notice."
        ),
    }
