import type { ReactNode } from 'react'
import { usd } from '../format'
import type { Recommendation } from '../types'

/** BET / SMALL / NO, in the colours the whole UI uses for yes-ish and no-ish. */
export function RecBadge({ rec }: { rec: Recommendation }) {
  const cls = rec === 'BET' ? 'bet' : rec === 'SMALL' ? 'small' : 'no'
  return <span className={`badge ${cls}`}>{rec}</span>
}

/** WON / LOST, shown only in lookback mode. */
export function OutcomeBadge({ outcome }: { outcome: 'WON' | 'LOST' }) {
  return (
    <span className={`badge ${outcome === 'WON' ? 'hit' : 'miss'}`}>
      {outcome === 'WON' ? 'Won' : 'Lost'}
    </span>
  )
}

/**
 * How much money is on a market, as a figure and a bar.
 *
 * The bar is log-scaled against $2M. Kalshi volumes span four orders of
 * magnitude across a slate -- a linear bar would render every college market
 * as empty and every NFL market as full, which tells you nothing. The tier
 * label beside it is the engine's own classification, which is what actually
 * gates the required edge; the bar is only there to make the comparison quick.
 */
export function MoneyBar({
  dollars,
  tier,
  note,
}: {
  dollars: number
  tier?: string
  note?: string | null
}) {
  const frac = dollars <= 0 ? 0 : Math.min(1, Math.log10(1 + dollars) / Math.log10(2_000_000))
  return (
    <div className="money">
      <div className="money-top">
        <span className="money-amt">{usd(dollars)}</span>
        {tier ? <span className="money-tier">{tier}</span> : null}
      </div>
      <div className="track">
        <i style={{ width: `${Math.max(frac * 100, dollars > 0 ? 2 : 0)}%` }} />
      </div>
      {note ? <div className="note">{note}</div> : null}
    </div>
  )
}

export function Card({
  title,
  children,
  className = '',
}: {
  title?: string
  children: ReactNode
  className?: string
}) {
  return (
    <section className={`card ${className}`}>
      {title ? <h2 className="card-h">{title}</h2> : null}
      {children}
    </section>
  )
}

export function Empty({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="state">
      <h2>{title}</h2>
      {children ? <p>{children}</p> : null}
    </div>
  )
}

export function Failed({
  error,
  status,
}: {
  error: string
  status: number | null
}) {
  // Two statuses here are information rather than breakage, and styling them
  // as errors would be a lie about what happened:
  //
  //   503 -- a data provider declining to be used; the message says what
  //          would have to change to enable it.
  //   422 -- the game is real and the engine read it fine, but it will not
  //          put a number on it, and it says why. A model that refuses on
  //          thin history is working correctly; the failure worth fixing is
  //          refusing *silently*, which is what this screen used to do.
  //
  // The engine's reasons arrive joined with ' -- ', split back out so each
  // reads as its own point instead of one run-on line.
  const declined = status === 422
  const blocked = status === 503
  const soft = declined || blocked
  const [head, ...reasons] = error.split(' -- ')
  return (
    <div className={`state ${soft ? '' : 'err'}`}>
      <h2>
        {declined
          ? 'No prediction for this game'
          : blocked
            ? 'That data source is switched off'
            : 'Could not load that'}
      </h2>
      <p>{declined ? head : error}</p>
      {declined && reasons.length > 0 ? (
        <div className="note" style={{ display: 'grid', gap: 8, marginTop: 12, textAlign: 'left' }}>
          {reasons.map((r, i) => (
            <p key={i}>{r}</p>
          ))}
        </div>
      ) : null}
      {declined ? (
        <p className="note" style={{ marginTop: 12 }}>
          The market numbers on the slate are still live &mdash; it is the model&rsquo;s own
          forecast that is missing, so there is nothing to compare them against.
        </p>
      ) : null}
    </div>
  )
}

export function Skeletons({ n = 6 }: { n?: number }) {
  return (
    <div className="games" aria-busy="true" aria-label="Loading games">
      {Array.from({ length: n }, (_, i) => (
        <div className="skel" key={i} />
      ))}
    </div>
  )
}
