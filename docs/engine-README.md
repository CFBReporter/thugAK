# thugsak

A local Streamlit app for reading Kalshi sports markets before you place a
bet. Pick a sport, see the day's games, click into one, and get a predicted
total and spread, every ladder strike priced against the model net of fees,
and a read on where the big money is.

**Read-only.** Every endpoint it uses is public, so there is no Kalshi API key
anywhere in this project and no code path that can place an order.

Leagues: NFL, NBA, NCAAF, NCAAMB.

---

## Setup

```bash
cd C:/Users/User/thugsak
py -3.14 -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt

# Load three seasons, fit the model, then prune the history.
.venv/Scripts/python.exe scripts/import_history.py --league all --seasons 3

.venv/Scripts/streamlit run app/streamlit_app.py
```

Python 3.14 is verified working, including pyarrow. Dependencies are pinned in
`requirements.txt` with nothing published inside a 7-day window.

The Claude write-up uses this machine's existing Claude Code credentials — no
API key to configure.

### Useful commands

```bash
.venv/Scripts/python.exe scripts/smoke.py              # endpoint health
.venv/Scripts/python.exe -m pytest tests/ -q           # the maths
.venv/Scripts/python.exe scripts/import_history.py --league nfl --no-prune
```

---

## How it works

### The output

Every game produces the same six things, in this order and nothing else:

```
Predicted score       Portland 104 - Oklahoma City 110
                      Oklahoma City by 6 (65% to win), 214 total points

Moneyline, away       BET / SMALL / NO
Moneyline, home       BET / SMALL / NO
Spread, away          BET / SMALL / NO
Spread, home          BET / SMALL / NO
Total, over or under  BET / SMALL / NO
```

Each row names one Kalshi market, its price, the dollars traded on it, and a
one-sentence reason. A NO occupies the same place a recommendation would, so
"nothing here" is an answer rather than an absence to notice.

The fixed shape is the point. The full ladder — sixty-odd strikes for a single
game — is one disagreement with the market repeated at every strike, and a
sorted table of it reads as sixty independent opportunities. It is still
available, one expander down, as evidence.

### How much money is on it

The dollars behind a price decide how much edge is worth acting on, because a
price is an estimate and its reliability scales with the capital behind it:

| Dollars traded | Edge needed | Reading |
|---|---|---|
| over $100k | 5% | heavily contested; the market is probably right |
| $10k–$100k | 4% | actively priced |
| $1k–$10k | 3% | moderately priced |
| under $1k | 3%, small stake | may be stale rather than wrong |
| under $250 | never | a 100-contract order is most of the money ever traded |

So a 3% edge against a $2m moneyline is a reason to doubt the model; the same
3% against a $4,000 college total may be real. Direction of flow is reported
separately and only ever softens a recommendation — large money is often
wrong, and following it just reproduces the price with a lag.

### Checking it against games that already happened

Open a completed game and the page shows what it **would have said an hour
before tip-off**, with the final score beside it and a WON/LOST on each
recommended bet. The date picker takes past dates, so this is the same two
clicks as checking tonight's slate.

The reconstruction is faithful rather than convenient, and each of these is a
way it could silently have flattered itself:

| | How it is handled |
|---|---|
| Team history | cut off at kickoff minus an hour by the as-of guard, so the game being graded is invisible |
| Price | the historical **ask** from hourly candles, not the last trade — pricing at the trade flatters every edge by the spread |
| Money | per-interval candle volume summed to the cutoff, and open interest as a level. One NBA moneyline held $87k before tip-off and $2.7m after |
| In-game trading | ignored entirely, though it is in the same response |

**Two components a lookback cannot test.** The injury adjustment is switched
off: ESPN publishes current availability only, so today's report applied to
last week's game is wrong data, not a reconstruction. Money flow is skipped
for the same reason — the live tape is mostly in-game.

And one game proves nothing in either direction. With a 13-point typical
margin error, a 20-point miss is routine and a close hit is partly luck.

### Making the sources agree on team names

Every source names teams differently, and a disagreement costs you the game:
no Kalshi market matches, so the fixture is there but cannot be analysed. On
one evening that was 15 of 29 college basketball games and the only NFL game
on the card. `matching.py` resolves names through one pipeline, strictest
first, and refuses to guess at the end.

**Sources tried, in order of how well the name is spelled out.** Kalshi's
ticker abbreviations come *last*, because they agree with nobody (`WAS` where
ESPN says `WSH`). Before them come the names in `rules_secondary`, and then
the market titles — for most college events the title is the only readable
team name in the whole payload, and nothing used to look at it. Once an event
resolves, its Kalshi codes are learned as aliases, so the ladders on the same
event and every later game resolve immediately.

**Normalisations applied to both sides**, so each can only remove a mismatch:

| Problem | Example | Treatment |
|---|---|---|
| Truncated nickname | Kalshi's `New York G` | the initial matches as a *prefix*; as a word it is simply absent from "New York Giants" and rules it out |
| Accents | ESPN's `San José State` | folded; unfolded, the accent is stripped as punctuation and leaves `jos`, which shares no token with `jose` |
| City abbreviation | ESPN's `LA Clippers` | spelled-out forms contract (`los angeles` → `la`), which leaves Louisiana Tech alone in a way that expanding would not |
| `St.` | `Alabama St.` vs `St. John's` | *state* at the end, *saint* at the front |
| Parenthetical | `Johnson & Wales (RI)` | optional, not ignored — dropping it always would merge `Miami (OH)` into `Miami` |
| "college", "university" | `Boston College` | optional, not noise — dropped always, Boston College and Boston University collapse onto one token |

Optional means exactly that: a strict pass keeps those tokens and runs first,
and only if it finds nothing does a loose pass drop them.

**Ambiguity resolves to nothing.** "New York" fits the Giants and the Jets
equally, and the alias really was bound to the Giants — a silent coin flip
that could put a bet on the wrong team. Where the claimants are *unequal* the
better one wins: a D1 programme beats a lower-division namesake, so
"Charlotte" is the 49ers and not the Division II Charlotte Saints. Where they
are equal the alias is retired and the market surfaces as unmatched, which is
visible and fixable in a way that a wrong team is not.

`NAME_OVERRIDES` remains for divergences no rule can bridge.

### Why an edge is possible

Kalshi charges no vig, only a fee; a sportsbook charges vig. The two venues
therefore disagree by construction, and once the book's margin is stripped out
the remaining gap is information. More usefully, Kalshi lists spreads and
totals as **ladders of independent binaries** ("over 59.5?", "wins by over
7.5?"), so one predicted total implies a probability at every strike and each
is priced separately. Market attention sits near the line, so the tails are
where mispricings concentrate.

### The model

```
game logs → opponent-adjusted windows (last 3 / last 7 / season)
          → fitted blend  → expected points per team
          → predicted spread and total
          → a probability at every ladder strike
```

Constants are **fitted from three seasons**, not guessed: home-field
advantage, the window blend weights, and σ (the residual error that converts a
prediction into a probability). They live in the `calibration` table and are
shown in the app's sidebar.

Three anchors appear beside every prediction — the model, a pure power rating,
and the market. Agreement is uninformative; disagreement is the signal. When
the model is the lone outlier by a wide margin, the app says so rather than
presenting it as an opportunity.

### What the model is actually worth

Measured on the imported history, as variance explained versus simply
predicting the league average:

| League | Spread | Total | σ margin | σ total | HFA | Games |
|---|---|---|---|---|---|---|
| NFL | +15.4% | +5.0% | 13.15 | 13.27 | +1.58 | 614 |
| NBA | +17.5% | +7.6% | 14.69 | 19.11 | +1.68 | 2,621 |
| NCAAF | +42.4% | +6.3% | 16.89 | 16.29 | +5.83 | 4,226 |
| NCAAMB | +36.1% | +18.8% | 12.25 | 17.12 | +3.40 | 11,047 |

Read these before trusting a number. **Spreads carry real signal; totals are
consistently much weaker** — NFL totals at +5.0% are barely better than a
constant. The app surfaces these figures so a prediction is never read as more
than it is, and `adjusted_edge` scales every edge by them, so a league where
the model has no demonstrated skill produces no recommendations.

### The guard that matters most

A model total a few points off the market's makes **every** OVER on the ladder
look cheap. On a real game the first version produced 27 "best bets" at
+10–14% edge each — all of them the same directional guess, repeated. Stacking
money across those rungs turns a modest model error into a large loss.

Three checks now prevent that:

- Edges are scaled by the league's measured skill at that quantity, so a
  totals edge from a model with +2.0% total skill is discounted to nothing.
- A ladder where most strikes share a direction is flagged as **one position,
  not many** — including the spread case, where a one-sided view concentrates
  on a single team rather than showing up as a high share.
- The model's total and spread are each compared against the market, with a
  tolerance scaled to σ rather than fixed in points.

Two findings worth knowing, both from the fit rather than assumption:

- **Recent form adds very little.** The fitted weights land on the season
  window (0.60–0.85 across the four leagues), with the last-3 window earning
  almost nothing. Recent form improves on a plain season average by under 2%.
  The windows are still displayed because the trend between them is
  informative context, but the model does not lean on them. This contradicts
  the usual assumption that the last few games matter most, and it is the
  clearest thing the historical fit bought.
- **Home-field advantage is smaller than folklore.** NFL measures +1.58
  points, not 3. For NCAAMB the ridge estimate is +3.40 while a naive average
  of home margins gives +5.52 — college hosts are usually the stronger team,
  so the naive figure credits the venue for a mismatch.

### Money flow

Everything is weighted by **notional** (price × contracts), so a large
position counts more than a small one — 1,000 contracts at 3c is a $30 punt,
not a conviction bet. On the most liquid NFL market the last 1,000 trades
split 932 YES against 68 NO takers, so the asymmetry is real and measurable.

- Notional-weighted taker imbalance, time-decayed with a 12-hour half-life.
- Whale thresholds computed per market, since a whale on a thin NCAAMB market
  is a minnow on NFL primetime.
- Block trades reported separately — negotiated off-book, different
  information.
- Open interest against price, which separates new money backing a move from
  shorts merely covering. Those look identical on a price chart.
- The orderbook is walked at your intended size, because a 3-cent edge on 40
  resting contracts will not fill 500.

### Injuries

Five signals per player, strongest first: the team's **with/without scoring
split** (observed, not inferred), last-3 and last-7 production, share of team
production, the **starter-minus-backup delta** from the depth chart, and a
status weighting (Out is certain, Questionable is a probability).

Each adjustment carries a confidence level. "Low" means it rests on a
positional prior, which is a visible editable judgement in
`model/injuries.py`, not a measurement. Splits below two games either side are
suppressed rather than reported as findings.

**Injury effects cannot be validated by backtest** — ESPN publishes no
historical injury snapshots. This is the one part of the model resting on
reasoning rather than measurement.

### College availability — measured, not reported

There is **no usable college injury feed.** Verified against the live
endpoints: ESPN's NCAAF injury feed carried 3 players league-wide, its NCAAMB
feed was empty, college game summaries have no `injuries` block at all, and
every college athlete's roster status reads "Active". The NCAA publishes
availability reports only for the championship tournaments, and the
aggregators that do cover it are paid.

So for college the question is answered a different way: **who has actually
been playing**, read from recent box scores. For betting purposes availability
is the question that matters; the diagnosis was only ever a means to it. On a
live NCAAF game this surfaced a starting quarterback who had missed two
straight games and carried 28% of team usage, with the QB position retaining
just 41% of its output — information no feed carries, which is precisely why
the market may not have priced it.

Each absence is classified, because one case is easy to get backwards:

| `kind` | Meaning |
|---|---|
| `absent` | the position's output fell with them — a genuine loss |
| `replaced` | someone absorbed the role, output held up |
| `stand_in` | *they* were covering for a starter who has returned — not a loss |

A backup quarterback who starts while the first choice is hurt racks up usage,
then "disappears" when the starter returns. Read naively that is a 30%-usage
player going missing; in fact the team got stronger. The test that separates
them is whether the *position* kept producing, which is also the quantity the
points model needs.

Two limits, stated in the output: it is **backward-looking** (missing the last
two games is evidence about the next one, not a report on it), and football box
scores list only players who record stats, so linemen and most defenders are
invisible. It runs for the NFL and NBA too, as a cross-check — a player listed
Questionable who has not actually played in three weeks is a different
proposition from one who played last Sunday.

### Checking it yourself

Pick a past date in the sidebar. Completed games render exactly as they would
have pre-kickoff, with the final score beside the prediction. For NFL the
historical closing line is shown too, so you can see whether the model or the
market was closer.

This only means anything because predictions are pinned to **kickoff minus one
hour** and every history query filters to strictly-earlier games
(`model/asof.py`). Published power ratings are end-of-season snapshots, so
using one for a mid-season game would leak the result — which is why the model
computes its own point-in-time ratings and treats FPI, BPI and Barttorvik as
present-day cross-checks only.

A handful of games cannot separate skill from luck in either direction.
Patterns across a few dozen are what mean something.

---

## What is kept

Almost everything in `data/thugsak.db` is a cache. `import_history.py`
rebuilds the games, the ratings and the calibration from public sources on
demand, so losing them costs time and nothing else.

**The `predictions` table is the exception, and it is the one thing here that
cannot be rebuilt.** It is what the model actually said at the time, and no
source will reissue that. Each row holds:

- the prediction, with its as-of cutoff and model version
- **the final score, copied in** once the game finishes
- **the five bets exactly as recommended** — prices, edges, dollars traded,
  and enough of each bet's identity (kind, side, strike, subject team) to
  settle it against a score later
- the matchup, the slate date and both team ids

It is deliberately self-contained rather than joined to `games`, because
`prune_seasons` deletes a season's games once its calibration has been
distilled — including, at rollover, the season you have just spent a year
collecting predictions for. Joining would leave a drawer of forecasts with
nothing to check them against, exactly when the record finally got long
enough to mean something. A test asserts the record still reads after its
games are pruned.

The bets are stored for a second reason: they depend on the Kalshi ask and
the money standing at the cutoff. Kalshi still serves a settled market's
candles today, but that is their retention policy rather than a guarantee,
and a price nobody kept is a bet nobody can grade.

Rows are **upserted** on (game, cutoff, model version). Opening a game used
to append a row every time — one fixture reached twenty — and any accuracy
figure over the table would have counted it twenty times, biasing toward
whichever games got looked at most.

Results fill in by themselves: loading any slate settles every prediction
whose game has since finished, so the record accrues through normal use
rather than needing a job nobody remembers to run. `get_track_record` reads
it back.

**What it is not.** It covers only games whose page was opened, so it is a
record of use, not a backtest over every fixture — the games you chose to
look at are not a random sample. For an unbiased measure use `model_skill`,
fitted on three full seasons.

## Data retention

The three bootstrap seasons exist only to be distilled into `calibration`,
then they are pruned. Steady state keeps the current season (which opponent
ratings and the lookback both need) plus rolling form — around 5–10 MB.

This is safe because every source is public and permanent, so
`import_history.py` rebuilds on demand. Pass `--no-prune` to keep the raw
history and re-fit without re-downloading.

At season rollover, `fit.py` re-fits including the finished season and then
prunes it, so calibration improves each year while storage stays flat.
`predictions` is never pruned — it is your record.

---

## Data sources

All free, none requiring a key.

| Source | Provides |
|---|---|
| **Kalshi** | markets, spread/total ladders, orderbook, trades, candlesticks, fee multipliers |
| **ESPN** | scores, schedules, DraftKings lines, injuries with athlete ids, player game logs, depth charts, FPI/BPI |
| **Barttorvik** | NCAAMB adjusted efficiency for 365 teams, and every D1 game in one file |
| **ESPN box scores** | who actually played — the only availability signal college has |
| **nflverse** | NFL team EPA, and historical closing lines |

Barttorvik exists because ESPN's NCAAMB power index covers only the **top 25
teams**, leaving roughly 340 unrated — and thin NCAAMB markets hold the
biggest mispricings.

Deliberately not used: `stats.nba.com` (unreachable here; ESPN's BPI already
covers NBA), Sports-Reference (ToS prohibits scraping), KenPom (paywalled),
Reddit (403 unauthenticated).

### Adding a stats source

Write one adapter in `src/thugsak/clients/stats/`, implementing `name`,
`leagues` and `team_ratings`, then register it:

```python
from . import TeamRating, register

class MySource:
    name = "mysource"
    leagues = {"nfl"}

    async def team_ratings(self, client, league):
        return {"team name": TeamRating(source=self.name, name="...", net=3.4)}

register(MySource())
```

Add it to `_autoregister()` in `stats/__init__.py`. Nothing else changes —
`get_team_stats` and the MCP tools pick it up, and `matching.resolve_team`
maps its names onto canonical ids.

---

## Using it from Claude Code

`.mcp.json` registers the stdio MCP server, so the same tools are available in
a Claude Code session:

```
list_games · get_prediction · get_edges · get_money_flow ·
get_team_stats · get_price_history
```

The implementations live once in `src/thugsak/mcp/tools.py`; `server.py`
exposes them over stdio and `sdk_server.py` wraps the same callables
in-process for Streamlit, so the two surfaces cannot drift apart.

---

## Gotchas worth knowing

These each cost time to find, and all of them fail *silently* rather than
raising:

- **Point-in-time ratings must be bounded to one season.** The ridge solve
  read every completed game in the database regardless of season, so ten games
  into an NBA season a team's rating was a three-year average — and the
  prediction changed as a side effect of pruning the bootstrap seasons. On one
  live game it projected Oklahoma City by 22 where the book had Portland by
  2.5; bounded to the current season the same solve lands on Portland by 2.1.
  Measured skill barely moved, because the fit is dominated by mid-season rows
  where a cumulative solve is mostly the current season anyway — the damage was
  concentrated in exactly the early-season games you are most likely to be
  looking at in November.
- **A with/without injury split is almost all noise at small n.** Team scoring
  swings about twelve points game to game, so three games either side of an
  absence carries a standard error near ten. Used raw, four absent players
  summed to the −14 point cap for one team and a *+6.5 point bonus* for the
  other — a team credited for having five players out. Deltas are now shrunk
  by `tau^2 / (tau^2 + se^2)`, and an absence can never improve a team.
- **A UTC calendar date is not a slate date.** A 7pm CT tip-off is 01:00 UTC
  the *next* day, so filtering an evening slate by the UTC date of its start
  time hides most of it — a 67-game college basketball night first showed up
  as 12 games — and after 6pm CST a UTC-defaulted "today" shows tomorrow's
  games entirely. Both failures look like a quiet evening. Every date in this
  app is Central (`thugsak/localtime.py`), stored timestamps stay UTC, and
  `games.slate_date` carries the Central date the game belongs to.
- **DST is not a fixed −6 offset.** A hardcoded offset is an hour wrong for
  the whole football season, and the Central day is 23 hours long in March and
  25 in November. The window a slate query spans is computed through the tz
  database. `zoneinfo` has no system fallback on Windows, so `tzdata` is a
  pinned dependency rather than an incidental one.
- **Kalshi prices are decimal-dollar strings** (`"0.3600"`), not integer
  cents. Parsed as `Decimal` throughout; a float cannot represent 0.36.
- **Candlestick `period_interval` accepts only 1, 60 or 1440.** Anything else
  returns an empty list, not an error.
- **`fee_multiplier` is per-series** and is 0 for fee-free series. It is read
  from the API, never assumed — hardcoding 1 invents fees and suppresses real
  edges.
- **ESPN's site-API depth chart returns 200 with an empty `{}` body.** The
  core API path is the real one.
- **Barttorvik's `super_sked` column 4 is a prediction, not a result.** It
  looks exactly like a score ("Villanova -4.5, 77-72"). The actual result is
  column 24. Importing column 4 trains the model on another model's forecasts,
  which produces a suspiciously tight fit and a useless predictor — the smoke
  test checks the spread of totals specifically to catch this.
- **nflverse's plain `.csv` can 404 while `.csv.gz` works** at the same path.
  Asset URLs resolve through the GitHub releases API.
- **Kalshi rate-limits cursor pagination.** Retried with exponential backoff.
- **Always read files with `encoding='utf-8'`** — Windows defaults to cp1252
  and ESPN payloads contain bytes that crash it.
- **Player game logs arrive ~40× larger than the stats they carry**, padded
  with `links` blocks. Projected at the client boundary.

## Layout

```
src/thugsak/
  config.py          league registry — all per-league quirks in one place
  localtime.py       UTC underneath, Central at every surface
  store.py           SQLite schema, as-of-safe reads, retention
  cache.py           HTTP TTL cache with backoff
  matching.py        Kalshi ticker ↔ ESPN game, team alias resolution
  clients/           kalshi.py, espn.py, stats/
  model/             asof.py, ratings.py, fit.py, gamelog.py, injuries.py,
                     availability.py, predict.py
  analysis/          fees.py, devig.py, flow.py, edge.py,
                     lookback.py (what it would have said),
                     money.py (dollars gate the recommendation),
                     verdict.py (the predicted score and five bets)
  mcp/               tools.py (the one implementation), server.py, sdk_server.py
app/                 streamlit_app.py, views/game_detail.py
scripts/             import_history.py, smoke.py
tests/               test_math.py, test_model.py, test_edge.py,
                     test_availability.py, test_verdict.py,
                     test_localtime.py, test_model_bugs.py,
                     test_lookback.py, test_record.py,
                     test_name_matching.py
```

`model/asof.py` is load-bearing: it is the single chokepoint deciding what the
model may see, and a test asserts a prediction is identical whether or not
later games exist in the database. It shares its timestamp parser with
`localtime.py`, so the display layer and the guard cannot disagree about what
an instant is — but cutoffs themselves stay UTC deliberately, because local
time is non-monotonic across the autumn DST fold, where two instants an hour
apart render identically and sort equal.

---

## Limitations

- **Totals are weak.** See the skill table. NFL totals barely beat a constant.
- **Injuries are unvalidated** by any backtest, for the reason above.
- **Only NFL has historical closing lines**, so lookbacks in the other three
  leagues compare against the result only. The app stores lines it sees from
  now on, so those leagues build their own benchmark over time.
- **ESPN endpoints are undocumented** and can change without notice. Run
  `scripts/smoke.py` when numbers look wrong.
- **Team-name matching is the main maintenance burden**, and when it fails it
  fails *silently*: the game simply shows no Kalshi markets. There is no
  error, just a fixture you cannot analyse. `matching.py` normalises every
  source through one pipeline — see below — and anything it still cannot
  resolve is reported as an unmatched event with the reason attached, rather
  than dropped.
- **College has no injury feed or depth charts.** ESPN publishes depth charts
  only for the pro leagues (college paths return 400), so the
  starter-minus-backup signal is unavailable there. Derived availability (see
  above) covers the gap, but it is backward-looking rather than a report.
- **College is filtered to Division I**, plus any lower-division team a D1
  side actually played. ESPN's college-football `/teams` returns 763 teams
  across every division; the D1 list comes from the core API's conference
  groups (80 = FBS, 81 = FCS; 50 for basketball), giving 265 D1 football teams.
  Every stored NCAAF game involves at least one D1 team.
- **A positive expectation still loses often.** Nothing here makes a bet safe.
