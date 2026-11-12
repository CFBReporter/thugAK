"""thugsak -- pick a sport, see the day's games, click into one for a read.

Run with:

    .venv/Scripts/streamlit run app/streamlit_app.py

Past dates are allowed and are how accuracy gets checked: a completed game
renders exactly as it would have pre-kickoff, with the final score beside the
prediction.
"""

from __future__ import annotations

import asyncio
import queue
import sys
import threading
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

import streamlit as st

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from thugsak import store  # noqa: E402
from thugsak.config import DB_PATH, LEAGUES  # noqa: E402
from thugsak.localtime import fmt_time, today_local, tz_abbrev  # noqa: E402
from thugsak.mcp import tools  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from views import game_detail  # noqa: E402

st.set_page_config(page_title="thugsak", page_icon="📊", layout="wide")


# --------------------------------------------------------------------------
# async bridge
# --------------------------------------------------------------------------

def run_async(coro_factory: Callable[[], Any]) -> Any:
    """Run one coroutine to completion from Streamlit's synchronous flow.

    A fresh event loop per call, in a worker thread: Streamlit reruns the
    script on every interaction, and reusing a loop across reruns leaves
    closed-loop errors behind.
    """
    result: dict[str, Any] = {}

    def worker() -> None:
        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            result["value"] = loop.run_until_complete(coro_factory())
        except Exception as e:  # surfaced in the UI, not swallowed
            result["error"] = e
        finally:
            loop.close()

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    t.join()
    if "error" in result:
        raise result["error"]
    return result.get("value")


def stream_async(agen_factory: Callable[[], Any]) -> Iterator[str]:
    """Drain an async generator into a synchronous iterator.

    Used for the streamed write-up: the SDK yields asynchronously while
    Streamlit consumes synchronously, so a queue bridges the two.
    """
    q: queue.Queue[Any] = queue.Queue()
    SENTINEL = object()

    def worker() -> None:
        loop = asyncio.new_event_loop()

        async def pump() -> None:
            async for chunk in agen_factory():
                q.put(chunk)

        try:
            asyncio.set_event_loop(loop)
            loop.run_until_complete(pump())
        except Exception as e:
            q.put(e)
        finally:
            q.put(SENTINEL)
            loop.close()

    threading.Thread(target=worker, daemon=True).start()
    while True:
        item = q.get()
        if item is SENTINEL:
            return
        if isinstance(item, Exception):
            raise item
        yield item


# --------------------------------------------------------------------------
# sidebar
# --------------------------------------------------------------------------

def sidebar() -> tuple[str, str]:
    st.sidebar.title("thugsak")
    st.sidebar.caption("Kalshi sports edge finder · read-only")

    league = st.sidebar.selectbox(
        "Sport",
        options=list(LEAGUES),
        format_func=lambda k: LEAGUES[k].name,
        key="league",
    )

    # Central, not UTC and not the machine's zone: "today" is today in CT.
    # After 6pm CST the UTC date has already rolled over, so a UTC default
    # would show tomorrow's slate while tonight's games are still tipping off.
    today = today_local()
    picked = st.sidebar.date_input(
        "Date",
        value=st.session_state.get("picked_date", today),
        max_value=today + timedelta(days=14),
        key="picked_date",
        help="Pick a past date to check predictions against results.",
    )
    if isinstance(picked, (list, tuple)):
        picked = picked[0]

    if picked < today:
        st.sidebar.info(
            "**Lookback mode.** Predictions are computed as of kickoff minus "
            "one hour, using only games played before then."
        )

    with st.sidebar.expander("Track record", expanded=False):
        _track_record(league)

    with st.sidebar.expander("Model calibration", expanded=False):
        _calibration_panel(league)

    st.sidebar.divider()
    st.sidebar.caption(
        "Analysis only — this app never places an order and holds no API keys."
    )
    return league, picked.isoformat()


def _calibration_panel(league: str) -> None:
    """Show the constants fitted from the bootstrap seasons."""
    if not Path(DB_PATH).exists():
        st.warning("No database yet. Run `scripts/import_history.py`.")
        return
    with store.connect(DB_PATH, readonly=True) as conn:
        cal = store.get_calibration(conn, league)
        meta = store.calibration_meta(conn, league)
        counts = store.count_games(conn, league)

    if not cal:
        st.warning(
            f"{LEAGUES[league].name} not calibrated yet. Run:\n\n"
            f"`python scripts/import_history.py --league {league}`\n\n"
            "Until then the model falls back to rough league norms."
        )
        return

    st.metric("Home-field advantage", f"{cal.get('hfa', 0):+.2f} pts")
    c1, c2 = st.columns(2)
    c1.metric("σ margin", f"{cal.get('sigma_margin', 0):.2f}")
    c2.metric("σ total", f"{cal.get('sigma_total', 0):.2f}")
    st.caption(
        f"Blend weights — last 3: {cal.get('w_last3', 0):.2f}, "
        f"last 7: {cal.get('w_last7', 0):.2f}, "
        f"season: {cal.get('w_season', 0):.2f}"
    )
    gain = cal.get("form_gain")
    if gain is not None:
        st.caption(
            f"Recent form improves on a plain season average by "
            f"**{gain:+.1%}**."
            + (
                "  Near zero means recent form carries little signal for this "
                "league — the windows are still shown, but the model leans on "
                "the season."
                if abs(gain) < 0.02 else ""
            )
        )
    if meta.get("seasons"):
        st.caption(
            f"Fitted on seasons {meta['seasons']} "
            f"({meta.get('n_games', 0)} games), {str(meta.get('fitted_at', ''))[:10]}."
        )
    st.caption(f"Games retained: {counts or 'none'}")


def _track_record(league: str) -> None:
    """How the model has done on games actually looked at.

    Kept honest about what it is: a record of use, not a backtest. The games
    on it are the ones that happened to get opened, which is not a random
    sample, and a couple of dozen cannot separate skill from luck.
    """
    try:
        rec = run_async(lambda: tools.get_track_record(league))
    except Exception as e:
        st.caption(f"Unavailable: {type(e).__name__}")
        return
    if not rec.get("games"):
        st.caption(rec.get("note", "Nothing graded yet."))
        return

    c1, c2 = st.columns(2)
    c1.metric("Games graded", rec["games"])
    c2.metric("Winner called", rec.get("picked_the_winner") or "—")
    c1.metric(
        "Margin error",
        f"{rec['mean_absolute_margin_error']:.1f}"
        if rec.get("mean_absolute_margin_error") is not None else "—",
        help="Mean absolute error on the final margin, in points.",
    )
    c2.metric(
        "Total error",
        f"{rec['mean_absolute_total_error']:.1f}"
        if rec.get("mean_absolute_total_error") is not None else "—",
    )
    placed = rec.get("bets_placed") or 0
    if placed:
        st.metric(
            f"Bets: {rec['bets_won']}/{placed} won",
            f"${rec['profit_per_100_contracts_usd']:+,.2f}",
            help="Profit per 100 contracts on each recommended bet, after fees.",
        )
    else:
        st.caption("No recommended bet has settled yet.")
    st.caption(f":grey[{rec.get('note', '')}]")


# --------------------------------------------------------------------------
# slate
# --------------------------------------------------------------------------

@st.cache_data(ttl=60, show_spinner=False)
def load_slate(league: str, date_iso: str) -> dict[str, Any]:
    return run_async(lambda: tools.list_games(league, date_iso))


def slate_view(league: str, date_iso: str) -> None:
    st.subheader(f"{LEAGUES[league].name} — {date_iso}")
    st.caption(f"All times {tz_abbrev()} (Central).")

    try:
        with st.spinner("Loading games and Kalshi markets…"):
            data = load_slate(league, date_iso)
    except Exception as e:
        st.error(f"Could not load the slate: {type(e).__name__}: {e}")
        return

    games = data.get("games") or []
    if not games:
        st.info(f"No {LEAGUES[league].name} games found for {date_iso}.")
        return

    unmatched = data.get("unmatched_count") or 0
    if unmatched:
        with st.expander(
            f"{unmatched} Kalshi event(s) could not be matched to a game",
            expanded=False,
        ):
            st.caption(
                "Shown rather than hidden: unmatched events are "
                "disproportionately thin markets, which is where the largest "
                "mispricings tend to be. Usually a team-name mapping gap."
            )
            st.json(data.get("unmatched_kalshi_events") or [])

    for g in games:
        _game_row(league, g)


def _game_row(league: str, g: dict[str, Any]) -> None:
    # The date is already in the page header, so the row shows clock time
    # only -- in Central, with the zone named so it is never ambiguous.
    raw_start = g.get("start_time")
    start = fmt_time(raw_start) if raw_start else ""
    away = g.get("away_label") or g.get("away", "")
    home = g.get("home_label") or g.get("home", "")
    label = f"{away} @ {home}"
    with st.container(border=True):
        c1, c2, c3, c4 = st.columns([3, 2, 2, 1.4])

        with c1:
            st.markdown(f"**{label}**")
            bits = [start] if start else []
            if g.get("completed"):
                bits.append(f"final {g['score']}")
            st.caption(" · ".join(bits))

        with c2:
            spread = g.get("book_spread_home_relative")
            total = g.get("book_total")
            st.caption("Sportsbook")
            if spread is not None or total is not None:
                st.text(
                    f"spread {spread:+.1f}" if spread is not None else "spread —"
                )
                st.text(f"total {total:.1f}" if total is not None else "total —")
            else:
                st.text("no line")

        with c3:
            st.caption("Kalshi")
            n = g.get("kalshi_markets") or 0
            if n:
                st.text(f"{n} markets")
                st.text("ladders ✓" if g.get("has_ladders") else "winner only")
            else:
                st.text("not listed")

        with c4:
            if st.button(
                "Analyze →",
                key=f"go_{g['game_id']}",
                width="stretch",
                disabled=not (g.get("kalshi_markets") or 0),
            ):
                st.session_state["selected_game"] = g["game_id"]
                st.session_state["selected_label"] = label
                st.rerun()


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main() -> None:
    league, date_iso = sidebar()

    selected = st.session_state.get("selected_game")
    if selected:
        if st.button("← back to slate"):
            st.session_state.pop("selected_game", None)
            st.rerun()
        game_detail.render(
            league=league,
            game_id=selected,
            label=st.session_state.get("selected_label", ""),
            run_async=run_async,
            stream_async=stream_async,
        )
    else:
        slate_view(league, date_iso)


main()
