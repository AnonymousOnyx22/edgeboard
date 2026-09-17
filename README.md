# h2h-parlay-picker

A sports betting research tool for **NFL, MLB and NHL** that builds its picks from
head-to-head history — how these two teams have actually played each other, and
how each player has actually performed against *this specific opponent*.

It prices those reads against live sportsbook odds, scores them with a
confidence model, and searches for the parlays worth playing.

It does not place bets, post anything, or tell you it found free money.

```
DET @ BUF    Thu 8:15 pm ET
  H2H: BUF leads 1-0 over 1 meetings | avg total 90.0 | avg margin 6.0
  DET ML            +210 betmgm       edge  +7.5%  conf 5/10  1.0u
      DET won 0 of the last 1 meetings, covered 0/1
  Under 54.5        +100 draftkings   edge  +6.1%  conf 5/10  1.0u
      Under hit in 0/1 meetings at this number (avg 90.0)

Player props (market priced):
  Josh Allen 250.5+ Pass Yds   -113 fanduel  p=64.3%  edge=+14.3%
      1/1 vs DET (avg 362.0) · 7/10 in last 10 · proj 262.7
  James Cook III under 2.5 Receptions  -120 betmgm  p=62.0%  edge=+11.0%
      1/1 vs DET (avg 1.0) · 7/10 in last 10 · proj 1.8
```

---

## The idea

Most public betting models stop at team strength. This one asks a narrower and
more useful question: *what happened the last time these two played, and who
did it?*

For any matchup it pulls the previous meetings — scores, margins, totals, who
covered the spread — and then every skill player's own stat line **in those
games**. A receiver who has quietly gone over his yardage line in four straight
against one defence is a different bet from one who has the same season
average against everyone else.

That head-to-head signal then feeds three layers:

**Game lines.** Elo ratings trained on real completed results, nudged toward
the head-to-head record. The nudge scales with the number of prior meetings and
caps at 30%, because four games is evidence, not proof. Priced against live
moneyline, spread and total odds with the vig removed.

**Player props.** Each player's game log is weighted by recency *and* by whether
the game was against this opponent, turned into a projection with a proper
weighted standard deviation, and scored against the lines books are actually
offering across up to six sportsbooks.

**Parlays.** A combinatorial search over every qualifying line and prop, under
real correlation rules — no two legs from one game, never the same player
twice, never the same player-and-stat twice. Rankable by hit probability or by
expected value, which produce genuinely different slips.

---

## What I'm actually proud of

The interesting engineering here is not the model. It's everything that stops
the model from lying to you. A betting tool that reports confident nonsense is
worse than no tool.

**The market is used as a Bayesian prior.** Sportsbooks price props sharply and
know things this model cannot see — a snap share, a depth-chart change, a
coach's plan. So the model's probability is pulled partway toward the vig-free
market price, and the unadjusted number is kept alongside it for inspection.
Without this, a player whose role changed last month shows up as a 30% edge.
Disagreements wide enough to be implausible get flagged in plain language:

> *"Model is 20% away from the market. That usually means a role change or
> injury the model cannot see."*

**Thin samples shrink toward a coin flip.** Three-for-three in three games is
not a 99% bet. Empirical hit rates are blended with a normal estimate, weighted
by effective sample size, then shrunk toward 50% when the sample is thin.

**Games the player sat out are removed.** A Week 18 rest game is a row of zeroes
that dragged one quarterback's projection down 30 yards and inflated its spread
by 50%. Participation is checked per position — pass attempts for a QB, targets
for a receiver, batters faced for a pitcher.

**In-play games are never priced.** Books keep quoting a game once it starts.
A pregame model reads a live 7.5-run baseball line at -10000 as enormous value.
Started games are shown, but carry no picks.

**Totals use real team scoring rates**, shrunk toward the league mean, rather
than a flat league average. The flat version "found" a 28% edge and rated it
10/10 confidence purely by disagreeing with the market about how a matchup
scores. The same pick with real rates: +6.1%, 5/10.

**`None` means unknown, never zero.** MLB and NHL history carries scores but no
closing lines, so cover records come back `None` and are displayed as
unavailable rather than as 0/0.

### Bugs worth mentioning

Two were silent and would have quietly corrupted every number on screen:

- The odds API was never asked for American format. It defaults to decimal, so
  `1.91` was being truncated to the integer `+1` — every game-line edge in the
  database was garbage.
- ESPN team ids repeat across leagues, and the response cache wasn't namespaced
  by league. Syncing baseball renamed NFL team 2 from the Buffalo Bills to the
  Boston Red Sox, and the football board started showing baseball teams.

---

## Architecture

```
src/data/
  sports.py          sport registry - every league-specific fact lives here
  espn_client.py     schedules, teams, scores, backup odds
  espn_stats.py      player game logs, rosters, injuries
  espn_schedule.py   completed-game history for MLB and NHL
  odds_client.py     game-line prices
  props_odds.py      player-prop prices, with credit protection
  models.py          Elo, scaled per sport
src/picker/
  board.py           the read layer: a slate, or one matchup in depth
  h2h.py             head-to-head: team meetings and player histories
  props.py           projection and hit-probability model
  parlay_builder.py  parlay construction and combinatorial search
  confidence.py      the 1-10 confidence score
  edge.py            odds maths - pure functions, fully tested
docs/ENGINE_API.md   interface reference for the UI layer
```

The picker is sport-agnostic. Adding a league means adding one `SportConfig` —
its ESPN path segments, odds keys, which stats each position is bet on, line
increments, and the Elo scale that makes 100 rating points worth 2.75 points in
the NFL but a fraction of a run in baseball.

| Sport | History | Player stats | Props |
|---|---|---|---|
| NFL | nflverse (with closing lines) | ESPN | yes |
| MLB | ESPN | ESPN | yes — batting and pitching |
| NHL | ESPN | ESPN | yes — skaters and goalies |

**Data sources.** ESPN's public endpoints (free, no key) for schedules, scores,
rosters, player game logs and injuries; [nflverse](https://github.com/nflverse)
for NFL history including closing spreads and totals;
[The Odds API](https://the-odds-api.com) for live prices.

No field name in this codebase was guessed. Every endpoint was verified against
a live response first, and the shape of that response is documented in the
module that consumes it.

---

## Running it

```bash
pip install -r requirements.txt
cp .env.example .env          # add an Odds API key, or don't
python main.py sync           # schedules and game lines
python main.py board          # today's slate with edges
```

```bash
python main.py board --sport mlb           # one sport
python main.py matchup 1 --fetch-prices    # deep head-to-head for one game
python main.py parlay --legs 3             # safest 3-leg parlays
python main.py parlay --legs 3 --rank value
python main.py credits
```

`board` prints a game id next to each matchup; pass it to `matchup`.

**Without an API key it still works** — it falls back to ESPN's embedded lines
for game odds, and props become a model-only watchlist with no prices.

**Credits.** Prop pricing costs one credit per market per game, so about five
credits per matchup on a 500-credit free tier. The client caches hard, refuses
to spend below a configurable floor, and `--fetch-prices` is opt-in rather than
firing on page load.

### Tests

```bash
python -m pytest -q      # 87 tests
```

The odds maths, confidence model, projection model, head-to-head layer and
parlay correlation rules are covered. No test touches the network.

---

## Status

The engine is complete and verified against live data for NFL and MLB. NHL is
wired against the same endpoints and its stat mapping is in place, but it could
not be exercised against live games — the season starts in October.

The CLI exposes the full engine. A local Flask interface in `src/web/` renders
the same board, matchup and parlay views in a browser, built against
[`docs/ENGINE_API.md`](docs/ENGINE_API.md) — the interface contract that keeps
the UI free of any knowledge of ESPN or The Odds API.

`archive/` holds an earlier incarnation of this project — an Instagram content
generator with caption templates and card rendering — kept because it's where
the odds and confidence models originally came from. It isn't wired into the
app. See `archive/README.md`.

---

## A note on what this is

This is a research and modelling exercise. It surfaces disagreements between a
statistical model and a sportsbook, with the evidence attached so a human can
judge them. Those disagreements are frequently the model being wrong — which is
why so much of the code is dedicated to saying so.

It is not betting advice, and a positive modelled edge is not a prediction.

MIT licensed.
