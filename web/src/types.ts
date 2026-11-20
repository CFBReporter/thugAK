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
  /** Already resolved to a side by the server; see GameList for why. */
  home_win_price: number | null
  away_win_price: number | null
  has_ladders: boolean
  /** How many sportsbook lines moved significantly since first seen. */
  line_moves?: number
}

export interface GamesResponse {
  league: string
  date: string
  timezone: string
  games: GameRow[]
  unmatched_kalshi_events: unknown[]
  unmatched_count: number
  /** Where the price fields came from: live Kalshi, the agents' last saved
   * copy, or nothing yet. */
  prices_source: 'live' | 'snapshot' | 'none'
  /** UTC ISO time the prices were taken, when known. */
  prices_as_of: string | null
  note: string
}

/** The Kalshi fields of a slate row, as /api/prices returns them per game. */
export type PriceFields = Pick<
  GameRow,
  'kalshi_markets' | 'kalshi_winner_prices' | 'home_win_price' | 'away_win_price' | 'has_ladders'
>

export interface PricesResponse {
  league: string
  date: string
  as_of: string
  source: 'live'
  prices: Record<string, PriceFields>
  unmatched_kalshi_events: unknown[]
  unmatched_count: number
}

export interface EfficiencyTeam {
  adj_o: number
  adj_d: number
  net: number
  adj_t: number
  /** Average net rating of opponents faced (points per 100 possessions). */
  sos: number
  games: number
  ranks: { off?: number; def?: number; net?: number; pace?: number; sos?: number }
  /** How many teams the ranks are out of. */
  of: number
}

export interface EfficiencyModel {
  home_points: number
  away_points: number
  possessions: number
  home: EfficiencyTeam
  away: EfficiencyTeam
  ratings_games: number
  /** League-average possessions per game, for comparing this game's pace. */
  avg_tempo: number
}

export interface LineMove {
  sportsbook: string
  market: 'moneyline' | 'spread' | 'total'
  selection: string
  start: string
  end: string
  change: string
  note: string
  first_seen_at: string
  last_seen_at: string
}

/** A recommended bet that loses if the predicted score holds. */
export interface Longshot {
  label: string
  bet: string
  kalshi_ticker: string | null
  price: number | null
  all_in_cost_after_fees: number | null
  model_probability: number | null
  edge_after_fees: number | null
  why: string
}

export interface Bet {
  /** Recommended, but loses if the predicted score holds. */
  longshot?: boolean
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
  /** Keyed by side; `predicted_points` is keyed by display name. */
  predicted_away_points: number | null
  predicted_home_points: number | null
  predicted_winner: string
  win_probability: number | null
  predicted_margin: number | null
  predicted_total: number | null
  confidence_in_prediction: string
  based_on: string[]
  versus_the_sportsbook: string | null
  prediction_cautions: string[]

  the_call: string
  /** Sportsbook lines that moved far from where they were first seen. */
  line_moves?: LineMove[]
  /** College basketball: the tempo-free (KenPom-style) efficiency projection. */
  efficiency_model?: EfficiencyModel | null
  /** Value bets against the predicted outcome, kept out of the call. */
  longshots?: Longshot[]
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
  /** The model that made this prediction, and the steps it took. */
  model?: ModelBlock
  bets_graded?: string
  lookback_note?: string
  lookback_caveats?: string[]
}

/** One strike on a spread or total ladder, scored against the model. */
export interface EdgeRung {
  ticker: string
  kind: 'spread' | 'total' | string
  title: string
  strike: number | null
  subject_team: string | null
  /** Dollars per contract; null when the market has no ask. */
  ask: number | null
  fee_per_contract: number | null
  all_in_cost: number | null
  model_probability: number | null
  /** Raw arithmetic. */
  net_edge: number | null
  /** Scaled by measured model skill -- rank on this one. */
  adjusted_edge: number | null
  ev_per_100_contracts_usd: number | null
  volume: number | null
  /** An edge too good to be true is flagged, not promoted. */
  suspect: boolean
  notes: string[]
}

export interface EdgesResponse {
  game_id: string
  league: string
  as_of: string
  as_of_local: string
  predicted_spread_home_relative: number | null
  predicted_total: number | null
  position_size_contracts: number
  fee_multipliers: Record<string, number>
  model_skill: unknown
  correlated_ladders: unknown
  ladder_warnings: string[] | null
  edge_note: string
  rungs: EdgeRung[]
  best_bets: string[]
  arbitrage: unknown
  warnings: string[]
  note: string
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
  /** Static site only: when the data files were built (UTC ISO). */
  built_at?: string
  static?: boolean
}

// ---- the model (Model page + the label on each prediction) ----------------

export interface Accuracy {
  n: number
  mae_margin?: number
  mae_total?: number
  mae_points?: number
  winner_pct?: number | null
  book_mae_margin?: number | null
  book_mae_total?: number | null
  book_n?: number
}

export interface Adjustment {
  label: string
  home: number
  away: number
}

/** Which model made a prediction, and how it got from the engine to the score. */
export interface ModelBlock {
  version: string
  live_since: string | null
  avg_miss_margin: number | null
  avg_miss_total: number | null
  avg_miss_basis: string | null
  base_points: { home: number | null; away: number | null }
  adjustments: Adjustment[]
  final_points: { home: number | null; away: number | null }
}

export interface ModelOverview {
  league: string
  version: string
  live_since: string | null
  trained_on_games: number | null
  uses_correction: boolean
  holdout: Accuracy | null
  holdout_book: { n: number; mae_margin?: number; mae_total?: number } | null
  previous: Accuracy | null
  previous_version: string | null
  live_4_weeks: Accuracy
  live_all: Accuracy
  calibration_fitted_at: string | null
}

export interface ModelRun {
  league: string
  version: string
  trained_at: string
  promoted: boolean
  note: string | null
  changes: { settings: string[]; learned: string[] }
  holdout_games: number | null
  holdout_from: string | null
  candidate: Accuracy | null
  champion: Accuracy | null
  champion_version: string | null
  book: { n: number; mae_margin?: number; mae_total?: number } | null
  gain_pct: number | null
}

export interface GradedGame {
  game_id: string
  league: string
  date: string
  matchup: string | null
  version: string
  predicted: [number, number]
  final: [number, number]
  miss_margin: number
  miss_total: number
  miss_points: number
  right_winner: boolean
  decided: boolean
  book_miss_margin: number | null
  book_miss_total: number | null
}

export interface AccuracyWeek extends Accuracy {
  week: string
}

export interface ModelAccuracy {
  league: string
  live: AccuracyWeek[]
  replayed: unknown[]
}
