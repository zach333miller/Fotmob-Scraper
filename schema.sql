-- Starter schema for fotmob.db.
-- Build it with:  sqlite3 fotmob.db < schema.sql
--
-- Each scraper writes to the players table. Columns are added as you run
-- successive scripts:
--   build_players_fotmob.py    → seeds the basic identity columns
--   build_transfer_values.py   → fills transfer_value
--   build_player_form.py       → fills the season_* and recent_* columns
--   pull_club_league.py        → fills the club_* columns
--   pull_injuries.py           → fills the injury_* columns
--
-- This file pre-creates all of them so the scrapers can update without
-- having to ALTER TABLE on first run.

CREATE TABLE IF NOT EXISTS players (
    -- Identity (set by build_players_fotmob.py)
    id              TEXT    PRIMARY KEY,  -- name-slug, e.g. "kylian-mbappe"
    name            TEXT    NOT NULL,
    team_id         TEXT    NOT NULL,     -- 3-letter country code from WC_TEAMS
    position        TEXT,                 -- GK / DF / MF / FW
    photo_url       TEXT,                 -- images.fotmob.com CDN URL
    fotmob_id       INTEGER UNIQUE,
    in_squad        INTEGER DEFAULT 1,    -- 1 = in current squad, 0 = cut

    -- Market value (set by build_transfer_values.py)
    transfer_value  INTEGER,              -- EUR

    -- Season aggregates (set by build_player_form.py from mainLeague.stats)
    season_goals    INTEGER,
    season_assists  INTEGER,
    season_minutes  INTEGER,
    season_matches  INTEGER,
    season_started  INTEGER,
    season_rating   REAL,

    -- Last-10-match form (set by build_player_form.py from recentMatches)
    recent_avg_minutes      REAL,
    recent_goals_per_90     REAL,
    recent_assists_per_90   REAL,
    recent_rating           REAL,
    recent_starts           INTEGER,
    recent_matches_seen     INTEGER,

    -- Current club + league (set by pull_club_league.py)
    club_team_name      TEXT,
    club_league_name    TEXT,
    club_league_id      INTEGER,

    -- Availability (set by pull_injuries.py)
    injury_status       TEXT,             -- out / doubt / retired / NULL
    injury_note         TEXT,             -- Fotmob's free-text description
    injury_updated_at   TEXT
);

CREATE INDEX IF NOT EXISTS idx_players_team   ON players(team_id);
CREATE INDEX IF NOT EXISTS idx_players_fotmob ON players(fotmob_id);
