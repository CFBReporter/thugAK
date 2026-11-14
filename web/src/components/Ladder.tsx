import { api, useAsync } from '../api'
import { cents, pct, usdExact } from '../format'
import type { EdgeRung } from '../types'
import { Card, Failed, Skeletons } from './bits'

/**
 * The ladder: every spread and total strike Kalshi lists for this game, with
 * its price, the model's probability, and the edge net of fees.
 *
 * Deliberately below the five-slot verdict, never above it. One predicted
 * total implies a probability at every strike, so a ladder is a single
 * disagreement with the market repeated sixty times -- sorted, it reads as
 * sixty independent opportunities. The engine's `edge_note` says rank on
 * `adjusted_edge` (scaled by measured skill) and treat same-direction strikes
 * as one position; both are honoured here, and the suspect flag is shown as
 * a warning rather than hidden.
 */
export function LadderCard({ league, gameId }: { league: string; gameId: string }) {
  const { data, error, status, loading } = useAsync(
    (signal) => api.edges(league, gameId, signal),
    [league, gameId],
  )

  if (loading) return <Skeletons n={2} />
  if (error) return <Failed error={error} status={status} />
  if (!data) return null
  if (data.rungs.length === 0) {
    return (
      <Card title="Ladder">
        <div className="note">{data.note}</div>
      </Card>
    )
  }

  const spread = data.rungs.filter((r) => r.kind === 'spread')
  const total = data.rungs.filter((r) => r.kind === 'total')
  const best = new Set(data.best_bets)

  return (
    <Card title={`Ladder -- ${data.rungs.length} strikes, ${data.position_size_contracts} contracts`}>
      <div className="note">
        Model: spread {fmtSigned(data.predicted_spread_home_relative)} (home-relative), total{' '}
        {data.predicted_total !== null ? Math.round(data.predicted_total) : '--'}. As of{' '}
        {data.as_of_local}.
      </div>
      {data.ladder_warnings?.length ? (
        <div className="banner" style={{ marginTop: 10 }}>
          <span>
            <b>One position, not many.</b> {data.ladder_warnings.join(' ')}
          </span>
        </div>
      ) : null}

      {spread.length > 0 ? <Rungs title="Spread" rows={spread} best={best} /> : null}
      {total.length > 0 ? <Rungs title="Total" rows={total} best={best} /> : null}

      <div className="note" style={{ marginTop: 12 }}>
        {data.edge_note}
      </div>
    </Card>
  )
}

function Rungs({ title, rows, best }: { title: string; rows: EdgeRung[]; best: Set<string> }) {
  return (
    <div className="ladder">
      <div className="ladder-h">{title}</div>
      <table className="ladder-t">
        <thead>
          <tr>
            <th>Strike</th>
            <th className="num">Ask</th>
            <th className="num">All-in</th>
            <th className="num">Model</th>
            <th className="num">Edge</th>
            <th className="num">Adj.</th>
            <th className="num">Vol</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr
              key={r.ticker}
              className={`${r.suspect ? 'suspect' : ''} ${best.has(r.ticker) ? 'best' : ''}`}
              title={r.notes.join(' ')}
            >
              <td>
                <span className="ladder-title">{r.title}</span>
                {r.suspect ? <span className="badge no" style={{ marginLeft: 6 }}>suspect</span> : null}
                {best.has(r.ticker) ? <span className="badge yes" style={{ marginLeft: 6 }}>best</span> : null}
              </td>
              <td className="num">{cents(r.ask)}</td>
              <td className="num">{cents(r.all_in_cost)}</td>
              <td className="num">{pct(r.model_probability)}</td>
              <td className={`num ${edgeClass(r.net_edge)}`}>{pct(r.net_edge, true)}</td>
              <td className={`num ${edgeClass(r.adjusted_edge)}`}>{pct(r.adjusted_edge, true)}</td>
              <td className="num muted">{r.volume !== null ? usdExact(r.volume) : '--'}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function edgeClass(v: number | null): string {
  if (v === null) return ''
  if (v >= 0.03) return 'pos'
  if (v <= -0.03) return 'neg'
  return ''
}

function fmtSigned(v: number | null): string {
  if (v === null) return '--'
  const r = Math.round(v * 2) / 2
  return r > 0 ? `+${r}` : `${r}`
}
