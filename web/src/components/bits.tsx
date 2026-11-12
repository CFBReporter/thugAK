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
  // A 503 is a data provider declining to be used, and its message explains
  // what would have to change. That is information, not a crash, so it is
  // shown as written rather than behind a generic failure message.
  const blocked = status === 503
  return (
    <div className={`state ${blocked ? '' : 'err'}`}>
      <h2>{blocked ? 'That data source is switched off' : 'Could not load that'}</h2>
      <p>{error}</p>
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
