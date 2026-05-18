"""Backfill club_team_name + club_league_name for every in-squad player.

Lower-league players (South African PSL, MLS, Uzbek SL, etc.) were inflating
the recent-rating leaderboard because an 8.0 in those leagues isn't worth
what an 8.0 in the Premier League is. This script populates each player's
club + league so the API can apply a league-strength adjustment.

Hits the same Fotmob player profile endpoint as build_player_form.py and
reads `primaryTeam.teamName` + `mainLeague.leagueName/leagueId`.

Skips players who already have a non-null club_league_name — re-runnable
to fill in newly-added players (e.g. Ronaldo, Salah, Saka added today).

Run:  py scripts/pull_club_league.py
"""
import gzip
import json
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request

UA = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "Chrome/130.0.0.0 Safari/537.36"
    ),
    "Accept-Encoding": "gzip",
    "Referer": "https://www.fotmob.com/",
}
SLEEP_S = 0.3


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
    url = f"https://www.fotmob.com/_next/data/{build_id}/en/players/{fotmob_id}/x.json"
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
        time.sleep(1.0 * (attempt + 1))
    return None


def extract_club(payload: dict, fotmob_id: int) -> tuple[str | None, str | None, int | None]:
    p = payload.get("pageProps", {}).get("fallback", {}).get(f"player:{fotmob_id}")
    if not p:
        return None, None, None
    team = p.get("primaryTeam") or {}
    league = p.get("mainLeague") or {}
    return (
        team.get("teamName"),
        league.get("leagueName"),
        league.get("leagueId"),
    )


def main() -> None:
    con = sqlite3.connect("fotmob.db", timeout=30)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=30000")
    cur = con.cursor()

    rows = cur.execute(
        "SELECT id, name, fotmob_id FROM players "
        "WHERE fotmob_id IS NOT NULL AND in_squad = 1 "
        "AND club_league_name IS NULL"
    ).fetchall()
    sys.stderr.write(f"  {len(rows)} players to fetch\n")

    sys.stderr.write("Fetching Fotmob buildId...\n")
    build_id = fetch_build_id()
    sys.stderr.write(f"  buildId = {build_id}\n\n")

    ok = 0
    fail = 0
    for i, (pid, name, fotmob_id) in enumerate(rows, 1):
        if i % 50 == 0:
            con.commit()
        sys.stderr.write(f"  [{i}/{len(rows)}] {name[:30]:30s} ... ")
        sys.stderr.flush()
        payload = fetch_player(build_id, int(fotmob_id))
        if not payload:
            sys.stderr.write("MISS\n")
            fail += 1
            time.sleep(SLEEP_S)
            continue
        club, league, league_id = extract_club(payload, int(fotmob_id))
        if not league:
            sys.stderr.write("no-league\n")
            fail += 1
            time.sleep(SLEEP_S)
            continue
        cur.execute(
            "UPDATE players SET club_team_name=?, club_league_name=?, club_league_id=? "
            "WHERE id=?",
            (club, league, league_id, pid),
        )
        ok += 1
        sys.stderr.write(f"{club} / {league}\n")
        time.sleep(SLEEP_S)

    con.commit()
    con.close()
    sys.stderr.write(f"\nDone — {ok} updated, {fail} skipped/failed\n")


if __name__ == "__main__":
    main()
