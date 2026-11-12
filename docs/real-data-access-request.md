# Drafted data-access request to Real Vault, Inc

Send to **support@real.vg** (the support address on the Google Play listing),
or via the contact on **realsports.io**. Fill in the bracketed fields before
sending — name and contact details are deliberately left blank rather than
guessed.

Keep the non-commercial framing only if it is true. If the tool is ever used
to place bets with real money, say so: a licence obtained on a false
description is worse than no licence.

---

**Subject:** Permission request — personal, non-commercial use of Real sports data

Hello,

I'm building a personal tool that compares my own statistical forecasts of US
sports games against publicly listed prediction-market prices, as a way of
checking whether my model is any good. It covers NFL, NBA, college football
and college basketball.

Right now it reads game schedules, results and injury listings from ESPN's
undocumented public endpoints. I'd much rather use Real: your coverage is
broader and more consistent across exactly the leagues I care about, and the
college basketball depth in particular is better than what I can assemble
elsewhere.

Your Terms of Use prohibit automated access and systematic retrieval without
written permission, so I'm asking rather than assuming. Specifically I'd like
to know whether you offer, or would consider granting:

1. **API access** — a documented or even undocumented keyed endpoint for
   schedules, final scores, team game logs and injury status.
2. **A data licence** — periodic bulk files would work as well as a live API;
   I don't need real-time latency.
3. **Written permission** for low-volume automated reads of the public pages
   on www.realapp.com, if neither of the above is available.

What I can commit to:

- **Volume:** a few hundred requests a day at most, rate-limited, with
  aggressive local caching. Historical data is immutable once a game is final,
  so it is fetched once and never re-fetched.
- **Scope:** [personal use only / describe honestly] — a single local
  instance, not a hosted service, with no redistribution of your data and no
  public-facing surface.
- **Attribution:** happy to display "Data by Real" wherever the data appears,
  and to link to the app.
- **Identification:** a descriptive User-Agent so you can see and throttle my
  traffic, and I'll respect any limit you set.
- **Removal:** if you ask me to stop, I stop and delete the stored copy.

What I'd want to confirm on your side: whether the underlying data is yours to
license, or whether it comes from an upstream provider whose own terms would
govern. If it's the latter, I'd rather hear that than put you in an awkward
position.

If none of this is something you offer, a plain "no" is a perfectly good
answer and I'll stay on my current sources — I'd just rather ask than scrape.

Thank you for your time,

[Name]
[Contact]

---

## If they say yes

1. Record what was granted — scope, rate limit, attribution, expiry — in this
   file, with the date and the person who granted it.
2. Implement `RealProvider` in `src/thugsak/providers/real.py` against what
   was actually granted, not against what the mobile app happens to call.
3. Set `PERMISSION_ON_FILE = True` there and run with
   `THUGSAK_PROVIDER=real`.
4. Seed the alias table under `source="real"` so Real's team spellings are
   scoped and cannot contaminate ESPN's. `matching.seed_source_codes` is the
   existing pattern; `store.SCOPED_PREFIX` is why it stays private.
5. Re-import history and re-fit. Provider changes move the inputs, so the
   fitted sigma, HFA and blend weights are no longer the ones on file, and the
   stored prediction record spans two different data sources — note the
   cutover date in the record so the lookback is not read as continuous.

## If they say no

Nothing to do. The app runs on ESPN, which is what `DATA_PROVIDER` defaults
to. Alternatives with terms that permit programmatic use are listed in
`docs/data-sources.md`.
