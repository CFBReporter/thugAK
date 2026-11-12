"""Resolve Kalshi tickers onto ESPN games, and team names onto canonical ids.

Kalshi encodes a game in the ticker itself:

    KXNFLGAME-26NOV23CINWAS-WAS        winner, subject team WAS
    KXNFLSPREAD-26NOV16LACBAL-LAC8     LAC by more than 7.5
    KXNFLTOTAL-26NOV16LACBAL-60        over 59.5 combined points

The middle segment is ``YYMONDD`` plus the two team codes concatenated, and it
is **identical across the GAME, SPREAD and TOTAL series for the same game**.
That shared key is what links a game's three ladders together, and it is more
reliable than re-deriving the pairing from team names.

Splitting the concatenated codes is the awkward part: ``CINWAS`` is 3+3 but
``USFJOES`` is 3+4 and ``KENTEMU`` is 4+3, so length alone cannot separate
them. The winner market's suffix names one of the two teams outright, which
resolves the split exactly; totals markets carry no team code and are linked
through the shared middle segment instead.

Two further rules this module follows:

- Teams are matched as an unordered **pair**, with home/away taken from ESPN.
  Kalshi's ordering convention is not documented and is not worth trusting.
- A game that cannot be matched is reported as unmatched, never dropped.
  Silently discarding them would hide precisely the thin markets where the
  largest mispricings live.
"""

from __future__ import annotations

import re
import sqlite3
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Sequence

from . import store
from .config import LEAGUES, League, league as get_league

MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}

# "26NOV23CINWAS" and also "26NOV111445FENPAR", where a 4-digit kickoff time
# is interleaved before the team codes.
_EVENT_CODE = re.compile(
    r"^(?P<yy>\d{2})(?P<mon>[A-Z]{3})(?P<dd>\d{2})(?P<time>\d{4})?(?P<teams>[A-Z]+)$"
)
# Strike suffix: a team code, an optional threshold, or a bare number.
_SUFFIX = re.compile(r"^(?P<team>[A-Z]+)?(?P<num>\d+)?$")

# Thresholds are stated explicitly in market titles ("over 59.5 points
# scored?", "wins by over 7.5 points"). Reading the number from the title is
# safer than inferring it from the ticker's integer suffix.
_TITLE_NUM = re.compile(r"(?:over|by over|under)\s+(?P<n>\d+(?:\.\d+)?)", re.IGNORECASE)


@dataclass
class ParsedTicker:
    ticker: str
    series: str
    league: str | None
    kind: str                 # "game" | "spread" | "total" | "unknown"
    date: str                 # YYYY-MM-DD, the local date Kalshi encoded
    event_key: str            # the shared "26NOV16LACBAL" segment
    teams_blob: str
    team_code: str | None     # subject team, when the suffix names one
    threshold: float | None   # strike, read from the title when available
    event_ticker: str = ""

    @property
    def other_code(self) -> str | None:
        """The opponent's code, deduced from the blob and the subject team."""
        if not self.team_code or not self.teams_blob:
            return None
        b, t = self.teams_blob, self.team_code
        if b.startswith(t):
            return b[len(t):] or None
        if b.endswith(t):
            return b[: -len(t)] or None
        return None

    @property
    def codes(self) -> tuple[str, ...]:
        o = self.other_code
        return tuple(c for c in (self.team_code, o) if c)


def _series_league(series: str) -> tuple[str | None, str]:
    """Map a series ticker to (league_key, kind)."""
    for key, lg in LEAGUES.items():
        if series == lg.kalshi_game:
            return key, "game"
        if series == lg.kalshi_spread:
            return key, "spread"
        if series == lg.kalshi_total:
            return key, "total"
    return None, "unknown"


def parse_ticker(ticker: str, title: str = "") -> ParsedTicker | None:
    """Decompose a Kalshi market ticker. Returns None if it is not a game market."""
    parts = ticker.split("-")
    if len(parts) < 2:
        return None
    series, code = parts[0], parts[1]
    suffix = parts[2] if len(parts) > 2 else ""

    m = _EVENT_CODE.match(code)
    if not m:
        return None
    yy, mon, dd = m.group("yy"), m.group("mon").upper(), m.group("dd")
    if mon not in MONTHS:
        return None
    try:
        date = f"{2000 + int(yy):04d}-{MONTHS[mon]:02d}-{int(dd):02d}"
    except ValueError:
        return None

    league, kind = _series_league(series)
    sm = _SUFFIX.match(suffix) if suffix else None
    team_code = sm.group("team") if sm else None

    tn = _TITLE_NUM.search(title or "")
    threshold = float(tn.group("n")) if tn else None
    if threshold is None and sm and sm.group("num"):
        # Fall back to the ticker's integer, which sits half a point above the
        # actual line (suffix 60 == "over 59.5").
        threshold = float(sm.group("num")) - 0.5

    return ParsedTicker(
        ticker=ticker,
        series=series,
        league=league,
        kind=kind,
        date=date,
        event_key=code,
        teams_blob=m.group("teams"),
        team_code=team_code,
        threshold=threshold,
        event_ticker=f"{series}-{code}",
    )


# --------------------------------------------------------------------------
# alias table construction
# --------------------------------------------------------------------------

def _expand_st(s: str) -> str:
    """Disambiguate "St." -- it means Saint at the front and State elsewhere.

    College sources lean on this abbreviation heavily and inconsistently:
    Barttorvik writes "Alabama St." for Alabama State but "St. John's" for
    Saint John's. Getting it wrong silently merges two different schools.
    """
    s = re.sub(r"^st\.?\s+", "saint ", s)
    s = re.sub(r"\bst\.(?=\s|$)", "state", s)
    s = re.sub(r"\bst(?=\s|$)", "state", s)
    return s


def _fold_accents(s: str) -> str:
    """Strip diacritics so sources that disagree about them still match.

    ESPN writes "San Jose State" with an accent and Kalshi does not. Without
    folding, the accented character is stripped as punctuation and leaves
    "jos", which shares no token with "jose" -- so the two never match and
    the game silently has no markets.
    """
    return "".join(
        c for c in unicodedata.normalize("NFKD", s)
        if not unicodedata.combining(c)
    )


# A parenthetical qualifier: "Johnson & Wales (RI)", "Saint Mary's (CA)".
# Sources add them inconsistently to disambiguate same-named schools, so the
# qualifier is dropped rather than treated as part of the name -- kept, its
# token is simply absent from the other source's name and disqualifies an
# otherwise exact match.
_PARENTHETICAL = re.compile(r"\s*\([^)]*\)")


def _norm(name: str, *, strip_parens: bool = False) -> str:
    """Canonical form of a team name.

    `strip_parens` drops a qualifier like "(RI)". It is off by default
    because the qualifier is often the only thing distinguishing two schools
    -- "Miami (OH)" and "Miami" are different programmes, and folding them
    together silently merges them. It is the *loose* pass that drops it, and
    only after the strict pass has failed.
    """
    s = (name or "").strip().lower()
    s = _fold_accents(s)
    if strip_parens:
        s = _PARENTHETICAL.sub(" ", s)
    s = s.replace("&", " and ")
    for spelled_out, short in CITY_FORMS:
        s = s.replace(spelled_out, short)
    s = _expand_st(s)
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    # Collapse spaced-out initialisms so "N.C. State" -> "nc state" matches
    # ESPN's "NC State". Done after punctuation stripping, which is what
    # splits them apart in the first place.
    s = re.sub(r"\b(?:([a-z]) )+(?=[a-z]\b)", lambda m: m.group(0).replace(" ", ""), s)
    return re.sub(r"\s+", " ", s).strip()


# Divergences no normalisation rule can bridge, because the sources simply use
# different names for the same school. Keyed by league, then by the normalised
# foreign name. This is the alias table's maintenance burden, and it grows by
# inspection of the unresolved names an import reports.
NAME_OVERRIDES: dict[str, dict[str, str]] = {
    "ncaamb": {
        "southern california": "USC",
        "miami fl": "MIA",
        "miami florida": "MIA",
        "miami oh": "M-OH",
        "miami ohio": "M-OH",
        "connecticut": "CONN",
        "pittsburgh": "PITT",
        "nc state": "NCST",
        "north carolina state": "NCST",
        "ole miss": "MISS",
        "mississippi": "MISS",
        "central florida": "UCF",
        "louisiana lafayette": "ULL",
        "louisiana monroe": "ULM",
        "texas christian": "TCU",
        "brigham young": "BYU",
        "virginia commonwealth": "VCU",
        "saint josephs": "JOES",
        "st josephs": "JOES",
        "massachusetts": "MASS",
        "detroit mercy": "DET",
        "saint peters": "STPT",
        "cal state fullerton": "CSUF",
        "cal state northridge": "CSUN",
        "cal state bakersfield": "CSUB",
        "seattle": "SEA",
        "omaha": "OMA",
        "fort wayne": "PFW",
        "purdue fort wayne": "PFW",
        "arkansas pine bluff": "UAPB",
        "illinois chicago": "UIC",
        "nebraska omaha": "OMA",
        "st thomas": "STT",
        "saint thomas": "STT",
    },
    "ncaaf": {
        "southern california": "USC",
        "miami fl": "MIA",
        "miami oh": "M-OH",
        "connecticut": "CONN",
        "pittsburgh": "PITT",
        "nc state": "NCST",
        "ole miss": "MISS",
        "central florida": "UCF",
        "texas christian": "TCU",
        "brigham young": "BYU",
    },
}

# ESPN abbreviates a few schools in its own display name, so no rule can
# bridge the gap: its men's basketball entry for Appalachian State is "App
# State Mountaineers", and every other source spells it out.
NAME_OVERRIDES["ncaamb"]["appalachian state"] = "APP"

# The same schools play both college sports, so an override worked out for
# one applies to the other. Football's table was a near-empty subset of
# basketball's, which meant a name that resolved in one sport silently failed
# in the other. Sharing is safe because an override resolves through the
# alias table *within* a league: an abbreviation that does not exist there
# simply falls through to the normal matching.
_COLLEGE_SHARED = {**NAME_OVERRIDES["ncaaf"], **NAME_OVERRIDES["ncaamb"]}
for _lg in ("ncaaf", "ncaamb"):
    NAME_OVERRIDES[_lg] = {**_COLLEGE_SHARED, **NAME_OVERRIDES[_lg]}
del _lg


# Abbreviations a particular source uses that differ from ESPN's, seeded as
# aliases *scoped to that source*. The scoping matters: nflverse writes "LA"
# for the Rams, but "LA" on its own is a city two NFL teams share, so
# claiming it globally would be the Giants/Jets mistake in another costume.
# `store.resolve_alias` prefers a source-scoped row and falls back to the
# general table, so this disambiguates without narrowing anything else.
#
# Relocated franchises map to their current team so a club's history stays
# continuous across the move rather than splitting in two.
SOURCE_TEAM_CODES: dict[tuple[str, str], dict[str, str]] = {
    ("nflverse", "nfl"): {
        "LA": "LAR",     # the Rams; "LAC" is spelled out for the Chargers
        "WAS": "WSH",
        "STL": "LAR",    # St. Louis Rams, pre-2016
        "SD": "LAC",     # San Diego Chargers, pre-2017
        "OAK": "LV",     # Oakland Raiders, pre-2020
    },
}


def seed_source_codes(conn: sqlite3.Connection, source: str, league: str) -> int:
    """Register a source's own team abbreviations before importing from it.

    Without this an import silently *drops* every game whose teams it cannot
    resolve. Washington and the Rams ended up with one stored game each out
    of three seasons, so the model had no history for them at all and simply
    refused to predict their games -- with nothing on screen to say why.
    """
    codes = SOURCE_TEAM_CODES.get((source, league), {})
    seeded = 0
    for code, canonical in codes.items():
        tid = store.resolve_alias(conn, league, canonical)
        if tid:
            # Private to this source; see store.SCOPED_PREFIX.
            store.add_alias(
                conn, league, code, store.SCOPED_PREFIX + source, tid
            )
            seeded += 1
    return seeded


# City names one source spells out and another abbreviates. Applied to both
# sides, so it cannot introduce a mismatch -- only remove one. Contracting
# rather than expanding is deliberate: "los angeles" -> "la" leaves
# "Louisiana Tech" untouched, where expanding "la" would have mangled it.
#
# ESPN calls the Clippers "LA Clippers" while every other source says Los
# Angeles, so without this the team simply never matched.
CITY_FORMS: tuple[tuple[str, str], ...] = (
    ("los angeles", "la"),
    ("new york city", "nyc"),
)


# Noise words that stop a name from matching across sources.
# "state" is deliberately NOT here: it distinguishes Alabama from Alabama
# State, which are different schools that would otherwise collide.
_DROP = {
    "university", "univ", "the", "of", "college",
    "fc", "sc", "mens", "womens",
}


def name_tokens(name: str, *, loose: bool = True) -> frozenset[str]:
    """Comparable words in a name.

    Two strengths, because the words in `_DROP` and a parenthetical
    qualifier are *optional* rather than noise. Dropped unconditionally they
    collapse "Boston College" and "Boston University" onto the same single
    token, and the resolver then cannot tell two real schools apart. So a
    strict pass keeps them and runs first; the loose pass exists for the
    sources that omit them.
    """
    if loose:
        return frozenset(
            t for t in _norm(name, strip_parens=True).split()
            if t and t not in _DROP
        )
    return frozenset(t for t in _norm(name).split() if t)


def register_espn_team(
    conn: sqlite3.Connection, league: str, team: Any, *, is_d1: bool | None = None
) -> str:
    """Create the canonical team record from ESPN and seed its aliases.

    ESPN's abbreviation is the canonical key because ESPN is the only source
    covering all four leagues. Every name variant ESPN exposes is registered,
    `location` most importantly: it is the mascot-free school name that other
    sources actually use.
    """
    abbrev = (getattr(team, "abbrev", "") or "").upper()
    display = getattr(team, "display_name", "") or abbrev
    espn_id = str(getattr(team, "espn_id", "") or "")
    location = getattr(team, "location", "") or ""
    nickname = getattr(team, "nickname", "") or ""
    short = getattr(team, "short_name", "") or ""

    canonical = abbrev or _norm(display).replace(" ", "")

    # Abbreviations are not unique in college sports: among 763 NCAAF teams,
    # "Ohio State Buckeyes" and "Ohio State Newark Titans" both abbreviate to
    # OSU. Keyed on the abbreviation alone, the second silently overwrote the
    # first, so a top-25 programme was replaced in the table by a Division III
    # branch campus. The ESPN id disambiguates, and only the loser of a
    # collision carries the suffix so the common case stays readable (nfl:BAL,
    # not nfl:33).
    clash = conn.execute(
        "SELECT espn_id FROM teams WHERE league=? AND canonical=?",
        (league, canonical),
    ).fetchone()
    if clash and espn_id and str(clash["espn_id"] or "") not in ("", espn_id):
        canonical = f"{canonical}-{espn_id}"

    tid = store.upsert_team(conn, league, canonical, display, espn_id, is_d1=is_d1)

    # Keys this registration has deliberately voided as ambiguous. Without
    # it the two spellings of one alias undo each other: the raw form
    # ("New York") deletes the contested key, and the normalised form
    # ("new york") then finds it free and re-adds it to *this* team -- so
    # the alias simply changed owner instead of being retired.
    voided: set[str] = set()

    for alias in (abbrev, display, location, nickname, short):
        if not alias:
            continue
        for form in (alias, _norm(alias)):
            key = form.strip().lower()
            if key in voided:
                continue
            holder = store.resolve_alias(conn, league, form, "espn")
            if holder and holder != tid:
                if _settle_contested_alias(
                    conn, league, form, tid, holder, espn_id, is_d1
                ):
                    voided.add(key)
                continue
            store.add_alias(conn, league, form, "espn", tid)
    if espn_id:
        store.add_alias(conn, league, espn_id, "espn_id", tid)
    return tid


def _settle_contested_alias(
    conn: sqlite3.Connection,
    league: str,
    form: str,
    tid: str,
    holder: str,
    espn_id: str,
    is_d1: bool | None,
) -> bool:
    """Decide who keeps an alias two teams both answer to.

    Returns True when the alias was retired as ambiguous.

    Two different situations wear the same shape, and treating them alike
    gets one of them badly wrong:

    **Unequal claimants.** "Ohio State" fits a top-25 programme and a
    Division III branch campus. There is a right answer, and it is the D1
    side; failing that, ESPN issues ids roughly in order of establishment, so
    the lower id is the long-standing programme a person means.

    **Equal claimants.** "New York" fits the Giants and the Jets exactly as
    well. There is *no* right answer, and picking one silently is how a bet
    lands on the wrong team -- this alias really was bound to the Giants.
    The alias is therefore deleted, and the market surfaces as unmatched,
    which is visible and fixable in a way that a wrong team is not.
    """
    key = form.strip().lower()
    other = conn.execute(
        "SELECT espn_id, is_d1 FROM teams WHERE team_id=?", (holder,)
    ).fetchone()
    if other is None:
        return False

    mine_d1, theirs_d1 = is_d1, other["is_d1"]
    if mine_d1 is not None and theirs_d1 is not None and mine_d1 != bool(theirs_d1):
        if mine_d1:
            conn.execute(
                "UPDATE team_aliases SET team_id=? "
                "WHERE league=? AND alias=? AND source=?",
                (tid, league, key, "espn"),
            )
        return False

    # Equal standing: let seniority decide only when the ids say so, and
    # otherwise refuse to guess.
    try:
        mine, theirs = int(espn_id), int(other["espn_id"])
    except (TypeError, ValueError):
        return False
    if mine == theirs:
        return False

    # Both are current, equally-ranked teams, so the alias names neither.
    conn.execute(
        "DELETE FROM team_aliases WHERE league=? AND alias=?", (league, key)
    )
    return True


def drop_ambiguous_aliases(conn: sqlite3.Connection, league: str) -> list[str]:
    """Remove aliases that name two teams with equal right.

    Deliberately narrow, and the narrowness is the point. A first attempt
    dropped any alias that was a token-subset of two or more team names,
    which deleted 208 perfectly good college aliases: "Alabama" is a subset
    of both "Alabama Crimson Tide" and "Alabama State Hornets", yet it
    plainly means the former -- the other school's extra token is part of
    its *name*, not a mascot.

    The real hazard is narrower: two current teams sharing a bare location,
    as the Giants and Jets share "New York". Then the alias describes each
    exactly as well, and whichever registered first silently won -- which is
    how a bet lands on the wrong team. So the test is that the alias plus a
    single distinguishing word yields two *equally ranked* teams.

    Equal ranking matters as much as the word count. "Oklahoma State" also
    fits "Oklahoma Panhandle PANHANDLE ST" -- a Division II school whose
    ESPN name is mangled into tokens that happen to qualify -- and "Charlotte"
    fits both the 49ers and a Division II Charlotte Saints. In those cases
    there is a right answer and it is the D1 programme, so the alias stays.
    """
    # Strict tokens throughout: the loose form drops "college" and
    # "university", which collapses Boston College and Boston University
    # onto one token and makes two distinct schools look like one ambiguity.
    teams = list(store.all_teams(conn, league))
    by_tokens: dict[frozenset[str], set[bool | None]] = {}
    for t in teams:
        toks = name_tokens(t["display_name"] or "", loose=False)
        if toks:
            rank = None if t["is_d1"] is None else bool(t["is_d1"])
            by_tokens.setdefault(frozenset(toks), set()).add(rank)

    dropped: list[str] = []
    for r in conn.execute(
        "SELECT DISTINCT alias FROM team_aliases WHERE league=?", (league,)
    ).fetchall():
        alias = r["alias"]
        if alias.isdigit():
            continue
        want = name_tokens(alias, loose=False)
        if not want:
            continue
        # Distinct team names that are this alias plus exactly one more word.
        # Counted by token set rather than by team id, so one school stored
        # under two ids does not read as two rival claimants.
        fits = [
            (toks, ranks) for toks, ranks in by_tokens.items()
            if want < toks and len(toks - want) == 1
        ]
        # Only the top tier contests the alias: a D1 claimant beats every
        # lower-division one outright.
        if any(True in ranks for _toks, ranks in fits):
            fits = [f for f in fits if True in f[1]]
        exact_fits = [toks for toks, _ranks in fits]
        if len(exact_fits) > 1:
            conn.execute(
                "DELETE FROM team_aliases WHERE league=? AND alias=?",
                (league, alias),
            )
            dropped.append(f"{league}:{alias} ({len(exact_fits)} teams)")
    return dropped


def resolve_or_register(
    conn: sqlite3.Connection, league: str, team: Any, *, source: str = "espn"
) -> str | None:
    """Resolve an ESPN team, registering it as non-D1 if it is unknown.

    College team lists cover Division I only -- ESPN returns 365 basketball
    teams -- so a D1 side playing a Division II or III opponent has a game
    whose other half cannot be resolved, and the whole fixture gets dropped.
    Those games are wanted, so an unknown opponent is admitted here and
    flagged `is_d1 = 0`: it keeps the game while leaving the slate itself D1.
    """
    name = getattr(team, "abbrev", "") or getattr(team, "display_name", "")
    tid = resolve_team(conn, league, name, source=source)
    if tid:
        return tid
    for alt in (
        getattr(team, "display_name", ""),
        getattr(team, "location", ""),
        getattr(team, "short_name", ""),
    ):
        if alt:
            tid = resolve_team(conn, league, alt, source=source)
            if tid:
                return tid
    if not (getattr(team, "espn_id", "") or name):
        return None
    # Unknown to the D1 list, so record it as a lower-division opponent.
    return register_espn_team(conn, league, team, is_d1=False)


def link_alias(
    conn: sqlite3.Connection, league: str, alias: str, source: str, team_id: str
) -> None:
    store.add_alias(conn, league, alias, source, team_id)
    store.add_alias(conn, league, _norm(alias), source, team_id)


def resolve_team(
    conn: sqlite3.Connection,
    league: str,
    name: str,
    *,
    source: str | None = None,
    fuzzy: bool = True,
) -> str | None:
    """Canonical team_id for any name, abbreviation or ESPN id.

    Exact alias lookup first, then a token-overlap fallback, which is what
    bridges 'Saint Mary's' / 'Saint Marys' / 'STMARYS' across sources.
    """
    if not name:
        return None
    hit = store.resolve_alias(conn, league, name, source)
    if hit:
        return hit
    normed = _norm(name)
    hit = store.resolve_alias(conn, league, normed, source)
    if hit:
        return hit

    # Known cross-source divergences, before any fuzzy guessing.
    override = NAME_OVERRIDES.get(league, {}).get(normed)
    if override:
        hit = store.resolve_alias(conn, league, override)
        if hit:
            store.add_alias(conn, league, normed, source or "override", hit)
            return hit

    if not fuzzy:
        return None

    # Strictest first. Each pass refuses to guess between equals, so a name
    # that genuinely fits two teams falls through to None rather than being
    # assigned to whichever sorted first.
    for loose in (False, True):
        hit = _fuzzy_resolve(conn, league, name, loose=loose)
        if hit:
            return hit
    return None


def _fuzzy_resolve(
    conn: sqlite3.Connection, league: str, name: str, *, loose: bool
) -> str | None:
    """Best team for a name by token containment, or None if ambiguous.

    Containment, not Jaccard. Other sources give the school name ("Akron")
    while ESPN adds a mascot ("Akron Zips"), so scoring by overlap with the
    *union* penalises a correct match for the mascot it could never have
    supplied. What matters is whether the queried words are all present.

    Extra candidate tokens then break ties, so "Alabama" prefers "Alabama
    Crimson Tide" over "Alabama State Hornets" -- the latter carries a
    significant extra word ("state") that the query did not have.
    """
    normed = _norm(name, strip_parens=loose)

    # Kalshi truncates a nickname to its initial once a city fields two
    # teams: "New York G", "New York J", "Los Angeles C". The initial is the
    # only thing distinguishing them, so it has to be matched as a prefix
    # rather than as a word -- as a word it is simply missing from the
    # candidate and disqualifies the correct team.
    parts = normed.split()
    initial: str | None = None
    if len(parts) >= 2 and len(parts[-1]) == 1:
        initial = parts[-1]
        want = frozenset(parts[:-1]) - (_DROP if loose else frozenset())
    else:
        want = name_tokens(name, loose=loose)
    if not want:
        return None

    # (coverage, fewer extra words, division rank, team_id). Division rank
    # sits in the key because a D1 programme beats a lower-division namesake
    # outright: "Oklahoma State" also fits a Division II school whose ESPN
    # name is mangled to "Oklahoma Panhandle PANHANDLE ST", and "Charlotte"
    # fits both the 49ers and a Division II Charlotte Saints. Without the
    # rank those tie, and a tie is refused -- so two real teams stopped
    # resolving at all.
    best: tuple[float, int, int, str] | None = None
    runner_up: tuple[float, int, int, str] | None = None
    for t in store.all_teams(conn, league):
        # NULL means the division does not apply (the pro leagues), which
        # ranks with D1 rather than below it.
        rank = 1 if t["is_d1"] is None else int(bool(t["is_d1"]))
        for cand in (t["display_name"] or "", t["canonical"] or ""):
            have = name_tokens(cand, loose=loose)
            if not have:
                continue
            inter = len(want & have)
            if not inter:
                continue
            coverage = inter / len(want)
            # Words in the query that the candidate lacks are disqualifying:
            # "Alabama State" must not fall back to "Alabama".
            if want - have:
                continue
            extra_tokens = have - want
            if initial and not any(
                tok.startswith(initial) for tok in extra_tokens
            ):
                # "New York G" must not match the Jets.
                continue
            key = (coverage, -len(extra_tokens), rank, t["team_id"])
            if best is None or key > best:
                if best is not None and best[3] != t["team_id"]:
                    runner_up = best
                best = key
            elif (
                (runner_up is None or key > runner_up)
                and t["team_id"] != best[3]
            ):
                runner_up = key

    if best is None:
        return None
    coverage, neg_extra, rank, team_id = best

    # A tie is an ambiguity, not a winner. "New York" fits both the Giants
    # and the Jets equally well, and breaking the tie on team_id silently
    # picked whichever sorted first -- which is how a bet ends up on the
    # wrong team. Returning nothing surfaces the market as unmatched
    # instead, and unmatched is visible.
    if runner_up is not None and (coverage, neg_extra, rank) == (
        runner_up[0], runner_up[1], runner_up[2]
    ):
        return None

    # Every queried word matched; reject only when the candidate is so much
    # longer that the match is probably coincidental.
    return team_id if coverage >= 0.99 and -neg_extra <= 3 else None


# --------------------------------------------------------------------------
# game matching
# --------------------------------------------------------------------------

@dataclass
class MatchResult:
    parsed: ParsedTicker
    game_id: str | None = None
    home_team: str | None = None
    away_team: str | None = None
    subject_team: str | None = None
    confidence: str = "none"      # exact | fuzzy | none
    reason: str = ""

    @property
    def matched(self) -> bool:
        return self.game_id is not None


def extract_names_from_rules(rules: str) -> tuple[str, str] | None:
    """Pull the two full team names out of a market's rules text.

    `rules_secondary` reads like 'the team who wins the Cincinnati vs
    Washington Pro Football game', which is the only place Kalshi spells the
    names out -- the ticker only has abbreviations.
    """
    if not rules:
        return None
    m = re.search(
        r"\b(?:the\s+)?(?P<a>[A-Z][\w.'&-]*(?:\s+[A-Z][\w.'&-]*){0,3})"
        r"\s+(?:vs\.?|v\.|at|@)\s+"
        r"(?P<b>[A-Z][\w.'&-]*(?:\s+[A-Z][\w.'&-]*){0,3})\b",
        rules,
    )
    if not m:
        return None
    return m.group("a").strip(), m.group("b").strip()


# What Kalshi appends to a team name to make a market title. Stripped so the
# title can be used as a name: "Kansas City wins" resolves to nothing, while
# "Kansas City" resolves immediately.
_TITLE_SUFFIX = re.compile(
    r"\s*(?:wins?|to\s+win|wins\s+the\s+game)\s*$", re.IGNORECASE
)


def team_from_title(title: str) -> str:
    """The team name inside a winner market's title.

    Kalshi's titles are the only place a readable team name appears for most
    college markets: `rules_secondary` carries no 'A vs B' phrasing for them,
    so the ticker abbreviations were the sole source and those agree with
    nobody. The title is 'Oklahoma St. wins', which normalises cleanly.
    """
    return _TITLE_SUFFIX.sub("", (title or "").strip()).strip()


def match_market(
    conn: sqlite3.Connection,
    ticker: str,
    *,
    title: str = "",
    rules_secondary: str = "",
    league_hint: str | None = None,
    date_tolerance_days: int = 2,
    event_titles: Sequence[str] = (),
) -> MatchResult:
    """Resolve one Kalshi market to a stored game.

    Date tolerance survives because a ticker's date is the scheduled local
    date as Kalshi reckons it, which is not always the Central date we file
    the game under -- a late West Coast tip-off can differ by a day either
    way, and Kalshi occasionally reschedules without reissuing the ticker.
    """
    parsed = parse_ticker(ticker, title)
    if parsed is None:
        return MatchResult(
            parsed=ParsedTicker(ticker, "", None, "unknown", "", "", "", None, None),
            reason="ticker did not parse as a game market",
        )
    league = parsed.league or league_hint
    if not league:
        return MatchResult(parsed, reason=f"unknown series {parsed.series!r}")

    # Resolution sources in order of how well they are spelled out.
    #
    # The ticker's abbreviations come last on purpose: they are Kalshi's own
    # and agree with nobody. For most college markets `rules_secondary`
    # carries no 'A vs B' phrasing either, which left the codes as the only
    # source and failed 11 of 29 college basketball games on one evening --
    # while every one of those markets had a perfectly readable title
    # ("Oklahoma St. wins", "Bowling Green wins") that nothing looked at.
    candidates: list[tuple[str, str]] = []
    names = extract_names_from_rules(rules_secondary)
    if names:
        candidates.append(names)

    # The event's two winner markets name one team each; together they name
    # the game.
    titled = [team_from_title(t) for t in event_titles if t]
    titled = [t for t in titled if t]
    if len(titled) >= 2:
        candidates.append((titled[0], titled[1]))

    codes = parsed.codes
    if len(codes) == 2:
        candidates.append((codes[0], codes[1]))

    resolved: tuple[str, str] | None = None
    confidence = "none"
    for a_name, b_name in candidates:
        a = resolve_team(conn, league, a_name, source="kalshi")
        b = resolve_team(conn, league, b_name, source="kalshi")
        if a and b and a != b:
            resolved = (a, b)
            confidence = "exact" if names and (a_name, b_name) == names else "fuzzy"
            break
    if not resolved:
        return MatchResult(
            parsed,
            reason=f"could not resolve both teams from {candidates or 'no candidates'}",
        )

    row = _find_game(conn, league, parsed.date, resolved, date_tolerance_days)
    if row is None:
        return MatchResult(
            parsed,
            reason=f"no stored game on {parsed.date} +/-{date_tolerance_days}d "
                   f"for {resolved}",
        )

    subject = None
    if parsed.team_code:
        subject = resolve_team(conn, league, parsed.team_code, source="kalshi")
    if subject is None and title:
        # The code is unknown but this market's own title names the team.
        # Without this the alias learning below could only ever learn codes
        # it already knew, which is why Kalshi's abbreviations never became
        # resolvable no matter how many times a game matched.
        named = resolve_team(conn, league, team_from_title(title), source="kalshi")
        if named in (row["home_team"], row["away_team"]):
            subject = named

    # Learn the mapping so the next lookup is an exact hit, here and for
    # every spread and total rung on the same event.
    if parsed.team_code and subject:
        link_alias(conn, league, parsed.team_code, "kalshi", subject)
    other = parsed.other_code
    if other and subject:
        opp = row["away_team"] if subject == row["home_team"] else row["home_team"]
        link_alias(conn, league, other, "kalshi", opp)

    return MatchResult(
        parsed=parsed,
        game_id=row["game_id"],
        home_team=row["home_team"],
        away_team=row["away_team"],
        subject_team=subject,
        confidence=confidence,
    )


def _find_game(
    conn: sqlite3.Connection,
    league: str,
    date: str,
    teams: tuple[str, str],
    tolerance_days: int,
) -> sqlite3.Row | None:
    """Find a game by date window and unordered team pair.

    Compared against `slate_date` -- the Central calendar date -- rather than
    the UTC date of `start_time`, because the ticker's date is a local one.
    Matching local to local makes the common case an exact hit instead of a
    one-day-off tolerance hit, and the window below then only has to absorb
    genuine disagreements. Falls back to the UTC date for any row predating
    the slate_date backfill.
    """
    try:
        d0 = datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    lo = (d0 - timedelta(days=tolerance_days)).strftime("%Y-%m-%d")
    hi = (d0 + timedelta(days=tolerance_days)).strftime("%Y-%m-%d")
    a, b = teams
    return conn.execute(
        """SELECT * FROM games
           WHERE league=?
             AND COALESCE(slate_date, substr(start_time,1,10)) BETWEEN ? AND ?
             AND ((home_team=? AND away_team=?) OR (home_team=? AND away_team=?))
           ORDER BY abs(
                      julianday(COALESCE(slate_date, substr(start_time,1,10)))
                      - julianday(?)
                    )
           LIMIT 1""",
        (league, lo, hi, a, b, b, a, date),
    ).fetchone()


def group_by_event(
    markets: Iterable[Any],
) -> dict[str, list[Any]]:
    """Group markets by the shared middle segment of their ticker.

    This is what collects a game's winner market and its spread and total
    ladders into one bundle, across three different series tickers.
    """
    out: dict[str, list[Any]] = {}
    for m in markets:
        ticker = getattr(m, "ticker", None) or (m.get("ticker") if isinstance(m, dict) else None)
        if not ticker:
            continue
        p = parse_ticker(ticker)
        if p:
            out.setdefault(p.event_key, []).append(m)
    return out
