"""Pull per-player season + recent-form stats from Fotmob and emit a
migration that updates the players table. Powers a smarter projection
model: real club form replaces transfer-value-as-quality-proxy.

For each player with a fotmob_id, fetches their profile page and pulls:
  - mainLeague stats: season goals/assists/minutes/matches/rating
  - recentMatches: last ~10 matches → average minutes, g/90, a/90, rating

Run:  PYTHONIOENCODING=utf-8 py scripts/build_player_form.py \
        > migrations/0019_player_form_data.sql
"""
import json
import re
import sys
import time
import urllib.error
import urllib.request
import gzip
import sqlite3

UA = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "Chrome/130.0.0.0 Safari/537.36"
    ),
    "Accept-Encoding": "gzip",
    "Referer": "https://www.fotmob.com/",
}

# Look back at most this many recent matches when computing "recent form".
# Bigger window = more stable average, smaller = more reactive to current
# hot/cold streaks. 10 is the sweet spot for fantasy.
WINDOW = 10
SLEEP_S = 0.35  # be polite to Fotmob


def fetch_build_id() -> str:
    req = urllib.request.Request("https://www.fotmob.com/", headers=UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
    m = re.search(r'"buildId":"([^"]+)"', raw.decode("utf-8", "ignore"))
    if not m:
        raise RuntimeError("buildId not found")
    return m.group(1)


def fetch_player(build_id: str, fotmob_id: int) -> dict | None:
    url = (
        f"https://www.fotmob.com/_next/data/{build_id}/en/players/{fotmob_id}/x.json"
    )
    req = urllib.request.Request(url, headers=UA)
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                raw = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                return json.loads(raw.decode("utf-8", "ignore"))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            sys.stderr.write(f"  attempt {attempt + 1} HTTP {e.code}\n")
        except Exception as e:
            sys.stderr.write(f"  attempt {attempt + 1} err: {e}\n")
        time.sleep(1.5 * (attempt + 1))
    return None


def extract_stats(payload: dict, fotmob_id: int) -> dict | None:
    fb = payload.get("pageProps", {}).get("fallback", {})
    p = fb.get(f"player:{fotmob_id}")
    if not p:
        return None

    out: dict = {}

    # Season aggregate from mainLeague.stats — list of {title, value}.
    ml = p.get("mainLeague")
    if isinstance(ml, dict):
        stats = ml.get("stats") or []
        by_title = {s.get("title", "").lower(): s.get("value") for s in stats if isinstance(s, dict)}
        out["season_goals"] = _int(by_title.get("goals"))
        out["season_assists"] = _int(by_title.get("assists"))
        out["season_minutes"] = _int(by_title.get("minutes played"))
        out["season_matches"] = _int(by_title.get("matches"))
        out["season_started"] = _int(by_title.get("started"))
        out["season_rating"] = _float(by_title.get("rating"))

    # Recent form — walk the last WINDOW matches, sum stats, average rating.
    rm = p.get("recentMatches") or []
    if not isinstance(rm, list):
        rm = []
    # Filter to matches where the player actually appeared. onBench=true OR
    # minutesPlayed=0 means they didn't play.
    appeared = [
        m
        for m in rm
        if isinstance(m, dict)
        and not m.get("onBench")
        and (m.get("minutesPlayed") or 0) > 0
    ]
    window = appeared[:WINDOW]
    if window:
        total_mins = sum(m.get("minutesPlayed", 0) for m in window)
        total_goals = sum(m.get("goals", 0) for m in window)
        total_assists = sum(m.get("assists", 0) for m in window)
        ratings: list[float] = []
        for m in window:
            rp = m.get("ratingProps") or {}
            r = rp.get("rating")
            try:
                ratings.append(float(r))
            except (TypeError, ValueError):
                pass
        out["recent_avg_minutes"] = round(total_mins / len(window), 2)
        out["recent_goals_per_90"] = (
            round(total_goals * 90.0 / total_mins, 3) if total_mins > 0 else 0.0
        )
        out["recent_assists_per_90"] = (
            round(total_assists * 90.0 / total_mins, 3) if total_mins > 0 else 0.0
        )
        out["recent_rating"] = round(sum(ratings) / len(ratings), 2) if ratings else None
        out["recent_starts"] = len(window)
        out["recent_matches_seen"] = len(rm)
    return out


def _int(v) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _float(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def sql_value(v) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, (int, float)):
        return str(v)
    return "'" + str(v).replace("'", "''") + "'"


def main() -> None:
    db_path = "fotmob.db"
    sys.stderr.write(f"Updating {db_path} directly (no migration file)...\n")
    # 30s busy timeout — backend may briefly hold the writer lock during
    # Fotmob ingest. Without this we crash on the first contention.
    con = sqlite3.connect(db_path, timeout=30.0)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=30000")
    cur = con.cursor()
    # Skip players who already have form data — makes the script resumable
    # after a crash + cheap to re-run weekly to refresh stale entries.
    rows = cur.execute(
        "SELECT id, name, fotmob_id FROM players \
         WHERE fotmob_id IS NOT NULL AND in_squad = 1 AND recent_rating IS NULL"
    ).fetchall()
    sys.stderr.write(f"  {len(rows)} players to fetch\n")

    sys.stderr.write("Fetching Fotmob buildId...\n")
    build_id = fetch_build_id()
    sys.stderr.write(f"  buildId = {build_id}\n\n")

    ALL_COLS = [
        "season_goals",
        "season_assists",
        "season_minutes",
        "season_matches",
        "season_started",
        "season_rating",
        "recent_avg_minutes",
        "recent_goals_per_90",
        "recent_assists_per_90",
        "recent_rating",
        "recent_starts",
        "recent_matches_seen",
    ]

    ok = 0
    skipped = 0
    for i, (pid, name, fotmob_id) in enumerate(rows, 1):
        if i % 50 == 0:
            con.commit()  # checkpoint every 50 rows
        sys.stderr.write(f"  [{i}/{len(rows)}] {name[:30]:30s} ... ")
        sys.stderr.flush()
        payload = fetch_player(build_id, int(fotmob_id))
        if not payload:
            sys.stderr.write("MISS\n")
            skipped += 1
            time.sleep(SLEEP_S)
            continue
        stats = extract_stats(payload, int(fotmob_id))
        if not stats:
            sys.stderr.write("no-data\n")
            skipped += 1
            time.sleep(SLEEP_S)
            continue

        set_parts = []
        bind_vals: list[object] = []
        for k in ALL_COLS:
            if k in stats:
                set_parts.append(f"{k} = ?")
                bind_vals.append(stats[k])
        if not set_parts:
            skipped += 1
            time.sleep(SLEEP_S)
            continue
        bind_vals.append(pid)
        cur.execute(
            f"UPDATE players SET {', '.join(set_parts)} WHERE id = ?",
            bind_vals,
        )
        ok += 1
        sys.stderr.write(
            f"S:{stats.get('season_goals','?')}g/{stats.get('season_assists','?')}a "
            f"R:{stats.get('recent_rating','?')} "
            f"M:{stats.get('recent_avg_minutes','?')}\n"
        )
        time.sleep(SLEEP_S)

    con.commit()
    con.close()
    sys.stderr.write(f"\nDone — {ok} updated, {skipped} skipped\n")


if __name__ == "__main__":
    main()
