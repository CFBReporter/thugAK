"""Game detail: a predicted score, five bets, and the money behind them.

The page answers three questions in the order they get asked, and nothing
else sits above them:

1. **What happens?** A predicted final score.
2. **What do I bet?** Five fixed rows -- moneyline each way, spread each way,
   total over or under -- each with BET, SMALL or NO and one named Kalshi
   market.
3. **Is the market worth arguing with?** The dollars traded, on every row.

Earlier versions led with a sortable table of sixty ladder strikes. That is
data, not an answer: read top-down it presents one disagreement with the
market as sixty independent opportunities, and the reader has to work out
which row is the recommendation. Everything of that kind now sits below in a
single collapsed section, where it belongs as evidence rather than as the
headline.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

import pandas as pd
import streamlit as st

SKILL_PATH = (
    Path(__file__).resolve().parents[2]
    / ".claude" / "skills" / "thugsak-analyst" / "SKILL.md"
)

REC_ICON = {"BET": "🟢", "SMALL": "🟡", "NO": "⚪"}
OUTCOME_ICON = {"WON": "✅", "LOST": "❌"}


def render(
    *,
    league: str,
    game_id: str,
    label: str,
    run_async: Callable[[Callable[[], Any]], Any],
    stream_async: Callable[[Callable[[], Any]], Any],
) -> None:
    from thugsak.mcp import tools

    st.subheader(label or game_id)

    size = st.slider(
        "Position size (contracts)", 25, 2000, 100, step=25,
        help="Fees round up per order and the book is walked at this size, so "
             "the edge genuinely depends on it.",
    )

    try:
        with st.spinner("Predicting the game and pricing the markets…"):
            rec = run_async(
                lambda: tools.get_recommendation(league, game_id, contracts=size)
            )
    except Exception as e:
        st.error(f"Analysis failed: {type(e).__name__}: {e}")
        return
    if "error" in rec:
        st.error(rec["error"])
        for w in rec.get("warnings") or []:
            st.warning(w)
        return

    if rec.get("start_local"):
        st.caption(
            f"Tip-off {rec['start_local']} · predicted as of "
            f"{rec.get('as_of_local', '—')} · all times Central"
        )

    _predicted_score(rec)
    _the_call(rec)
    _bets(rec)
    _money(rec)
    _basis(rec)

    st.divider()
    st.markdown("#### Claude's read")
    _ai_read(league, game_id, label, stream_async)

    with st.expander("Everything underneath — all strikes, flow, injuries, form"):
        t1, t2, t3, t4 = st.tabs(
            ["All strikes", "Money flow", "Injuries & availability",
             "Form & inputs"]
        )
        with t1:
            _all_strikes(league, game_id, size, run_async)
        with t2:
            _flow(league, game_id, run_async)
        with t3:
            _availability_and_injuries(league, game_id, run_async)
        with t4:
            _form(league, game_id, run_async)


# --------------------------------------------------------------------------
# 1. the predicted score
# --------------------------------------------------------------------------

def _predicted_score(rec: dict[str, Any]) -> None:
    pts = rec.get("predicted_points") or {}
    if not pts or all(v is None for v in pts.values()):
        st.warning("No prediction available for this game.")
        return

    lookback = rec.get("mode") == "lookback"
    result = rec.get("result") or {}

    st.markdown(
        "#### Predicted final score"
        + ("  :grey[— as it would have read an hour before tip-off]"
           if lookback else "")
    )
    cols = st.columns(len(pts) + 1)
    for col, (team, v) in zip(cols, pts.items()):
        col.metric(team, f"{v:.0f}" if v is not None else "—")
    cols[-1].metric("Total", f"{(rec.get('predicted_total') or 0):.0f}")

    st.markdown(f"**{rec.get('predicted_result', '')}**")

    if lookback and result:
        # The whole point of a lookback is the comparison, so the actual
        # score sits beside the prediction rather than somewhere below it.
        st.markdown(f"**Actual:** {result.get('actual_score', '—')}")
        c1, c2, c3 = st.columns(3)
        me, te = result.get("margin_error"), result.get("total_error")
        c1.metric("Margin off by", f"{me:+.0f}" if me is not None else "—")
        c2.metric("Total off by", f"{te:+.0f}" if te is not None else "—")
        picked = result.get("picked_the_winner")
        c3.metric(
            "Picked the winner",
            "yes" if picked else ("no" if picked is False else "—"),
        )
        st.caption(result.get("note", ""))

    bits = [f"Confidence: {rec.get('confidence_in_prediction') or '—'}"]
    if rec.get("versus_the_sportsbook"):
        bits.append(f"Sportsbook: {rec['versus_the_sportsbook']}")
    st.caption(" · ".join(bits))

    if lookback:
        with st.expander("What a lookback can and cannot reconstruct"):
            st.caption(rec.get("lookback_note", ""))
            for c in rec.get("lookback_caveats") or []:
                st.markdown(f"- {c}")
    # Whole points are deliberate; saying so stops the roundness reading as
    # carelessness.
    st.caption(
        ":grey[Rounded to whole points — the model's own error is far larger "
        "than a point, so decimals here would be false precision.]"
    )
    for c in rec.get("prediction_cautions") or []:
        st.warning(c)


# --------------------------------------------------------------------------
# 2. the call
# --------------------------------------------------------------------------

def _the_call(rec: dict[str, Any]) -> None:
    """One instruction, above the table.

    Five rows of BET/SMALL/NO is a finding; this is what to do about it.
    Without it the reader has to rank five rows themselves and work out which
    "SMALL" was the strongest, which is exactly the work the page exists to
    do for them.
    """
    call = rec.get("the_call")
    if not call:
        return
    st.markdown("#### The call")
    best = rec.get("best_bet")
    if best:
        st.success(f"**{call}**")
    else:
        st.warning(f"**{call}**")

    if rec.get("mode") == "lookback":
        graded = rec.get("bets_graded")
        if graded:
            st.caption(f"How it turned out: {graded}")


# --------------------------------------------------------------------------
# 3. the five bets
# --------------------------------------------------------------------------

def _bets(rec: dict[str, Any]) -> None:
    st.markdown("#### What to bet")
    bets = rec.get("bets") or []
    if not bets:
        st.info("No Kalshi markets matched this game.")
        return

    if any(b["recommendation"] != "NO" for b in bets):
        st.success(f"**{rec.get('headline', '')}**")
    else:
        st.info(
            "**Nothing worth betting here.** All five rows are NO, and each "
            "says why. Most games hold no edge, and a manufactured "
            "recommendation is worse than none."
        )

    for b in bets:
        _bet_row(b)


def _bet_row(b: dict[str, Any]) -> None:
    verdict = b["recommendation"]
    with st.container(border=True):
        c1, c2, c3, c4 = st.columns([2.6, 3.0, 1.5, 1.9])

        outcome = b.get("outcome")
        badge = (
            f"  {OUTCOME_ICON[outcome]} {outcome}"
            if outcome in OUTCOME_ICON else ""
        )
        c1.markdown(f"{REC_ICON.get(verdict, '')} **{verdict}**{badge}")
        c1.caption(b["label"])

        if b.get("bet"):
            c2.markdown(f"**{b['bet']}**")
            c2.caption(b.get("kalshi_ticker") or "")
        else:
            c2.markdown("—")

        price = b.get("price")
        if price is not None:
            c3.markdown(f"**{price:.2f}**")
            allin = b.get("all_in_cost_after_fees")
            c3.caption(f"{allin:.3f} all-in" if allin else "")
        else:
            c3.markdown("—")

        edge = b.get("edge_after_fees")
        need = b.get("edge_required_at_this_money")
        if edge is not None:
            c4.markdown(f"**{edge:+.1%}**")
            c4.caption(f"needs {need:.0%}" if need else "edge after fees")
        else:
            c4.markdown("—")

        st.caption(b["why"])

        line = f"Money on this market: ${b.get('dollars_traded_on_this_market') or 0:,.0f}"
        if b.get("money_flow"):
            line += f" · {b['money_flow']}"
        st.caption(line)

        profit = b.get("profit_per_100_contracts_usd")
        if profit is not None:
            st.caption(
                f"Settled {b.get('outcome', '').lower()}: "
                f"**${profit:+,.2f}** per 100 contracts."
            )

        for c in b.get("cautions") or []:
            st.caption(f"⚠ {c}")


# --------------------------------------------------------------------------
# 3. the money
# --------------------------------------------------------------------------

def _money(rec: dict[str, Any]) -> None:
    km = rec.get("kalshi_money") or {}
    if not km.get("markets_counted"):
        return
    st.markdown("#### Money on Kalshi")
    c1, c2, c3 = st.columns(3)
    c1.metric("Traded on this game", f"${km.get('total_traded_usd', 0):,.0f}")
    c2.metric("Open positions", f"${km.get('open_positions_usd', 0):,.0f}")
    c3.metric("Markets", km.get("markets_counted", 0))

    by = km.get("by_bet_type_usd") or {}
    if by:
        st.caption(
            "By bet type: "
            + ", ".join(
                f"{k} ${v:,.0f}"
                for k, v in sorted(by.items(), key=lambda kv: -kv[1])
            )
        )
    if km.get("busiest_market"):
        st.caption(
            f"Busiest: {km['busiest_market']} "
            f"(${km.get('busiest_market_usd', 0):,.0f})"
        )
    st.caption(f":grey[{km.get('note', '')}]")


# --------------------------------------------------------------------------
# 4. what it rests on
# --------------------------------------------------------------------------

def _basis(rec: dict[str, Any]) -> None:
    basis = rec.get("based_on") or []
    if not basis:
        return
    with st.expander("What the prediction is based on"):
        for line in basis:
            st.markdown(f"- {line}")

        skill = rec.get("model_skill") or {}
        sm, stot = skill.get("spread_and_moneyline"), skill.get("total")
        bits = []
        if sm is not None:
            bits.append(f"spread & moneyline **{sm:+.0%}**")
        if stot is not None:
            bits.append(f"totals **{stot:+.0%}**")
        if bits:
            st.caption(
                "Measured accuracy in this league, as the share of outcome "
                "variance explained against simply predicting the league "
                "average: " + ", ".join(bits)
                + ". Every edge above is already scaled by these."
            )

        census = rec.get("market_census") or {}
        if census:
            st.caption(
                "Kalshi markets for this game — "
                + ", ".join(
                    f"{k}: {v['listed']} listed, {v['credible']} usable"
                    for k, v in census.items()
                )
                + ". " + (rec.get("census_note") or "")
            )


# --------------------------------------------------------------------------
# the evidence, all of it below the fold
# --------------------------------------------------------------------------

def _all_strikes(league: str, game_id: str, size: int, run_async: Callable) -> None:
    from thugsak.mcp import tools

    st.caption(
        "Every strike, for inspection. The five rows above hold the "
        "recommendation — a sorted list of edges on one ladder is usually one "
        "disagreement with the market repeated, not many separate bets."
    )
    try:
        data = run_async(lambda: tools.get_edges(league, game_id, contracts=size))
    except Exception as e:
        st.error(f"Edge calculation failed: {type(e).__name__}: {e}")
        return

    rungs = data.get("rungs") or []
    if not rungs:
        st.info(data.get("note") or "No Kalshi markets matched this game.")
        return

    for w in data.get("ladder_warnings") or []:
        st.warning(w)
    arb = data.get("arbitrage") or []
    if arb:
        st.success(
            f"**{len(arb)} arbitrage opportunit(ies)** — both sides after fees "
            f"cost under $1, so the payout is locked either way."
        )
        st.json(arb)

    kind_label = {"game": "moneyline", "spread": "spread", "total": "total"}
    df = pd.DataFrame(
        [
            {
                "bet type": kind_label.get(r["kind"], r["kind"]),
                "market": r["title"],
                "ask": r["ask"],
                "all-in": r["all_in_cost"],
                "model prob": r["model_probability"],
                "edge (scaled)": r.get("adjusted_edge"),
                "raw edge": r["net_edge"],
                "EV/100 ($)": r["ev_per_100_contracts_usd"],
                "volume": r["volume"],
                "suspect": r["suspect"],
            }
            for r in rungs
        ]
    )
    st.dataframe(
        df, hide_index=True, width="stretch",
        column_config={
            "ask": st.column_config.NumberColumn(format="%.2f"),
            "all-in": st.column_config.NumberColumn(format="%.3f"),
            "model prob": st.column_config.NumberColumn(format="%.1f%%"),
            "edge (scaled)": st.column_config.NumberColumn(format="%.2f%%"),
            "raw edge": st.column_config.NumberColumn(format="%.2f%%"),
            "EV/100 ($)": st.column_config.NumberColumn(format="$%.2f"),
        },
    )
    st.caption(f"Fee multipliers in force: {data.get('fee_multipliers')}")


def _flow(league: str, game_id: str, run_async: Callable) -> None:
    from thugsak.mcp import tools

    try:
        edges = run_async(lambda: tools.get_edges(league, game_id, contracts=100))
    except Exception as e:
        st.error(f"Could not list markets: {type(e).__name__}: {e}")
        return
    rungs = edges.get("rungs") or []
    winners = [r for r in rungs if r["kind"] == "game"]
    choices = [r["ticker"] for r in (winners or rungs)]
    if not choices:
        st.info("No markets to analyse.")
        return

    ticker = st.selectbox("Market", choices, key=f"flow_{game_id}")
    hours = st.slider("Lookback (hours)", 6, 168, 48, step=6)
    try:
        with st.spinner("Reading the tape…"):
            fl = run_async(lambda: tools.get_money_flow(ticker, lookback_hours=hours))
    except Exception as e:
        st.error(f"Flow analysis failed: {type(e).__name__}: {e}")
        return

    st.info(fl.get("summary") or "No flow summary.")
    if fl.get("divergence"):
        st.success(f"**Divergence:** {fl['divergence']}")

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Imbalance", f"{fl['imbalance']:+.2f}",
              help="-1 all NO, +1 all YES. Weighted by dollars at risk and "
                   "time-decayed, so a big recent position counts most.")
    c2.metric("Direction", str(fl.get("direction", "—")).upper())
    c3.metric("Conviction", str(fl.get("conviction", "—")))
    c4.metric("Notional", f"${fl.get('total_notional_usd', 0):,.0f}")

    c1, c2, c3 = st.columns(3)
    wc = fl.get("whale_consensus_yes_share")
    c1.metric("Biggest trades (YES)", f"{wc:.0%}" if wc is not None else "—")
    c2.metric("By trade count", f"{fl.get('imbalance_by_trade_count', 0):+.2f}")
    c3.metric("Block trades", fl.get("block_trades", 0))
    st.caption(fl.get("count_vs_size_note", ""))

    if fl.get("open_interest_reading"):
        st.markdown(f"**Open interest:** {fl['open_interest_reading']}")

    big = fl.get("largest_trades") or []
    if big:
        st.markdown("**Largest trades by dollars at risk**")
        st.dataframe(
            pd.DataFrame(big), hide_index=True, width="stretch",
            column_config={
                "price": st.column_config.NumberColumn(format="%.2f"),
                "notional_usd": st.column_config.NumberColumn(format="$%.0f"),
                "contracts": st.column_config.NumberColumn(format="%.0f"),
                "hours_ago": st.column_config.NumberColumn(format="%.1f"),
                "traded_at_local": st.column_config.TextColumn("traded (CT)"),
            },
        )
    if fl.get("depth"):
        with st.expander("Orderbook depth and true fill cost"):
            st.caption(
                "The walked fill price is what an order of this size would "
                "actually pay. Top-of-book flatters thin markets."
            )
            st.json(fl["depth"])
    for n in fl.get("notes") or []:
        st.caption(f"· {n}")


def _availability_and_injuries(league: str, game_id: str, run_async: Callable) -> None:
    from thugsak.mcp import tools

    try:
        pred = run_async(lambda: tools.get_prediction(league, game_id))
    except Exception as e:
        st.error(f"Could not load injuries: {type(e).__name__}: {e}")
        return

    _availability(pred)

    inj = pred.get("injury_adjustment") or {}
    detail = inj.get("detail") or []
    st.markdown("#### Injury report")
    c1, c2, c3 = st.columns(3)
    c1.metric("Home adjustment", f"{inj.get('home_points', 0):+.1f} pts")
    c2.metric("Away adjustment", f"{inj.get('away_points', 0):+.1f} pts")
    c3.metric("Confidence", str(inj.get("confidence", "—")))
    st.caption(inj.get("note", ""))

    if not detail:
        st.info(
            "No injuries with doubtful availability were reported. For college "
            "games this is expected — no usable injury feed exists, so the "
            "availability panel above is the signal to read."
        )
        return

    for d in detail:
        with st.container(border=True):
            st.markdown(
                f"**{d['player']}** · {d['position']} · _{d['status']}_ "
                f"→ **{d['adjustment']:+.1f} pts** ({d['confidence']} confidence)"
            )
            if d.get("with_without"):
                st.caption(f"With/without: {d['with_without']}")
            if d.get("backup"):
                st.caption(f"Replacement: {d['backup']}")
            cols = st.columns(2)
            for col, key, title in (
                (cols[0], "last3", "Last 3 games (per game)"),
                (cols[1], "last7", "Last 7 games (per game)"),
            ):
                stats = d.get(key) or {}
                with col:
                    st.caption(title)
                    if stats:
                        st.dataframe(
                            pd.DataFrame([dict(list(stats.items())[:6])]).T.rename(
                                columns={0: "avg"}
                            ),
                            width="stretch",
                        )
                    else:
                        st.text("no stats logged")
            for r in d.get("rationale") or []:
                st.caption(f"· {r}")


def _availability(pred: dict[str, Any]) -> None:
    av = pred.get("availability") or {}
    teams = av.get("teams") or {}
    if not teams:
        return

    primary = bool(av.get("is_primary_signal"))
    st.markdown(
        "#### Who has actually been playing"
        + ("  :red[**primary signal for this league**]" if primary else "")
    )
    st.caption(av.get("why", ""))

    for tid, blk in teams.items():
        label = blk.get("team_label") or tid
        if "error" in blk:
            st.caption(f"**{label}**: unavailable ({blk['error']})")
            continue
        rows = blk.get("detail") or []
        st.markdown(
            f"**{label}** — read {blk.get('games_read', 0)} recent box score(s)"
        )
        if not rows:
            filtered = blk.get("immaterial_absences_filtered") or 0
            st.caption(
                "No established contributor missing."
                + (f" ({filtered} immaterial absences filtered out)"
                   if filtered else "")
            )
            continue
        share = blk.get("share_of_usage_missing") or 0.0
        if share:
            st.caption(
                f"Absent contributors account for **{share:.0%}** of team "
                f"{blk.get('usage_unit', 'usage')}."
            )
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "player": r["name"],
                        "pos": r["position"],
                        "kind": r["kind"],
                        "missed": r["consecutive_missed"],
                        "usage share": r["usage_share"],
                        "position output retained": r["position_output_retained"],
                        "read": r["confidence"],
                    }
                    for r in rows
                ]
            ),
            hide_index=True, width="stretch",
            column_config={
                "usage share": st.column_config.NumberColumn(format="%.1f%%"),
                "position output retained": st.column_config.NumberColumn(
                    format="%.0f%%",
                    help="Share of the position's output the team kept while "
                         "this player was out. Near 100% means the role was "
                         "covered, so the absence costs little.",
                ),
            },
        )
        if blk.get("note"):
            st.caption(blk["note"])
    st.caption(
        ":grey[Derived from who appeared in recent box scores, not an injury "
        "report. It establishes that a player has been absent — evidence "
        "about the next game, not a statement about it.]"
    )
    st.divider()


def _form(league: str, game_id: str, run_async: Callable) -> None:
    from thugsak.mcp import tools

    try:
        pred = run_async(
            lambda: tools.get_prediction(league, game_id, include_injuries=False)
        )
    except Exception as e:
        st.error(f"Could not load form: {type(e).__name__}: {e}")
        return

    rows = [
        {
            "anchor": a["name"],
            "spread (home-relative)": a.get("spread_home_relative"),
            "total": a.get("total"),
            "home win prob": a.get("home_win_prob"),
            "source": a.get("source"),
        }
        for a in (pred.get("anchors") or [])
    ]
    if rows:
        st.markdown("**Three independent views**")
        st.caption(
            "Agreement is uninformative; disagreement is the signal. If the "
            "model is the lone outlier, suspect a bug before an edge."
        )
        st.dataframe(
            pd.DataFrame(rows), hide_index=True, width="stretch",
            column_config={
                "home win prob": st.column_config.NumberColumn(format="%.1f%%"),
                "spread (home-relative)": st.column_config.NumberColumn(
                    format="%+.1f"
                ),
                "total": st.column_config.NumberColumn(format="%.1f"),
            },
        )

    st.markdown("**Form**")
    st.caption(
        "Opponent-adjusted: each game's points are re-based by that "
        "opponent's defensive rating, so a big number against a weak defence "
        "does not read as strength."
    )
    for side, key in (("home_name", "home_form"), ("away_name", "away_form")):
        f = pred.get(key) or {}
        st.markdown(f"**{pred.get(side, side)}** — {f.get('games_played', 0)} games")
        wrows = [
            {"window": w, **f[w]}
            for w in ("last3", "last7", "season", "home", "away")
            if isinstance(f.get(w), dict)
        ]
        if wrows:
            st.dataframe(pd.DataFrame(wrows), hide_index=True, width="stretch")
        bits = []
        if f.get("trend_last3_vs_last7") is not None:
            bits.append(f"trend {f['trend_last3_vs_last7']:+.1f} pts")
        if f.get("home_away_delta") is not None:
            bits.append(f"home/away delta {f['home_away_delta']:+.1f} pts")
        if f.get("sparse"):
            bits.append("thin sample — leaning on shrunk ratings")
        if bits:
            st.caption(" · ".join(bits))

    st.caption(
        f"Blend weights: {pred.get('blend_weights')} · "
        f"σ margin {pred.get('sigma_margin', 0):.2f}, "
        f"σ total {pred.get('sigma_total', 0):.2f}"
    )


# --------------------------------------------------------------------------

def _ai_read(league: str, game_id: str, label: str, stream_async: Callable) -> None:
    st.caption(
        "Claude reads the same tools this page uses and writes up what it "
        "makes of them. It can disagree with the model — that is the point."
    )
    if not st.button("Generate read", key=f"ai_{game_id}"):
        return

    prompt = (
        f"Analyse the {league.upper()} game {label} (game_id {game_id}).\n\n"
        f"Call get_recommendation first. It returns a predicted score and "
        f"exactly five bets: moneyline each way, spread each way, and the "
        f"total as over or under. Evaluate only those five.\n\n"
        f"Write a short brief:\n"
        f"1. The predicted score, and whether you trust it given the inputs, "
        f"the sample sizes and this league's measured accuracy.\n"
        f"2. For each of the five bets: place it or not, and why in one "
        f"sentence. Say how much money is on each market and let that weigh "
        f"on you — a price with a million dollars behind it is far more "
        f"likely right than one with two hundred.\n"
        f"3. Any bet you would actually place, with the Kalshi market, the "
        f"price and the edge after fees — or say plainly there is none.\n\n"
        f"Be concrete and brief. Flag anything that looks like a data problem "
        f"rather than an opportunity."
    )
    try:
        box = st.empty()
        acc: list[str] = []
        for chunk in stream_async(lambda: _agent_stream(prompt)):
            acc.append(chunk)
            box.markdown("".join(acc))
    except Exception as e:
        st.error(
            f"Agent run failed: {type(e).__name__}: {e}\n\n"
            "The SDK uses this machine's Claude Code credentials; if you are "
            "signed out of the CLI, this is where it shows."
        )


async def _agent_stream(prompt: str):
    from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

    from thugsak.mcp.sdk_server import (
        SERVER_NAME, allowed_tool_names, build_sdk_server,
    )

    system = (
        SKILL_PATH.read_text(encoding="utf-8")
        if SKILL_PATH.exists()
        else "You are a careful sports-betting analyst."
    )
    options = ClaudeAgentOptions(
        mcp_servers={SERVER_NAME: build_sdk_server()},
        allowed_tools=allowed_tool_names(),
        system_prompt=system,
        max_turns=12,
    )
    async with ClaudeSDKClient(options=options) as client:
        await client.query(prompt)
        async for msg in client.receive_response():
            for block in getattr(msg, "content", None) or []:
                text = getattr(block, "text", None)
                if text:
                    yield text
