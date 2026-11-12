"""Central clock: UTC underneath, Central Time at every surface.

The split is deliberate and the two halves must not be confused:

**Stored and compared in UTC.** `games.start_time`, `predictions.as_of` and
the as-of cutoff are UTC ISO strings. The as-of guard works by comparing
timestamps, so they have to be unambiguous and monotonic -- a local-time
column would be non-monotonic across the autumn DST fold (01:30 CDT and
01:30 CST are an hour apart and sort equal), which is exactly the kind of
silent wrongness `model.asof` exists to prevent.

**Displayed and selected in Central Time.** Every time a person or Claude
reads is rendered here, and "today" means today in Central, not in UTC. That
distinction is not cosmetic: after 6pm CST the UTC date has already rolled
over, so a UTC-defaulted slate shows tomorrow's games while the person is
looking at tonight's. The same bug hides an evening slate entirely, because a
7pm CT tip-off is 01:00 UTC the next day.

`zoneinfo` needs the `tzdata` package on Windows -- there is no system tz
database to fall back on -- so it is a pinned dependency rather than an
implicit one. DST is handled by the tz database, never by a fixed -6 offset.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .config import LOCAL_TZ_NAME

LOCAL_TZ = ZoneInfo(LOCAL_TZ_NAME)

__all__ = [
    "LOCAL_TZ",
    "day_bounds_utc",
    "fmt_day",
    "fmt_datetime",
    "fmt_time",
    "local_date",
    "now_local",
    "parse_ts",
    "to_local",
    "today_local",
    "tz_abbrev",
]


def parse_ts(ts: str | datetime | int | float) -> datetime:
    """Parse any timestamp these sources emit into an aware UTC datetime.

    Handles the shapes seen in practice: ESPN's trailing 'Z', Kalshi's
    explicit offsets, our own naive strings (assumed UTC, which is what we
    write), and Kalshi candlestick epoch seconds.
    """
    if isinstance(ts, datetime):
        dt = ts
    elif isinstance(ts, (int, float)) and not isinstance(ts, bool):
        return datetime.fromtimestamp(float(ts), tz=timezone.utc)
    else:
        s = str(ts).strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def to_local(ts: str | datetime | int | float) -> datetime:
    """The same instant, in Central Time."""
    return parse_ts(ts).astimezone(LOCAL_TZ)


def now_local() -> datetime:
    return datetime.now(LOCAL_TZ)


def today_local() -> date:
    """Today in Central -- what the person means by "today"."""
    return now_local().date()


def local_date(ts: str | datetime | int | float) -> str:
    """The Central calendar date of an instant, as YYYY-MM-DD.

    This is the slate date: the date a person would say a game is on.
    """
    return to_local(ts).strftime("%Y-%m-%d")


def tz_abbrev(ts: str | datetime | int | float | None = None) -> str:
    """"CST" or "CDT" for the instant in question.

    Taken from the tz database rather than assumed, so a game played either
    side of a DST boundary is labelled correctly.
    """
    dt = to_local(ts) if ts is not None else now_local()
    return dt.tzname() or LOCAL_TZ_NAME


def _clock(dt: datetime) -> str:
    # %-I is not available on Windows, so strip the pad by hand.
    return f"{dt.strftime('%I:%M %p').lstrip('0')}"


def fmt_time(ts: str | datetime | int | float, *, tz: bool = True) -> str:
    """"7:00 PM CST" -- the clock time a person would read off a listing."""
    dt = to_local(ts)
    out = _clock(dt)
    return f"{out} {tz_abbrev(dt)}" if tz else out


def fmt_day(ts: str | datetime | int | float) -> str:
    """"Tue Nov 11"."""
    dt = to_local(ts)
    return f"{dt.strftime('%a %b')} {dt.day}"


def fmt_datetime(ts: str | datetime | int | float, *, tz: bool = True) -> str:
    """"Tue Nov 11, 7:00 PM CST"."""
    return f"{fmt_day(ts)}, {fmt_time(ts, tz=tz)}"


def day_bounds_utc(date_iso: str) -> tuple[str, str]:
    """The UTC half-open window [lo, hi) covering one Central calendar day.

    Returned at minute precision as `YYYY-MM-DDTHH:MM`, which is what makes
    them safe to compare against `substr(start_time,1,16)` in SQL: stored
    timestamps arrive in two shapes ('...T19:00Z' from ESPN and
    '...T19:00:00+00:00' from our own writes) that do *not* sort against each
    other lexicographically, while their first 16 characters do.

    Computed through the tz database, so the window is 24 hours on ordinary
    days, 23 in spring and 25 in autumn.
    """
    d = datetime.strptime(date_iso, "%Y-%m-%d").date()
    lo = datetime.combine(d, datetime.min.time(), tzinfo=LOCAL_TZ)
    hi = datetime.combine(d + timedelta(days=1), datetime.min.time(), tzinfo=LOCAL_TZ)
    fmt = "%Y-%m-%dT%H:%M"
    return (
        lo.astimezone(timezone.utc).strftime(fmt),
        hi.astimezone(timezone.utc).strftime(fmt),
    )
