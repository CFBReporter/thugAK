import { cents, pct, usd } from '../format'
import type { Bet } from '../types'
import { MoneyBar, OutcomeBadge, RecBadge } from './bits'

/**
 * One of the five fixed slots: home moneyline, away moneyline, home spread,
 * away spread, over/under.
 *
 * The order and the count never change, which is the point -- a slot with
 * nothing worth betting says NO rather than disappearing, so the absence of a
 * recommendation is visible instead of being mistaken for a missing market.
 *
 * The price pair is rendered the way Kalshi renders it, yes on the left and no
 * on the right, but only the side the engine actually recommends is lit. A
 * total has no "no" market on Kalshi -- the under is a NO on an "Over X"
 * contract -- so showing both as equally live would misrepresent what you can
 * buy.
 */
export function BetRow({ bet }: { bet: Bet }) {
  const live = bet.recommendation !== 'NO'
  const edge = bet.edge_after_fees
  const bar = bet.edge_required_at_this_money

  return (
    <article className={`bet ${live ? 'is-bet' : ''}`}>
      <div className="bet-top">
        <div>
          <div className="bet-slot">{bet.slot}</div>
          <div className="bet-label">{bet.label}</div>
        </div>
        <div style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
          {bet.outcome ? <OutcomeBadge outcome={bet.outcome} /> : null}
          <RecBadge rec={bet.recommendation} />
        </div>
      </div>

      {bet.price !== null ? (
        <div className="bet-mkt">
          <span className={`price ${live ? 'yes' : 'no'}`}>
            <span className="price-k">Ask</span>
            {cents(bet.price)}
          </span>
          {bet.all_in_cost_after_fees !== null ? (
            <span className="ticker">
              {cents(bet.all_in_cost_after_fees)} all-in after fees
            </span>
          ) : null}
          {edge !== null ? (
            <span className={`badge ${edge > 0 ? 'edge' : 'no'}`}>
              {pct(edge, true)} edge
              {bar !== null ? ` vs ${pct(bar)} bar` : ''}
            </span>
          ) : null}
        </div>
      ) : null}

      <div className="bet-why">{bet.why}</div>

      {bet.dollars_traded_on_this_market > 0 || bet.money_note ? (
        <MoneyBar dollars={bet.dollars_traded_on_this_market} note={bet.money_note} />
      ) : null}

      {bet.money_flow ? <div className="note">{bet.money_flow}</div> : null}

      {bet.cautions.length > 0 ? (
        <div className="note">{bet.cautions.join(' ')}</div>
      ) : null}

      {bet.profit_per_100_contracts_usd !== null &&
      bet.profit_per_100_contracts_usd !== undefined ? (
        <div className="note">
          Would have returned {usd(bet.profit_per_100_contracts_usd)} per 100 contracts.
        </div>
      ) : null}
    </article>
  )
}
