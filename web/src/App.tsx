import { useMemo, useState } from 'react'
import { api, useAsync } from './api'
import { prettyDate, shiftDate } from './format'
import { GameDetail } from './components/GameDetail'
import { GameList } from './components/GameList'
import { ModelPage } from './components/ModelPage'
import { Failed, Skeletons } from './components/bits'
import type { GameRow, GamesResponse, PricesResponse } from './types'

/**
 * Lay live prices over the slate. The slate arrives first, from the database,
 * carrying the agents' last saved prices; this swaps in live ones per game as
 * soon as Kalshi answers, so the page is usable before the slow part ends.
 */
function withLivePrices(slate: GamesResponse, live: PricesResponse | null): GamesResponse {
  if (!live || live.league !== slate.league || live.date !== slate.date) return slate
  return {
    ...slate,
    games: slate.games.map((g) => ({ ...g, ...(live.prices[g.game_id] ?? {}) })),
    unmatched_kalshi_events: live.unmatched_kalshi_events,
    unmatched_count: live.unmatched_count,
    prices_source: 'live',
    prices_as_of: live.as_of,
  }
}

const LEAGUES = [
  { key: 'nfl', label: 'NFL' },
  { key: 'nba', label: 'NBA' },
  { key: 'ncaaf', label: 'NCAAF' },
  { key: 'ncaamb', label: 'NCAAMB' },
] as const

/**
 * The date the app opens on comes from the server, not the browser.
 *
 * This looks pedantic and is not: "today" here means today in Central Time,
 * and after 6pm CST the browser's UTC date has already rolled over. A
 * browser-derived default would open on tomorrow's empty slate while tonight's
 * games were still being played. The server knows the Central date because
 * v1's localtime module is the single clock.
 */
export default function App() {
  const [league, setLeague] = useState<string>('nfl')
  const [date, setDate] = useState<string | null>(null)
  const [picked, setPicked] = useState<GameRow | null>(null)
  // 'model' shows the Model page for the selected league.
  const [view, setView] = useState<'slate' | 'model'>('slate')

  const health = useAsync(() => api.health(), [])
  const today = health.data?.today ?? null
  const activeDate = date ?? today

  const games = useAsync(
    (signal) =>
      activeDate
        ? api.games(league, activeDate, signal)
        : // Until the server has told us what today is, there is nothing
          // sensible to ask for. Resolving empty keeps the skeleton showing
          // rather than firing a request for the wrong day.
          new Promise<never>(() => {}),
    [league, activeDate],
  )

  // Fetched alongside the slate, not after it: the two run in parallel and
  // whichever is slower (almost always Kalshi) no longer holds up the other.
  const prices = useAsync(
    (signal) =>
      activeDate ? api.prices(league, activeDate, signal) : new Promise<never>(() => {}),
    [league, activeDate],
  )
  const slate = useMemo(
    () => (games.data ? withLivePrices(games.data, prices.data) : null),
    [games.data, prices.data],
  )

  const tz = health.data?.timezone ?? 'CT'

  function pickLeague(key: string) {
    setLeague(key)
    setPicked(null)
  }

  function nudge(days: number) {
    if (!activeDate) return
    setDate(shiftDate(activeDate, days))
    setPicked(null)
  }

  return (
    <div className="app">
      <header className="hdr">
        <div className="hdr-in">
          <div className="brand">
            <span className="brand-dot" />
            thugsak
          </div>

          <nav className="tabs" role="tablist" aria-label="League">
            {LEAGUES.map((l) => (
              <button
                key={l.key}
                className="tab"
                role="tab"
                aria-selected={league === l.key}
                onClick={() => pickLeague(l.key)}
              >
                {l.label}
              </button>
            ))}
          </nav>

          <nav className="tabs" aria-label="View">
            <button
              className="tab"
              aria-pressed={view === 'model'}
              aria-selected={view === 'model'}
              onClick={() => {
                setView(view === 'model' ? 'slate' : 'model')
                setPicked(null)
              }}
            >
              Model
            </button>
          </nav>

          <div className="hdr-sp" />

          <div className="hdr-meta">
            <span>All times {tz}</span>
            {health.data ? <span>data: {health.data.provider}</span> : null}
          </div>
        </div>
      </header>

      <main className="main">
        {health.error ? (
          <Failed
            error={`${health.error} -- is the API running? Start it with: uvicorn api.main:app --reload`}
            status={health.status}
          />
        ) : null}

        {view === 'model' ? <ModelPage league={league} /> : null}

        {view === 'slate' && !picked && activeDate ? (
          <div className="bar">
            <div className="datenav">
              <button onClick={() => nudge(-1)} aria-label="Previous day">
                &larr;
              </button>
              <input
                type="date"
                value={activeDate}
                onChange={(e) => {
                  if (e.target.value) {
                    setDate(e.target.value)
                    setPicked(null)
                  }
                }}
                aria-label="Slate date"
              />
              <button
                onClick={() => nudge(1)}
                aria-label="Next day"
              >
                &rarr;
              </button>
            </div>
            <span className="count">{prettyDate(activeDate)}</span>
            {today && activeDate !== today ? (
              <button
                className="tab"
                onClick={() => {
                  setDate(null)
                  setPicked(null)
                }}
              >
                Today
              </button>
            ) : null}
            {today && activeDate < today ? (
              <span className="badge small">Lookback</span>
            ) : null}
          </div>
        ) : null}

        {view === 'model' ? null : picked ? (
          <GameDetail league={league} game={picked} onBack={() => setPicked(null)} />
        ) : games.loading ? (
          <Skeletons />
        ) : games.error ? (
          <Failed error={games.error} status={games.status} />
        ) : slate ? (
          <GameList
            data={slate}
            pricesLoading={prices.loading}
            pricesError={prices.error}
            onPick={setPicked}
          />
        ) : null}
      </main>
    </div>
  )
}
