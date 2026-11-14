# Game-data sources

The app reads schedules, results, team game logs and injuries from one
**provider** (`src/thugsak/providers/`), chosen by `THUGSAK_PROVIDER` and
defaulting to `espn`. Team *strength ratings* come from a separate registry
(`src/thugsak/clients/stats/`) and can be mixed freely — that is why
Barttorvik supplies NCAAMB ratings while ESPN supplies the games.

Swapping the provider is a configuration change. Swapping it **well** is not,
because every fitted constant in the `calibration` table was fitted on the
current provider's numbers. See the bottom of this page.

## Status

| Source | Role | Usable now | Why |
|---|---|---|---|
| **ESPN** | provider (default) | yes | Undocumented public endpoints, no key. Carried over from v1. |
| **Barttorvik** | NCAAMB ratings | yes | 365 D1 teams; ESPN's BPI covers only the top 25. |
| **nflverse** | NFL history + lines | yes | Public GitHub releases; the only free source of historical closing lines found. |
| **Real Sports** | provider | **no** | No public API, and the Terms of Use prohibit automated access without written permission. See below. |

## Real Sports (realapp.com) — blocked

Surveyed 2026-11-12. Coverage is genuinely better than the current mix: all
four leagues this project models, plus MLB, NHL, WNBA, soccer, tennis and
golf, from one consistent source.

Two things block it:

1. **No public API.** The app's backend is `mobileweb.realapp.com`. `GET /`
   returns `{"success":true}` and `GET /health` returns
   `{"healthy":true,"realVaultEnvironment":"MOBILEWEBAPI"}`, but every other
   path tried returned `HTTP 400 {"error":"Bad Request","message":"Invalid
   request"}`. A 400 rather than a 404 means the route exists and is refusing
   an unauthenticated or unsigned request — it is a private backend for their
   own client, not an open API. There is no `/openapi.json`, no developer
   portal and no published plan.

2. **The Terms of Use forbid this use.** Real Vault, Inc's terms
   (<https://docs.realapp.link/policies/terms-of-use>, last updated
   2023-10-01) prohibit accessing the Product "through automated or non-human
   means, whether through a bot, script, or otherwise"; "systematically
   retriev[ing] data or other content from the Product to create or compile,
   directly or indirectly, a collection, compilation, database, or directory
   without written permission"; and any "spider, robot, ... scraper, or
   offline reader".

   `www.realapp.com/robots.txt` is permissive, which is easy to mistake for a
   licence. It is not: it governs search-engine crawling of the marketing
   site, while the Terms govern the Product. Where they differ, the Terms are
   the agreement.

**The route in:** every one of those clauses is conditioned on *"without
written permission from us"*, so permission is a real option rather than a
formality. A drafted request is in
[`real-data-access-request.md`](./real-data-access-request.md). If it is
granted, fill in `RealProvider` against whatever was actually granted and set
`PERMISSION_ON_FILE = True`.

This project already treats terms as binding — Sports-Reference was excluded
from v1 on terms grounds too (the specific clause is below), and Barttorvik
adopted in its place. Treating Real differently would be inconsistent, not
bolder.

## Sports-Reference — excluded, for a narrower reason than v1 recorded

Re-checked 2026-11-12 against the live Terms
(<https://www.sports-reference.com/termsofuse.html>, last updated
2023-05-19), `robots.txt` and `data_use.html`, because v1's plan said only
"ToS prohibits scraping" and that turned out to be imprecise in a way that
matters.

**Their automated-access clause is conditional.** Section 5 forbids using
"scripts, bots, scrapers, data miners, or similar software" only *"in a
manner that adversely impacts site performance or access"* and without
written permission. Polite, rate-limited fetching is therefore not, by
itself, the violation.

**Clause 11 is unconditional, and it names this architecture.** The same
section prohibits using the site *"for purposes of training, fine-tuning,
prompting, or instructing artificial intelligence models."* The agent layer
in this project is Claude reading box scores and reasoning over them — that
is "prompting or instructing an AI model" in plain words. `robots.txt`
`Disallow: /` for GPTBot is consistent with the intent. Their custom-data
route starts at a stated **$5,000 minimum**, and `data_use.html` says of some
datasets that their own licences "completely preclude any redistribution".

**It would not have filled the gap anyway.** Sports-Reference is a
historical archive: box scores, team game logs, per-player game logs, all
excellent. It carries **no injury status and no depth charts** — the two
forward-looking inputs the injury model depends on and the two things ESPN
uniquely supplies. `robots.txt` also disallows the `/cbb/players/*/splits/*`
and `/cfb/players/*/splits/*` paths, which are the per-player breakdowns most
worth having for college.

**What is permitted, and used:** their Section 5 guiding principle says
"sharing, using, modifying, repackaging, or publishing data found on
individual SRL webpages is welcomed" with attribution. A person reading a page
to sanity-check a number is fine. An automated feed into an agent is not, and
there is no adapter.

**Where the player data comes from instead:** ESPN's existing verified
endpoints (`player_gamelog`, `depth_chart`, `injuries`, `summary` box scores)
plus nflverse's player-level files (`stats_player_week_*`, rosters, snap
counts), which are open data and already a dependency.

## Alternatives with terms that permit programmatic use

Checked as live on 2026-11-12. **Confirm current terms before adopting any of
them** — all that was verified here is that the endpoints respond and that
each publishes an access path, not the detail of what each licence allows.

- **CollegeFootballData.com** — `api.collegefootballdata.com` responds 200 and
  the project publishes an API with free keys. Deep NCAAF coverage: games,
  drives, plays, advanced team metrics, betting lines. The strongest candidate
  for improving NCAAF specifically, which is currently the league with the
  least detail behind it.
- **balldontlie.io** — `api.balldontlie.io/v1/teams` responds 401, i.e. a
  key-gated API with a documented free tier. NBA primarily, with NFL and MLB
  added. Would be the natural second provider for NBA.
- **SportsDataIO** — commercial, with a free tier limited to last season's
  data. Formal licence terms, which is exactly what Real lacks.
- **Sportradar** — enterprise, trial available. Almost certainly oversized and
  overpriced for a single-user local tool.
- **The Odds API** — odds rather than stats; relevant only if historical
  closing lines are wanted for NBA, NCAAF and NCAAMB, which is the one real
  gap in the lookback (nflverse already covers NFL).

## What a provider swap costs

Not just the adapter. Anything that changes the input numbers invalidates
what was fitted on the old ones:

1. **Re-seed the alias table** under the new `source` name. Use
   `matching.seed_source_codes`; `store.SCOPED_PREFIX` is what keeps one
   provider's spellings from contaminating another's. v1 shipped a bug here —
   an nflverse-scoped alias leaked into general lookups — so the scoping is
   load-bearing, not tidiness.
2. **Re-import history and re-fit.** The fitted sigma, HFA and blend weights
   in `calibration` describe the old provider's data. A new provider that
   counts a stat differently moves them.
3. **Note the cutover date** in the predictions record. The lookback reads as
   one continuous history, and it would not be: predictions either side of a
   provider change were made by materially different models. Without a marker,
   a step change in accuracy looks like the model getting better or worse
   rather than the data changing underneath it.
4. **Re-run `scripts/smoke.py`.** It exists because undocumented endpoints
   change without notice, and a new provider is a new set of assumptions about
   response shape.
