import { api, useAsync } from '../api'
import { pct, points, usdExact } from '../format'
import type { GameRow } from '../types'
import { BetRow } from './BetRow'
import { LadderCard } from './Ladder'
import { Card, Failed, MoneyBar, Skeletons } from './bits'

/**
 * The answer screen: predicted score, one definitive call, then the five bets.
 *
 * Deliberately ordered so the top of the page is a decision rather than
 * evidence. v1 learned this the hard way -- five rows of NO and SMALL left
 * the question "so what do I actually bet?" unanswered, so `the_call` is a
 * single instruction above everything, and the supporting detail sits below
 * it for anyone who wants to check the reasoning.
 */
export function GameDetail({
  league,
  game,
  onBack,
}: {
  league: string
  game: GameRow
  onBack: () => void
}) {
  const { data, error, status, loading } = useAsync(
    (signal) => api.recommendation(league, game.game_id, signal),
    [league, game.game_id],
  )

  return (
    <>
      <button className="back" onClick={onBack}>
        &larr; {game.away_label} @ {game.home_label}
      </button>

      {loading ? <Skeletons n={4} /> : null}
      {error ? <Failed error={error} status={status} /> : null}

      {data ? (
        <>
          {data.mode === 'lookback' ? (
            <div className="banner">
              <span>
                <b>Lookback.</b> {data.lookback_note}
              </span>
            </div>
          ) : null}

          {/* ---- predicted score ---- */}
          <Card className="hero">
            <div className="score">
              {/* Keyed by side, not by name. `predicted_points` is keyed by
                  the model's display label ("Washington Commanders"), which
                  is not the abbreviation the slate hands us ("WSH"), so
                  indexing it by that would render a dash on every game. */}
              <div className="score-side away">
                <div className="score-team">{game.away_name}</div>
                <div className="score-num">{points(data.predicted_away_points)}</div>
              </div>
              <div className="score-sep">&ndash;</div>
              <div className="score-side">
                <div className="score-team">{game.home_name}</div>
                <div className="score-num">{points(data.predicted_home_points)}</div>
              </div>
            </div>

            {data.result ? (
              <div className="score-actual" style={{ textAlign: 'center' }}>
                ACTUAL {data.result.actual_score}
                {data.result.margin_error_abs !== undefined
                  ? ` -- margin off by ${data.result.margin_error_abs}`
                  : ''}
                {data.result.total_error_abs !== undefined
                  ? `, total off by ${data.result.total_error_abs}`
                  : ''}
                {data.result.picked_the_winner !== undefined
                  ? data.result.picked_the_winner
                    ? ', picked the winner'
                    : ', wrong winner'
                  : ''}
              </div>
            ) : null}

            <div className="score-sub">
              <div className="kv">
                <span className="k">Result</span>
                <span className="v">{data.predicted_result}</span>
              </div>
              {data.versus_the_sportsbook ? (
                <div className="kv">
                  <span className="k">Book</span>
                  <span className="v">{data.versus_the_sportsbook}</span>
                </div>
              ) : null}
              <div className="kv">
                <span className="k">Based on</span>
                <span className="v">{data.based_on.join(' ')}</span>
              </div>
              <div className="kv">
                <span className="k">Confidence</span>
                <span className="v">{data.confidence_in_prediction}</span>
              </div>
            </div>
          </Card>

          {/* ---- the call ---- */}
          <Card className={`call ${data.best_bet ? '' : 'none'}`}>
            <div className="call-h">
              <span className="card-h" style={{ margin: 0 }}>
                The call
              </span>
              {data.bets_graded ? (
                <span className="badge no">{data.bets_graded}</span>
              ) : null}
            </div>
            <div className="call-txt">{data.the_call}</div>
            {data.best_bet ? (
              <div className="call-why">{data.best_bet.why}</div>
            ) : null}
          </Card>

          {/* ---- money ---- */}
          <Card title={`Kalshi money -- ${data.kalshi_money.markets_counted} markets`}>
            <MoneyBar
              dollars={data.kalshi_money.total_traded_usd}
              tier={data.kalshi_money.tier}
              note={data.kalshi_money.summary}
            />
            <div className="score-sub">
              <div className="kv">
                <span className="k">Traded</span>
                <span className="v">{usdExact(data.kalshi_money.total_traded_usd)}</span>
              </div>
              <div className="kv">
                <span className="k">Open</span>
                <span className="v">{usdExact(data.kalshi_money.open_positions_usd)}</span>
              </div>
              {data.kalshi_money.busiest_market ? (
                <div className="kv">
                  <span className="k">Busiest</span>
                  <span className="v">
                    {data.kalshi_money.busiest_market} &mdash;{' '}
                    {usdExact(data.kalshi_money.busiest_market_usd)}
                  </span>
                </div>
              ) : null}
            </div>
            {data.kalshi_money.note ? (
              <div className="note" style={{ marginTop: 10 }}>
                {data.kalshi_money.note}
              </div>
            ) : null}
          </Card>

          {/* ---- the five bets ---- */}
          <Card title="Every bet on this game">
            <div className="bets">
              {data.bets.map((b) => (
                <BetRow key={b.slot} bet={b} />
              ))}
            </div>
          </Card>

          {/* ---- the ladder ---- */}
          {/* Below the verdict on purpose: sixty strikes are one disagreement
              with the market repeated, not sixty opportunities. Only fetched
              when Kalshi actually lists spread/total ladders for this game. */}
          {game.has_ladders ? <LadderCard league={league} gameId={game.game_id} /> : null}

          {/* ---- caveats ---- */}
          {data.warnings.length > 0 ||
          data.prediction_cautions.length > 0 ||
          data.lookback_caveats?.length ? (
            <Card title="Worth knowing">
              <div className="note" style={{ display: 'grid', gap: 8 }}>
                {/* Deduplicated: `prediction_cautions` and `warnings` overlap,
                    because a caution the prediction raises is also a warning
                    about the bet built on it. Concatenated raw, the thin-history
                    note and the model-vs-market disagreement each appeared
                    twice, which reads as two separate problems rather than one
                    stated once. */}
                {Array.from(
                  new Set([
                    ...data.prediction_cautions,
                    ...data.warnings,
                    ...(data.lookback_caveats ?? []),
                  ]),
                ).map((w, i) => (
                  <p key={i}>{w}</p>
                ))}
              </div>
            </Card>
          ) : null}

          <Card title="Measured skill">
            <div className="score-sub" style={{ marginTop: 0, paddingTop: 0, border: 0 }}>
              <div className="kv">
                <span className="k">Margin</span>
                <span className="v">{pct(data.model_skill.spread_and_moneyline, true)}</span>
              </div>
              <div className="kv">
                <span className="k">Total</span>
                <span className="v">{pct(data.model_skill.total, true)}</span>
              </div>
            </div>
            <div className="note" style={{ marginTop: 10 }}>
              {data.model_skill.note}
            </div>
          </Card>
        </>
      ) : null}
    </>
  )
}
