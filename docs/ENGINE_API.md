# Engine API

Everything the dashboard needs, and nothing it has to know about ESPN or The
Odds API. Written for whoever builds the UI: import from these four modules and
treat the rest as internal.

```python
from src.picker import board          # read a slate, read one matchup
from src.picker import parlay_builder # combine legs into slips
from src.data   import sports         # which sports exist, and their labels
from src.picker import actions        # approve / edit / reject saved picks
```

Sports supported: `nfl`, `mlb`, `nhl`. Pass the code as a string anywhere a
`sport` argument appears.

---

## 1. The slate

```python
cards = board.slate_board(day=date.today(), sport="nfl")
```

Fast (local DB + cached schedule frame, no live player pulls). Returns
`list[GameCard]`, one per game that day, in kickoff order.

### `GameCard`

| Field / property | Type | Notes |
|---|---|---|
| `game_id` | `int` | DB id. Pass this to `deep_matchup` lookups. |
| `home`, `away` | `str` | Team abbreviations, e.g. `BUF`, `DET`. |
| `matchup` | `str` | `"DET @ BUF"`. |
| `sport` | `str` | `nfl` / `mlb` / `nhl`. |
| `status` | `str` | `scheduled` / `in_progress` / `final` / `postponed`. |
| `start_utc` | `str` | ISO timestamp. |
| `kickoff_local()` | `str` | `"Thu 8:15 pm ET"`. |
| `started` / `bettable` | `bool` | **See the warning below.** |
| `prediction` | `dict` | See below. |
| `h2h` | `TeamH2H \| None` | Prior meetings. |
| `candidates` | `list[LineCandidate]` | Bettable game lines. Empty for started games. |
| `best` | `LineCandidate \| None` | Highest edge, or `None`. |

> **Started games carry no candidates.** Books keep quoting a game once it is
> under way, and a pregame model reads those live prices as enormous edges  a
> 7.5-run baseball line at -10000 is a scoreline, not value. `slate_board`
> returns started games so you can display them, with `candidates == []`.
> Render `card.bettable is False` as "under way / final", not as "no value".

`card.prediction` keys:

| Key | Meaning |
|---|---|
| `home_win_prob` | Model probability the home side wins. |
| `margin_mean`, `margin_sd` | Model margin, home-relative. |
| `elo_margin` | Margin before the head-to-head nudge. |
| `h2h_margin` | Average home margin in prior meetings, or `None`. |
| `h2h_total` | Average combined score in prior meetings, or `None`. |
| `h2h_weight` | `0.0`–`0.30`. How far the H2H record pulled the model. |

Showing `elo_margin` next to `margin_mean` is the honest way to display the
head-to-head adjustment: it makes the size of the nudge visible.

### `LineCandidate`

| Field | Notes |
|---|---|
| `market` | `h2h` (moneyline) / `spread` / `total`. |
| `side` | Team abbreviation, or `Over` / `Under`. |
| `line` | `None` for moneyline. |
| `headline` | Ready to print: `"DET ML"`, `"DET +5.5"`, `"Under 54.5"`. |
| `odds` | American, e.g. `-110`, `+210`. |
| `bookmaker` | Which book offered it. |
| `model_prob`, `implied_prob`, `edge` | `edge = model_prob - implied_prob`, vig removed. |
| `confidence` | `dict`  `score` is the 1–10 value; the rest is the breakdown. |
| `units` | Suggested stake, from the confidence tiers. |
| `h2h_note` | One sentence of head-to-head evidence. Can be `""`. |

`confidence` breakdown keys: `score`, `raw`, `edge_score`, `prob_score`,
`quality_score`. Show `score` prominently; the others belong in a tooltip.

### `TeamH2H`

```python
series = card.h2h
series.n                     # number of prior meetings found
series.summary_line()        # "BUF leads 3-1 over 4 meetings | avg total 48.2 | ..."
series.record()              # (team_a wins, team_b wins, ties)
series.avg_total()           # float | None
series.avg_margin_for("BUF") # float | None
series.cover_rate_for("BUF") # (covers, games with a posted spread) | None
series.totals_over_rate()    # (overs, games with a posted total) | None
series.meetings              # list[Meeting], newest first
```

`Meeting` has `date`, `season`, `week`, `home`, `away`, `home_score`,
`away_score`, `margin`, `total`, `winner()`, `home_covered()`, `over_hit()`,
`margin_for(team)`.

Anything that returns `None` means *not known*, never zero. NFL meetings carry
closing spreads and totals (from nflverse); MLB and NHL meetings carry scores
only, so `cover_rate_for` and `totals_over_rate` return `None` there. Render
that as "not available", not as 0/0.

---

## 2. One matchup, in depth

```python
deep = board.deep_matchup(card, fetch_prices=True, progress=cb)
```

Slow on first open: it pulls both rosters and every player's game log from
ESPN, then optionally buys prop prices. Everything is cached on disk
afterwards. `progress` is `callable(fraction: float, message: str)`  wire it
to a progress bar, because the first call on a cold cache takes a while.

Returns `DeepMatchup`:

| Field | Notes |
|---|---|
| `card` | The `GameCard` it was built from. |
| `home_players`, `away_players` | `list[PlayerSample]`. |
| `props` | `list[PropCandidate]`, best first. |
| `priced` | `True` = real book prices. `False` = model-only watchlist. |
| `props_note` | Why prices are missing, when they are. Display it. |

### Credits

`fetch_prices=True` spends Odds API credits: **one credit per market per
game**. The default market set is 4–5 markets, so roughly 5 credits per
matchup. The client refuses to spend below a floor (`credits.floor` in
`config/settings.yaml`, default 50) and raises `CreditGuard`.

```python
from src.data.props_odds import PropsOddsClient
PropsOddsClient.credits()   # {"remaining": 469, "used": 31, "checked_at": ...}
```

Put the remaining balance somewhere permanent in the UI, and make
`fetch_prices` an explicit button rather than something that fires on page
load. `fetch_prices=False` uses only what is already cached and never spends.

### `PropCandidate`

| Field | Notes |
|---|---|
| `player`, `position`, `team`, `opponent` | Who and against whom. |
| `headline` | `"Josh Allen 250.5+ Pass Yds"` or `"James Cook under 2.5 Receptions"`. |
| `side` | `over` / `under`. |
| `line`, `stat_key`, `label` | `label` is the display name, e.g. `Rec Yds`. |
| `probability` | Model probability this hits. Always present. |
| `odds`, `implied_prob`, `edge`, `bookmaker` | **Only when `priced` is `True`.** |
| `priced` | `False` → `odds` is `None`. Do not print a price. |
| `evidence()` | `"3/4 vs MIA (avg 223.2) · 7/9 in last 9 · proj 240.8"`. |
| `hit_vs_opponent` | `(cleared, games against this opponent)`. |
| `hit_recent` | `(cleared, recent games)`. |
| `projection` | `Projection`  `mean`, `sd`, `n_games`, `n_vs`, `vs_average`. |
| `injury_status` | `""` when healthy; otherwise e.g. `"Questionable"`. Surface it. |
| `raw_probability` | The model's view *before* the market prior. |
| `warnings` | `list[str]` of plain-language cautions. |
| `trustworthy` | `False` when `warnings` is non-empty. |

**The market is used as a prior.** Books price props sharply and know things
this model cannot see  a snap share, a depth-chart change, a coach's plan. So
`probability` is the model's view pulled partway toward the vig-free price, and
`raw_probability` keeps the unadjusted number for inspection. Without this, a
player whose role changed last month shows up as a 30% edge.

`warnings` fires on: a disagreement with the market wide enough that missing
information is the likelier explanation, a listed injury, a sample under six
games, and no prior meeting with this opponent. **Display them next to the
pick, not in a tooltip.** A prop with `trustworthy is False` is not
necessarily bad  it is one the user should look at before backing.

`hit_vs_opponent[1] == 0` means these two have never met with this player
involved. That is common and worth showing plainly  it is the difference
between a head-to-head read and a form read.

### `PlayerSample`

`athlete_id`, `name`, `position`, `team`, `opponent`, `vs_games`,
`recent_games`, `injury_status`, plus `vs_average(stat)`,
`recent_average(stat)`, `values(stat, vs_only=False)`, `stat_keys()`.

Each game in `vs_games` / `recent_games` is a dict with `date`, `opponent`,
`home_away`, `result`, `score`, and that sport's stat keys. Games the player
sat out are already removed.

---

## 3. Parlays

```python
pool = board.parlay_pool(cards, props=deep.props, min_probability=0.55)
slips = parlay_builder.optimize(pool, min_legs=2, max_legs=4,
                                rank_by="probability", limit=10)
```

`parlay_pool` turns lines and props into `Leg`s and skips started games.
`optimize` searches combinations under the correlation rules:

- no two legs from the same game (unless `allow_same_game=True`)
- never the same player twice
- never the same player-and-stat twice

`rank_by="probability"` gives the most likely slip; `"value"` gives the best
expected value. Offer both  they are genuinely different slips.

### `Parlay`

| Field | Notes |
|---|---|
| `legs` | `list[Leg]`; use `leg.display()` and `leg.reason`. |
| `american_display()` | `"+458"`. |
| `combined_model_prob` | Model probability the whole slip hits. |
| `implied_prob` | What the price implies. |
| `expected_value` | Profit per 1 unit staked, at model probabilities. |
| `edge` | `combined_model_prob - implied_prob`. |
| `correlated` | `True` for same-game slips (probability already discounted). |
| `fully_priced` | `False` if any leg used an assumed price. |
| `notes` | Warnings to display verbatim. |

Combined probability multiplies the legs, which assumes independence. That
holds across different games; inside one game it does not, so same-game slips
are discounted and flagged. Show `notes`  they are the caveats.

---

## 4. Saving and grading

`board` and `Picker.suggest()` never write. Automatic selection is the only
straight-pick write path:

```python
from src.picker import autopick
autopick.run("nfl", days=7)
```

Qualifying picks are committed as `approved`, then become `graded` after the
game is final. Non-qualifying candidates are never stored.
Grading is `src/picker/grader.grade_all(game_date=...)`; season numbers come
from `src/picker/backtest.record_summary()`.

---

## 5. Syncing

```python
from src.picker.engine import Picker
p = Picker("mlb")
p.sync_games(date.today())        # ESPN schedule -> games/teams tables
p.sync_odds(day=date.today())     # game lines -> odds table
```

`sync_odds` returns `{"source": "odds_api" | "espn_backup", "games": n}`. It
falls back to ESPN's embedded lines when the Odds API is unavailable, so a
missing key degrades rather than breaks.

Game-line syncing is cheap (one credit per sport per call, cached a day).
Prop pricing is the expensive part  see Credits above.

---

## 6. Sports registry

```python
from src.data import sports
sports.enabled_codes()        # ["nfl", "mlb", "nhl"], in settings.yaml priority
cfg = sports.get("mlb")
cfg.name, cfg.emoji           # "MLB", "⚾"
cfg.stat_labels               # {"hits": "Hits", ...}
cfg.stats_for("SP")           # ["strikeouts", "innings", ...]
```

Never hardcode a stat name or a sport list in the UI; read them from here so
the three sports stay consistent.

---

## Gotchas worth knowing

1. **NHL is out of season until October.** Its wiring is verified against the
   same endpoints as MLB, but a slate will be empty until then. Say "no games
   today", not "error".
2. **First matchup open is slow** on a cold cache  dozens of ESPN requests.
   Always show progress.
3. **`None` means unknown.** Coverage rates, H2H averages and prop prices are
   all `None`-able. Rendering `None` as `0` would be a lie.
4. **Team abbreviations differ by source.** nflverse says `LA`/`WAS`, ESPN says
   `LAR`/`WSH`. The engine normalises to ESPN spellings; display `card.home`
   and `card.away` as given.
5. **Emoji need UTF-8.** The Windows console is cp1252; `main.py` reconfigures
   stdout. The local Flask UI is fine.
