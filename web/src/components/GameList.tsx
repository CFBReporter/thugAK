import { cents, spread, timeOf } from '../format'
import type { GameRow, GamesResponse } from '../types'
import { Empty } from './bits'

/**
 * One row per game, in the shape a prediction-market screen uses: who is
 * playing on the left, the numbers that would make you click on the right.
 *
 * Both win prices come from the server already resolved to a side
 * (`home_win_price` / `away_win_price`). They are deliberately not derived
 * here from `kalshi_winner_prices`, which is keyed by ticker: Kalshi's team
 * codes and the store's team ids disagree -- WAS against WSH, for one -- so
 * matching a ticker suffix in the browser would quietly show the wrong team's
 * price, or none. Side resolution belongs with the alias table.
 */
function Row({ game, onPick }: { game: GameRow; onPick: (g: GameRow) => void }) {
  const [awayScore, homeScore] = (game.score ?? '').split('-')
  const aw = awayScore ? Number(awayScore) : null
  const hm = homeScore ? Number(homeScore) : null

  return (
    <button className="game" onClick={() => onPick(game)}>
      <div className="game-teams">
        <div className="team-line">
          {game.completed && aw !== null ? (
            <span className={`team-score ${hm !== null && aw > hm ? 'won' : ''}`}>{aw}</span>
          ) : null}
          <span className="team-name">{game.away_name}</span>
        </div>
        <div className="team-line">
          {game.completed && hm !== null ? (
            <span className={`team-score ${aw !== null && hm > aw ? 'won' : ''}`}>{hm}</span>
          ) : (
            <span className="at">AT</span>
          )}
          <span className="team-name">{game.home_name}</span>
        </div>
      </div>

      <div className="game-right">
        <div className="when">
          {game.completed ? (
            <span className="final">FINAL</span>
          ) : (
            timeOf(game.start_local)
          )}
        </div>

        <div className="odds">
          <div className="odds-cell">
            <span className="k">{game.away_label} win</span>
            <span className="v">{cents(game.away_win_price)}</span>
          </div>
          <div className="odds-cell">
            <span className="k">{game.home_label} win</span>
            <span className="v">{cents(game.home_win_price)}</span>
          </div>
          <div className="odds-cell">
            <span className="k">Spread</span>
            <span className="v">{spread(game.book_spread_home_relative)}</span>
          </div>
          <div className="odds-cell">
            <span className="k">Total</span>
            <span className="v">{game.book_total ?? '--'}</span>
          </div>
        </div>

        <div className="vol">
          {game.kalshi_markets > 0 ? `${game.kalshi_markets} mkts` : 'no book'}
        </div>
      </div>
    </button>
  )
}

export function GameList({
  data,
  onPick,
}: {
  data: GamesResponse
  onPick: (g: GameRow) => void
}) {
  if (data.games.length === 0) {
    return (
      <Empty title="No games on this date">
        Nothing is scheduled in this league on {data.date}. Try another date -- past dates
        work too, and show what the model would have said before kickoff.
      </Empty>
    )
  }

  const withMarkets = data.games.filter((g) => g.kalshi_markets > 0).length

  return (
    <>
      <div className="title-row">
        <h1>{data.games.length} games</h1>
        <span className="count">
          {withMarkets} with Kalshi markets
          {data.unmatched_count > 0 ? ` -- ${data.unmatched_count} unmatched events` : ''}
        </span>
      </div>

      {withMarkets < data.games.length ? (
        <div className="banner">
          <span>
            <b>{data.games.length - withMarkets} game(s) have no Kalshi market.</b> That is
            Kalshi not having listed them, not a name-matching failure -- unmatched events
            are counted separately and there are {data.unmatched_count}. A game with no
            market can still be predicted; there is just nothing to bet against.
          </span>
        </div>
      ) : null}

      <div className="games">
        {data.games.map((g) => (
          <Row key={g.game_id} game={g} onPick={onPick} />
        ))}
      </div>
    </>
  )
}
