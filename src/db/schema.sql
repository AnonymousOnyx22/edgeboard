PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS sports (
    id          INTEGER PRIMARY KEY,
    code        TEXT UNIQUE NOT NULL,          -- nfl / mlb / nhl / nba
    name        TEXT NOT NULL,
    enabled     INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS teams (
    id          INTEGER PRIMARY KEY,
    sport_id    INTEGER NOT NULL REFERENCES sports(id),
    league_key  TEXT NOT NULL,                 -- API team id (ESPN / league)
    name        TEXT NOT NULL,
    abbrev      TEXT,
    UNIQUE(sport_id, league_key)
);

CREATE TABLE IF NOT EXISTS games (
    id          INTEGER PRIMARY KEY,
    sport_id    INTEGER NOT NULL REFERENCES sports(id),
    external_id TEXT UNIQUE,                   -- ESPN event id
    away_team   INTEGER REFERENCES teams(id),
    home_team   INTEGER REFERENCES teams(id),
    start_utc   TEXT NOT NULL,
    game_date   TEXT,                          -- league-local calendar date
    slate       TEXT,                          -- NFL kickoff window: thursday,
                                               -- sunday_early, sunday_night, ...
                                               -- (column name kept for existing DBs)
    status      TEXT NOT NULL DEFAULT 'scheduled',  -- scheduled / in_progress / final / postponed
    week        INTEGER,
    away_score  INTEGER,
    home_score  INTEGER
);
CREATE INDEX IF NOT EXISTS idx_games_start ON games(start_utc);
CREATE INDEX IF NOT EXISTS idx_games_sport  ON games(sport_id, status);

CREATE TABLE IF NOT EXISTS odds (
    id          INTEGER PRIMARY KEY,
    game_id     INTEGER NOT NULL REFERENCES games(id),
    market      TEXT NOT NULL,                 -- h2h / spreads / totals
    side        TEXT NOT NULL,                 -- away / home / over / under / label
    price       INTEGER NOT NULL,              -- American odds, e.g. -110
    line        REAL,                          -- spread / total
    bookmaker   TEXT,
    source      TEXT NOT NULL DEFAULT 'odds_api',
    captured_utc TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_odds_game ON odds(game_id, market, side);

CREATE TABLE IF NOT EXISTS picks (
    id              INTEGER PRIMARY KEY,
    sport_id        INTEGER NOT NULL REFERENCES sports(id),
    nature          TEXT NOT NULL DEFAULT 'straight',   -- straight / parlay
    source          TEXT NOT NULL DEFAULT 'model',      -- model / manual / edited
    status          TEXT NOT NULL DEFAULT 'approved',   -- approved / scheduled / graded
    edge            REAL,
    confidence_model REAL,
    confidence_final REAL,
    units_model     REAL,
    units_final     REAL,
    reason          TEXT,
    episode_id      INTEGER REFERENCES episodes(id),
    created_utc     TEXT NOT NULL,
    edited_utc      TEXT,
    approved_utc    TEXT
);

CREATE TABLE IF NOT EXISTS pick_legs (
    id          INTEGER PRIMARY KEY,
    pick_id     INTEGER NOT NULL REFERENCES picks(id) ON DELETE CASCADE,
    ord         INTEGER NOT NULL,
    game_id     INTEGER NOT NULL REFERENCES games(id),
    market      TEXT NOT NULL,                 -- h2h / spread / total
    team        TEXT NOT NULL,                 -- team abbrev or "Over"/"Under"
    line        REAL,                          -- for spread/total
    odds_american INTEGER NOT NULL,
    model_prob  REAL,
    implied_prob REAL,
    edge        REAL,
    support_stat TEXT,
    reason      TEXT,
    UNIQUE(pick_id, ord)
);

CREATE TABLE IF NOT EXISTS episodes (
    id             INTEGER PRIMARY KEY,
    number         INTEGER NOT NULL,
    ep_type        TEXT NOT NULL,              -- picks / recap / weekly / nfl_cheatsheet / ...
    date           TEXT NOT NULL,              -- YYYY-MM-DD
    slate          TEXT,
    sport          TEXT,
    template_key   TEXT,
    hook_key       TEXT,
    status         TEXT NOT NULL DEFAULT 'draft',   -- draft / generated / scheduled / posted
    caption_text   TEXT,
    alt_text       TEXT,
    hashtags       TEXT,
    output_dir     TEXT,
    created_utc    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_episodes_type_date ON episodes(ep_type, date);

CREATE TABLE IF NOT EXISTS episode_picks (
    episode_id INTEGER NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
    pick_id    INTEGER NOT NULL REFERENCES picks(id) ON DELETE CASCADE,
    PRIMARY KEY (episode_id, pick_id)
);

CREATE TABLE IF NOT EXISTS counters (
    ep_type     TEXT PRIMARY KEY,
    last_number INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS results (
    pick_id     INTEGER PRIMARY KEY REFERENCES picks(id),
    result      TEXT NOT NULL,                 -- W / L / P
    units_won   REAL NOT NULL,
    graded_utc  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ig_snapshots (
    snapshot_date   TEXT PRIMARY KEY,          -- YYYY-MM-DD
    followers       INTEGER,
    follows         INTEGER,
    media_count     INTEGER,
    reach           INTEGER,
    profile_views   INTEGER,
    accounts_engaged INTEGER,
    pulled_utc      TEXT
);

CREATE TABLE IF NOT EXISTS ig_posts (
    id          INTEGER PRIMARY KEY,
    ig_id       TEXT UNIQUE NOT NULL,
    timestamp   TEXT NOT NULL,
    media_type  TEXT,                          -- IMAGE / CAROUSEL / ...
    caption     TEXT,
    likes       INTEGER,
    comments    INTEGER,
    saves       INTEGER,
    shares      INTEGER,
    reach       INTEGER,
    episode_id  INTEGER REFERENCES episodes(id),
    pulled_utc  TEXT
);

CREATE TABLE IF NOT EXISTS settings (
    key         TEXT PRIMARY KEY,
    value       TEXT
);

-- Seed the sports
INSERT OR IGNORE INTO sports (code, name, enabled) VALUES
    ('nfl', 'NFL', 1),
    ('mlb', 'MLB', 0),
    ('nhl', 'NHL', 0),
    ('nba', 'NBA', 0);

-- Completed games with their closing lines, kept locally so the picker can ask
-- "has this exact bet actually cashed before?" without refetching a season of
-- schedule data every time. Populated by `python main.py backfill`.
--
-- Team abbreviations are normalised to ESPN's spelling on the way in (nflverse
-- says LA/WAS where ESPN says LAR/WSH), so a pairing query never has to know
-- which source a row came from.
CREATE TABLE IF NOT EXISTS game_history (
    id          INTEGER PRIMARY KEY,
    sport       TEXT NOT NULL,                 -- nfl / mlb / nhl
    game_key    TEXT NOT NULL,                 -- source id, used to dedupe
    season      INTEGER,
    week        INTEGER,
    game_date   TEXT NOT NULL,                 -- YYYY-MM-DD
    home_team   TEXT NOT NULL,
    away_team   TEXT NOT NULL,
    home_score  REAL NOT NULL,
    away_score  REAL NOT NULL,
    spread_line REAL,                          -- home-relative, favourite positive
    total_line  REAL,
    source      TEXT,                          -- nflverse / espn
    UNIQUE(sport, game_key)
);

-- A watched parlay is repriced whenever its sport is refreshed.  Keeping the
-- snapshots separate from the current odds table lets the UI draw a genuine
-- movement line without allowing stale book prices into today's model.
CREATE TABLE IF NOT EXISTS pick_snapshots (
    id              INTEGER PRIMARY KEY,
    pick_id         INTEGER NOT NULL REFERENCES picks(id) ON DELETE CASCADE,
    captured_utc    TEXT NOT NULL,
    decimal_odds    REAL NOT NULL,
    implied_prob    REAL NOT NULL,
    model_prob      REAL NOT NULL,
    expected_value  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pick_snapshots_pick
    ON pick_snapshots(pick_id, captured_utc);
CREATE INDEX IF NOT EXISTS idx_history_pair ON game_history(sport, home_team, away_team);
CREATE INDEX IF NOT EXISTS idx_history_date ON game_history(sport, game_date);
