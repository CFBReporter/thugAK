import { useMemo, useState } from 'react'
import { api, useAsync } from '../api'
import type { Accuracy, AccuracyWeek, GradedGame, ModelOverview, ModelRun } from '../types'
import { Card, Empty, Failed, Skeletons } from './bits'

/**
 * The model, in the open: which version is live, how accurate it is, every
 * weekly training run (including the ones that were kept out), and every
 * graded prediction. The goal the page is built around is score accuracy --
 * how many points the predictions miss by -- with the sportsbook line shown
 * beside it as a yardstick, never as the target.
 */

const SERIES = {
  model: { name: 'Model', color: '#5b8cff', dash: undefined },
  book: { name: 'Sportsbook line', color: '#cf5aa8', dash: '5 4' },
} as const

function pts(x: number | null | undefined, digits = 1): string {
  return x === null || x === undefined ? '--' : x.toFixed(digits)
}

function pctOf(x: number | null | undefined): string {
  return x === null || x === undefined ? '--' : `${Math.round(x * 100)}%`
}

function when(iso: string | null): string {
  if (!iso) return '--'
  return new Date(iso).toLocaleDateString('en-US', {
    weekday: 'short',
    month: 'short',
    day: 'numeric',
    timeZone: 'America/Chicago',
  })
}

function Stat({ k, v, sub }: { k: string; v: string; sub?: string }) {
  return (
    <div className="m-stat">
      <span className="k">{k}</span>
      <span className="v">{v}</span>
      {sub ? <span className="sub">{sub}</span> : null}
    </div>
  )
}

function Current({ o }: { o: ModelOverview }) {
  const live = o.live_4_weeks
  const held = o.holdout
  return (
    <Card title={`${o.league.toUpperCase()} -- model ${o.version}`}>
      <div className="m-line">
        {o.live_since
          ? `Live since ${when(o.live_since)} · trained on ${o.trained_on_games?.toLocaleString() ?? '--'} games`
          : 'The engine as first calibrated -- no trained version has beaten it yet'}
        {o.live_since ? (o.uses_correction ? ' · with learned corrections' : ' · refitted settings only') : ''}
      </div>
      <div className="m-stats">
        <Stat
          k="Avg miss, margin"
          v={`${pts(live.mae_margin)} pts`}
          sub={live.n ? `live, last 4 weeks (${live.n} games)` : 'no graded games yet'}
        />
        <Stat k="Avg miss, total" v={`${pts(live.mae_total)} pts`} sub="live, last 4 weeks" />
        <Stat k="Right winner" v={pctOf(live.winner_pct)} sub="live, last 4 weeks" />
        <Stat
          k="Sportsbook line, same games"
          v={`${pts(live.book_mae_margin)} / ${pts(live.book_mae_total)}`}
          sub={live.book_n ? `margin / total, ${live.book_n} games with a line` : 'no lines stored'}
        />
      </div>
      {held && held.n ? (
        <div className="m-line faint">
          On {held.n} replayed recent games it misses the margin by {pts(held.mae_margin)} and the
          total by {pts(held.mae_total)}
          {o.previous && o.previous_version
            ? ` (${o.previous_version} was ${pts(o.previous.mae_margin)} / ${pts(o.previous.mae_total)})`
            : ''}
          .
        </div>
      ) : null}
    </Card>
  )
}

/** Weekly average margin miss: the model against the sportsbook line. */
function AccuracyChart({ weeks }: { weeks: AccuracyWeek[] }) {
  const [hover, setHover] = useState<number | null>(null)
  const pts2 = weeks.filter((w) => w.n > 0)
  const first = pts2[0]
  const lastWeek = pts2[pts2.length - 1]
  if (!first || !lastWeek || pts2.length < 2) {
    return (
      <div className="m-line faint">
        {first
          ? `One graded week so far (${first.n} games, margin miss ${pts(first.mae_margin)} pts). The trend line appears after a second week.`
          : 'No graded games yet. Predictions are graded when their games finish.'}
      </div>
    )
  }
  const W = 640
  const H = 220
  const pad = { l: 40, r: 110, t: 12, b: 28 }
  const vals = pts2.flatMap((w) => [w.mae_margin ?? 0, w.book_mae_margin ?? w.mae_margin ?? 0])
  const yMax = Math.ceil((Math.max(...vals) + 1) / 5) * 5
  const x = (i: number) => pad.l + (i * (W - pad.l - pad.r)) / (pts2.length - 1)
  const y = (v: number) => pad.t + (1 - v / yMax) * (H - pad.t - pad.b)
  const line = (get: (w: AccuracyWeek) => number | null | undefined) =>
    pts2
      .map((w, i) => [i, get(w)] as const)
      .filter(([, v]) => v !== null && v !== undefined)
      .map(([i, v], j) => `${j ? 'L' : 'M'}${x(i).toFixed(1)},${y(v as number).toFixed(1)}`)
      .join(' ')
  const ticks = [0, yMax / 2, yMax]
  const last = pts2.length - 1
  const h = hover !== null ? pts2[hover] ?? null : null

  return (
    <div className="m-chart">
      <div className="m-legend" aria-hidden="true">
        {Object.values(SERIES).map((s) => (
          <span key={s.name}>
            <svg width="22" height="8">
              <line x1="1" y1="4" x2="21" y2="4" stroke={s.color} strokeWidth="2" strokeDasharray={s.dash} />
            </svg>
            {s.name}
          </span>
        ))}
      </div>
      <svg
        viewBox={`0 0 ${W} ${H}`}
        role="img"
        aria-label="Average margin miss by week, model versus sportsbook line"
        onMouseLeave={() => setHover(null)}
      >
        {ticks.map((t) => (
          <g key={t}>
            <line x1={pad.l} x2={W - pad.r} y1={y(t)} y2={y(t)} className="m-grid" />
            <text x={pad.l - 6} y={y(t) + 4} textAnchor="end" className="m-axis">
              {t}
            </text>
          </g>
        ))}
        <path d={line((w) => w.book_mae_margin)} fill="none" stroke={SERIES.book.color} strokeWidth="2" strokeDasharray={SERIES.book.dash} />
        <path d={line((w) => w.mae_margin)} fill="none" stroke={SERIES.model.color} strokeWidth="2" />
        {pts2.map((w, i) =>
          w.mae_margin !== undefined ? (
            <circle key={w.week} cx={x(i)} cy={y(w.mae_margin)} r="4" fill={SERIES.model.color} stroke="var(--surface)" strokeWidth="2" />
          ) : null,
        )}
        {/* direct labels at the right end */}
        {lastWeek.mae_margin !== undefined ? (
          <text x={x(last) + 8} y={y(lastWeek.mae_margin) + 4} className="m-dlabel">
            Model {pts(lastWeek.mae_margin)}
          </text>
        ) : null}
        {lastWeek.book_mae_margin ? (
          <text x={x(last) + 8} y={y(lastWeek.book_mae_margin) + 16} className="m-dlabel">
            Line {pts(lastWeek.book_mae_margin)}
          </text>
        ) : null}
        {pts2.map((w, i) => (
          <text key={`x${w.week}`} x={x(i)} y={H - 8} textAnchor="middle" className="m-axis">
            {w.week.slice(5)}
          </text>
        ))}
        {hover !== null ? (
          <line x1={x(hover)} x2={x(hover)} y1={pad.t} y2={H - pad.b} className="m-cross" />
        ) : null}
        {pts2.map((w, i) => (
          <rect
            key={`h${w.week}`}
            x={x(i) - (W - pad.l - pad.r) / (pts2.length - 1) / 2}
            y={pad.t}
            width={(W - pad.l - pad.r) / (pts2.length - 1)}
            height={H - pad.t - pad.b}
            fill="transparent"
            onMouseEnter={() => setHover(i)}
          />
        ))}
      </svg>
      {h ? (
        <div className="m-tip">
          Week of {h.week}: model misses the margin by <b>{pts(h.mae_margin)}</b>, the line by{' '}
          <b>{pts(h.book_mae_margin)}</b> · {h.n} games · right winner {pctOf(h.winner_pct)}
        </div>
      ) : (
        <div className="m-tip faint">Average points the margin was missed by, per week. Lower is better.</div>
      )}
    </div>
  )
}

function Log({ runs }: { runs: ModelRun[] }) {
  if (!runs.length) {
    return (
      <div className="m-line faint">
        No training runs yet. The first runs on Monday morning; each one is listed here, including
        candidates that were not better and so were kept out.
      </div>
    )
  }
  return (
    <div className="m-log">
      {runs.map((r) => (
        <div key={`${r.league}-${r.version}-${r.trained_at}`} className="m-log-row">
          <div className="m-log-h">
            <span className="date">{when(r.trained_at)}</span>
            <span className={`badge ${r.promoted ? 'yes' : 'no'}`}>
              {r.version} {r.promoted ? 'PROMOTED' : 'KEPT OUT'}
            </span>
            <span className="lg">{r.league.toUpperCase()}</span>
          </div>
          {r.note ? <div className="m-line">{r.note}</div> : null}
          {r.changes.settings.length ? (
            <div className="m-line faint">Changed: {r.changes.settings.join(' · ')}</div>
          ) : null}
          {r.changes.learned.length ? (
            <ul className="m-learned">
              {r.changes.learned.map((l) => (
                <li key={l}>{l}</li>
              ))}
            </ul>
          ) : null}
        </div>
      ))}
    </div>
  )
}

function Record({ games }: { games: GradedGame[] }) {
  const [version, setVersion] = useState<string>('all')
  const versions = useMemo(() => Array.from(new Set(games.map((g) => g.version))).sort(), [games])
  const shown = version === 'all' ? games : games.filter((g) => g.version === version)
  if (!games.length) return <div className="m-line faint">No graded predictions yet.</div>
  return (
    <>
      {versions.length > 1 ? (
        <div className="m-filter">
          <label>
            Version{' '}
            <select value={version} onChange={(e) => setVersion(e.target.value)}>
              <option value="all">all</option>
              {versions.map((v) => (
                <option key={v}>{v}</option>
              ))}
            </select>
          </label>
        </div>
      ) : null}
      <div className="m-table-wrap">
        <table className="m-table">
          <thead>
            <tr>
              <th>Date</th>
              <th>Game</th>
              <th>Predicted</th>
              <th>Final</th>
              <th>Margin miss</th>
              <th>Total miss</th>
              <th>Winner</th>
              <th>Ver.</th>
            </tr>
          </thead>
          <tbody>
            {shown.slice(0, 200).map((g) => (
              <tr key={`${g.game_id}-${g.version}`}>
                <td>{g.date.slice(5)}</td>
                <td>{g.matchup ?? g.game_id}</td>
                <td>{g.predicted.join('-')}</td>
                <td>{g.final.join('-')}</td>
                <td>{pts(g.miss_margin)}</td>
                <td>{pts(g.miss_total)}</td>
                <td>{g.decided ? (g.right_winner ? '✓' : '✗') : '='}</td>
                <td>{g.version}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  )
}

export function ModelPage({ league }: { league: string }) {
  const overview = useAsync((s) => api.model(league, s), [league])
  const accuracy = useAsync((s) => api.modelAccuracy(league, s), [league])
  const log = useAsync((s) => api.modelLog(league, s), [league])
  const record = useAsync((s) => api.modelRecord(league, s), [league])

  if (overview.loading) return <Skeletons n={3} />
  if (overview.error) return <Failed error={overview.error} status={overview.status} />
  const o = overview.data?.leagues[0]
  if (!o) return <Empty title="No model data">Nothing is stored for this league yet.</Empty>
  const summary: Accuracy | undefined = record.data?.summary

  return (
    <div className="model-page">
      <Current o={o} />
      <Card title="Accuracy by week">
        {accuracy.data ? <AccuracyChart weeks={accuracy.data.live} /> : <Skeletons n={1} />}
      </Card>
      <Card title="Model log">{log.data ? <Log runs={log.data.runs} /> : <Skeletons n={1} />}</Card>
      <Card
        title={`Prediction record${summary && summary.n ? ` -- ${summary.n} graded, avg margin miss ${pts(summary.mae_margin)} pts` : ''}`}
      >
        {record.data ? <Record games={record.data.games} /> : <Skeletons n={2} />}
      </Card>
    </div>
  )
}
