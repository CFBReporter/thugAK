// Shapes returned by the FastAPI bridge, which forwards `thugsak.mcp.tools`
// verbatim. Field names are the engine's, not re-mapped: a rename here would
// be a second place to keep in sync, and a silent source of undefined.
//
// Optional means "the engine can legitimately omit this" -- a game with no
// Kalshi market has no prices, a pre-game view has no result. Those are
// normal states, so the UI renders them rather than treating them as errors.

export type Recommendation = 'BET' | 'SMALL' | 'NO'

export type BetKind = 'moneyline' | 'spread' | 'total'

export interface GameRow {
  game_id: string
  start_time: string
  start_local: string
  home: string
  away: string
  home_label: string
  away_label: string
  home_name: string
  away_name: string
  completed: boolean
  /** "away-home", e.g. "20-24". Null until the game has a score. */
  score: string | null
  /** Positive means the home team is favoured. */
  book_spread_home_relative: number | null
  book_total: number | null
  kalshi_markets: number
  kalshi_winner_prices: Record<string, number | null>
  has_ladders: boolean
}

export interface GamesResponse {
  league: string
  date: string
  timezone: string
  games: GameRow[]
  unmatched_kalshi_events: unknown[]
  unmatched_count: number
  note: string
}

export interface Bet {
  slot: string
  label: string
  recommendation: Recommendation
  /** Null when the slot is not actionable -- there is nothing to place. */
  bet: string | null
  why: string
  kalshi_ticker: string | null
  price: number | null
  all_in_cost_after_fees: number | null
  model_probability: number | null
  sportsbook_probability: number | null
  edge_after_fees: number | null
  edge_required_at_this_money: number | null
  dollars_traded_on_this_market: number
  money_note: string | null
  money_flow: string | null
  cautions: string[]
  /** Present only in lookback mode. */
  outcome?: 'WON' | 'LOST' | null
  profit_per_100_contracts_usd?: number | null
}

export interface KalshiMoney {
  total_traded_usd: number
  open_positions_usd: number
  by_bet_type_usd: Record<string, number>
  markets_counted: number
  busiest_market: string | null
  busiest_market_usd: number
  tier: string
  summary: string
  note?: string | null
}

export interface LookbackResult {
  actual_score: string
  actual_margin_home_relative: number | null
  actual_total: number | null
  margin_error?: number
  margin_error_abs?: number
  picked_the_winner?: boolean
  total_error?: number
  total_error_abs?: number
  note: string
}

export interface BestBet {
  label: string
  bet: string
  kalshi_ticker: string | null
  price: number | null
  all_in_cost_after_fees: number | null
  edge_after_fees: number | null
  edge_required_at_this_money: number | null
  stake: string
  why: string
}

export interface RecommendationResponse {
  game_id: string
  league: string
  matchup: string
  as_of: string
  as_of_local: string
  start_local: string

  predicted_score: string
  predicted_result: string
  predicted_points: Record<string, number | null>
  predicted_winner: string
  win_probability: number | null
  predicted_margin: number | null
  predicted_total: number | null
  confidence_in_prediction: string
  based_on: string[]
  versus_the_sportsbook: string | null
  prediction_cautions: string[]

  the_call: string
  best_bet: BestBet | null
  headline: string
  bets: Bet[]

  kalshi_money: KalshiMoney
  sportsbook_line_source: string | null
  market_census: unknown
  census_note?: string | null
  model_skill: {
    spread_and_moneyline: number | null
    total: number | null
    note: string
  }
  warnings: string[]
  scope?: string | null

  mode: 'lookback' | 'pre-game'
  result?: LookbackResult
  bets_graded?: string
  lookback_note?: string
  lookback_caveats?: string[]
}

export interface League {
  key: string
  label: string
}

export interface LeaguesResponse {
  leagues: League[]
  timezone: string
  today: string
}

export interface ProviderInfo {
  name: string
  leagues: string[]
  blocked_reason: string | null
}

export interface HealthResponse {
  healthy: boolean
  provider: string
  providers: ProviderInfo[]
  today: string
  timezone: string
}
