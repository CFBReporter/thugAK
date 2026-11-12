// Display formatting. Kept apart from components so the rules are stated once.
//
// A standing rule from v1 carries over: never invent precision the model does
// not have. A predicted score is shown whole, because the model's own typical
// error is around 13 points on a margin -- a tenth of a point there reads as
// accuracy that is not present. Prices are the opposite case and are shown to
// the cent, because a cent is real money at stake.

/** Kalshi prices are dollars in [0,1]; the UI shows cents, as Kalshi does. */
export function cents(p: number | null | undefined): string {
  if (p === null || p === undefined) return '--'
  return `${Math.round(p * 100)}¢`
}

/** Dollars, compact above a thousand: $1.5M, $412K, $780. */
export function usd(n: number | null | undefined): string {
  if (n === null || n === undefined) return '--'
  const a = Math.abs(n)
  if (a >= 1_000_000) return `$${(n / 1_000_000).toFixed(a >= 10_000_000 ? 0 : 1)}M`
  if (a >= 1_000) return `$${(n / 1_000).toFixed(a >= 10_000 ? 0 : 1)}K`
  return `$${Math.round(n)}`
}

/** Exact dollars with separators, for the one place the full figure matters. */
export function usdExact(n: number | null | undefined): string {
  if (n === null || n === undefined) return '--'
  return `$${n.toLocaleString('en-US', { maximumFractionDigits: 0 })}`
}

/** A probability or edge as a percentage. Edges are signed on purpose. */
export function pct(x: number | null | undefined, signed = false): string {
  if (x === null || x === undefined) return '--'
  const v = x * 100
  const s = `${Math.abs(v) < 10 ? v.toFixed(1) : Math.round(v)}%`
  return signed && v > 0 ? `+${s}` : s
}

/** Whole points. See the note above on false precision. */
export function points(x: number | null | undefined): string {
  if (x === null || x === undefined) return '--'
  return String(Math.round(x))
}

/** A spread as a bettor reads it: "-3.5" for the favourite. */
export function spread(x: number | null | undefined): string {
  if (x === null || x === undefined) return '--'
  return x > 0 ? `-${x}` : `+${Math.abs(x)}`
}

/**
 * The time portion of the engine's already-localised string.
 *
 * `start_local` arrives from Python as e.g. "Thu Nov 12, 7:15 PM CST" -- it is
 * already Central, which is the whole point of v1's localtime module. The
 * browser deliberately does NOT re-derive it from `start_time`: the viewer's
 * machine may be in any zone, and re-localising would silently show a
 * different day from the one the slate was selected by.
 */
export function timeOf(startLocal: string): string {
  const i = startLocal.indexOf(', ')
  return i === -1 ? startLocal : startLocal.slice(i + 2)
}

/** ISO date shifted by whole days, staying on the calendar the user picked. */
export function shiftDate(iso: string, days: number): string {
  const [y, m, d] = iso.split('-').map(Number)
  // Constructed as UTC midday so a DST boundary cannot roll the date back a
  // day. The autumn fold makes a local-midnight date arithmetic off by one.
  const dt = new Date(Date.UTC(y ?? 1970, (m ?? 1) - 1, d ?? 1, 12))
  dt.setUTCDate(dt.getUTCDate() + days)
  return dt.toISOString().slice(0, 10)
}

export function prettyDate(iso: string): string {
  const [y, m, d] = iso.split('-').map(Number)
  const dt = new Date(Date.UTC(y ?? 1970, (m ?? 1) - 1, d ?? 1, 12))
  return dt.toLocaleDateString('en-US', {
    weekday: 'short',
    month: 'short',
    day: 'numeric',
    timeZone: 'UTC',
  })
}
