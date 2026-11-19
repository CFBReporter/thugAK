import { useEffect, useRef, useState } from 'react'
import type {
  EdgesResponse,
  GamesResponse,
  HealthResponse,
  Accuracy,
  GradedGame,
  LeaguesResponse,
  ModelAccuracy,
  ModelOverview,
  ModelRun,
  PricesResponse,
  RecommendationResponse,
} from './types'

/** Thrown with the server's own explanation, so the UI can show the reason. */
export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message)
    this.name = 'ApiError'
  }
}

async function get<T>(path: string, params: Record<string, string | undefined>, signal?: AbortSignal): Promise<T> {
  const qs = new URLSearchParams()
  for (const [k, v] of Object.entries(params)) if (v !== undefined) qs.set(k, v)
  const url = `/api/${path}${qs.size ? `?${qs}` : ''}`

  const res = await fetch(url, { signal })
  if (!res.ok) {
    // FastAPI puts the message in `detail`. For a 503 that message is the
    // provider explaining why it is unusable, which is worth showing verbatim
    // rather than replacing with "something went wrong".
    let detail = `${res.status} ${res.statusText}`
    try {
      const body = (await res.json()) as { detail?: string }
      if (body.detail) detail = body.detail
    } catch {
      /* non-JSON error body; the status line is all we have */
    }
    throw new ApiError(detail, res.status)
  }
  return (await res.json()) as T
}

export const api = {
  health: () => get<HealthResponse>('health', {}),
  leagues: () => get<LeaguesResponse>('leagues', {}),
  games: (league: string, date?: string, signal?: AbortSignal) =>
    get<GamesResponse>('games', { league, date }, signal),
  /** Live Kalshi prices for the slate; the slate itself renders before these. */
  prices: (league: string, date?: string, signal?: AbortSignal) =>
    get<PricesResponse>('prices', { league, date }, signal),
  recommendation: (league: string, gameId: string, signal?: AbortSignal) =>
    get<RecommendationResponse>('recommendation', { league, game_id: gameId }, signal),
  model: (league?: string, signal?: AbortSignal) =>
    get<{ leagues: ModelOverview[] }>('model', { league }, signal),
  modelLog: (league?: string, signal?: AbortSignal) =>
    get<{ runs: ModelRun[] }>('model/log', { league }, signal),
  modelRecord: (league?: string, signal?: AbortSignal) =>
    get<{ games: GradedGame[]; summary: Accuracy }>('model/record', { league }, signal),
  modelAccuracy: (league: string, signal?: AbortSignal) =>
    get<ModelAccuracy>('model/accuracy', { league }, signal),
  /** Every ladder strike for a game, scored against the model. */
  edges: (league: string, gameId: string, signal?: AbortSignal) =>
    get<EdgesResponse>('edges', { league, game_id: gameId }, signal),
}

export interface Async<T> {
  data: T | null
  error: string | null
  /** The HTTP status, so the UI can distinguish "blocked" from "broken". */
  status: number | null
  loading: boolean
}

/**
 * Fetch on dependency change, aborting the previous request.
 *
 * The abort matters more than it looks: clicking through a slate fires a
 * request per game, and without it a slow early response can land after a
 * fast later one and overwrite the game you are actually looking at.
 */
export function useAsync<T>(
  fn: (signal: AbortSignal) => Promise<T>,
  deps: readonly unknown[],
): Async<T> {
  const [state, setState] = useState<Async<T>>({
    data: null,
    error: null,
    status: null,
    loading: true,
  })
  // `fn` is a fresh closure every render; holding it in a ref keeps it out of
  // the dependency list so the effect runs when the inputs change, not on
  // every render.
  const fnRef = useRef(fn)
  fnRef.current = fn

  useEffect(() => {
    const ctrl = new AbortController()
    setState({ data: null, error: null, status: null, loading: true })
    fnRef.current(ctrl.signal).then(
      (data) => {
        if (!ctrl.signal.aborted) setState({ data, error: null, status: 200, loading: false })
      },
      (err: unknown) => {
        if (ctrl.signal.aborted) return
        const status = err instanceof ApiError ? err.status : null
        const message =
          err instanceof Error ? err.message : 'Request failed for an unknown reason.'
        setState({ data: null, error: message, status, loading: false })
      },
    )
    return () => ctrl.abort()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps)

  return state
}
