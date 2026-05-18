# Fotmob Scraper

A small collection of Python scripts that pull football data from [Fotmob](https://www.fotmob.com/) by reading the JSON its Next.js front-end already serves to anonymous browsers. No API key, no signed headers — just `urllib` + the stdlib.

Extracted from a personal World Cup 2026 fantasy app I was building. Published here because the access pattern turned out to be more interesting than the app itself.

## Why this exists

Fotmob has [excellent free football data](https://www.fotmob.com/) — match ratings, expected goals, recent form, injury status, full match histories. There's no public API. The official mobile/web apps reach an internal endpoint behind a signed `x-mas` header that rotates per build.

But Fotmob's website is a [Next.js](https://nextjs.org/) application. Every page server-renders a JSON blob into the HTML, and Next.js exposes that same JSON at `/_next/data/{buildId}/...` for client-side route transitions. **That endpoint has no authentication, no signed headers, and no rate limit you'll hit at reasonable use** — it's the same data anonymous browsers download for normal page loads.

The unlock is two steps:

1. Scrape the build ID from the homepage HTML (`"buildId":"..."` in a runtime config blob).
2. Hit `https://www.fotmob.com/_next/data/{buildId}/en/players/{id}/x.json` for a complete player payload.

Build IDs rotate per Fotmob deploy. Re-scrape when a request 404s.

## What's in here

Six scripts, each one a thin wrapper around the same `_next/data` pattern but targeting a different surface of Fotmob:

| Script | What it pulls |
|---|---|
| [`build_players_fotmob.py`](scripts/build_players_fotmob.py) | Squad rosters for all 48 World Cup 2026 teams — name, position, age, transfer value, photo URL |
| [`build_player_form.py`](scripts/build_player_form.py) | Per-player season stats (goals, assists, minutes, rating) + last 10-match form averages |
| [`build_transfer_values.py`](scripts/build_transfer_values.py) | Per-player market valuation in EUR |
| [`pull_club_league.py`](scripts/pull_club_league.py) | Primary club + main league for every player |
| [`pull_injuries.py`](scripts/pull_injuries.py) | Injury status + free-text expected-return dates, parsed into structured form |
| [`add_missing_stars.py`](scripts/add_missing_stars.py) | Search Fotmob by name and insert any missing veterans (Ronaldo, Salah, etc.) by their Fotmob ID |

All six share the same skeleton: `fetch_build_id()` → `fetch_player()` / `fetch_team_squad()` → extract → write to a local SQLite file.

## How it works

The interesting bits, briefly:

**`_next/data` endpoint**. Every Next.js page has a JSON sibling at `/_next/data/{buildId}/{locale}/{route}.json`. For Fotmob:

- Player profile: `/_next/data/{buildId}/en/players/{fotmob_id}/x.json`
- Team squad: `/_next/data/{buildId}/en/teams/{fotmob_id}/squad/{slug}.json`
- Search: `https://apigw.fotmob.com/searchapi/suggest?term={name}&hits=8` (separate API, also unauthenticated)

**Build ID caching**. The build ID changes on every Fotmob deploy. The scripts fetch the homepage once at startup and regex `"buildId":"([^"]+)"` out of the embedded `__NEXT_DATA__` blob. If a `_next/data` request 404s, refresh the build ID and retry — that means a deploy landed mid-session.

**Payload shape**. Each response wraps the actual data in `pageProps.fallback.player:{id}` (or `team-{id}`). After unwrapping you get a `dict` with fields like `recentMatches`, `injuryInformation`, `mainLeague.stats`, `careerHistory`. The extractors live in each script's `extract_*` function.

**Rate limiting**. Empirically Fotmob doesn't fingerprint at 4 req/sec sustained. The scripts sleep ~250-350ms between requests as a courtesy. Don't go full speed against an undocumented endpoint.

**Injury date parsing**. `pull_injuries.py` is the most interesting standalone bit. Fotmob's `injuryInformation.expectedReturn.expectedReturnFallback` is free text — `"Mid October 2026"`, `"Late May 2026"`, `"Out for season"`, `"A few weeks"`. The script parses the date-bearing forms ("Early/Mid/Late {Month} {Year}") and lets you filter for "will this player miss a specific date" (in my case, the World Cup kickoff on June 11, 2026). Strings that don't parse cleanly fall through as "no flag" — better than guessing.

## Setup

Python 3.10+. No dependencies — stdlib only.

```bash
git clone https://github.com/zach333miller/Fotmob-Scraper
cd Fotmob-Scraper

# Create the SQLite database with the expected schema
sqlite3 fotmob.db < schema.sql
```

## Running the scripts

Each script writes to a local `fotmob.db` SQLite file in the current directory.

```bash
# 1. Seed the players table with all 48 WC 2026 team rosters.
#    Outputs a SQL migration to stdout — pipe it into sqlite3.
PYTHONIOENCODING=utf-8 python scripts/build_players_fotmob.py | sqlite3 fotmob.db

# 2. Backfill transfer values from Fotmob squads.
PYTHONIOENCODING=utf-8 python scripts/build_transfer_values.py | sqlite3 fotmob.db

# 3. Pull recent-form stats for every player.
PYTHONIOENCODING=utf-8 python scripts/build_player_form.py

# 4. Pull current club + main league for every player.
PYTHONIOENCODING=utf-8 python scripts/pull_club_league.py

# 5. Refresh injury status (run daily).
PYTHONIOENCODING=utf-8 python scripts/pull_injuries.py

# 6. Add veterans Fotmob doesn't include in current club squads.
PYTHONIOENCODING=utf-8 python scripts/add_missing_stars.py
```

Total time for steps 1-5 against ~1,400 players: about 15 minutes wall.

## Schema

The scripts assume a `players` table with the columns in [`schema.sql`](schema.sql). The minimum is:

```sql
CREATE TABLE players (
    id TEXT PRIMARY KEY,           -- name-slug, e.g. "kylian-mbappe"
    name TEXT NOT NULL,
    team_id TEXT NOT NULL,         -- 3-letter country code
    position TEXT,                 -- GK / DF / MF / FW
    fotmob_id INTEGER UNIQUE,
    photo_url TEXT,
    in_squad INTEGER DEFAULT 1,
    transfer_value INTEGER
);
```

Plus form columns added by `build_player_form.py`, club columns added by `pull_club_league.py`, and injury columns added by `pull_injuries.py`. See `schema.sql` for the full picture.

## ⚠️ Disclaimer

This accesses an internal Fotmob endpoint that has no documented stability guarantee — Fotmob can change it on any deploy. The library is not officially supported by Fotmob; this is an unofficial scraper built by analyzing the public web app. Use within Fotmob's [Terms of Service](https://www.fotmob.com/terms-of-service): for personal / non-commercial use, no large-scale redistribution of their data.

## License

MIT — see [LICENSE](LICENSE).
