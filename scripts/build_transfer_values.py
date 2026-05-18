"""Pull transferValue per player from Fotmob squads and emit a SQL migration
that ALTERs the players table to add `transfer_value INTEGER` and bulk-updates
every player's value.

Run:
  PYTHONIOENCODING=utf-8 py scripts/build_transfer_values.py \
    > migrations/0014_player_transfer_values.sql
"""
import json
import re
import sys
import time
import unicodedata
import urllib.request
import gzip

sys.path.insert(0, ".")
from backend.scripts.build_players_fotmob import (
    WC_TEAMS,
    USER_AGENT,
    fetch_build_id,
    fetch_team_squad,
    slugify,
)


def main() -> None:
    sys.stderr.write("Fetching Fotmob buildId...\n")
    build_id = fetch_build_id()
    sys.stderr.write(f"  buildId = {build_id}\n\n")

    sys.stderr.write(f"Fetching transferValue for {len(WC_TEAMS)} teams...\n")
    rows: list[tuple[int, int]] = []  # (fotmob_id, transfer_value)
    used = set()
    for our_id, (fotmob_id, slug) in WC_TEAMS.items():
        sys.stderr.write(f"  [{our_id}]... ")
        sys.stderr.flush()
        payload = fetch_team_squad(build_id, fotmob_id, slug)
        if not payload:
            sys.stderr.write("FAILED\n")
            continue
        fb = payload.get("pageProps", {}).get("fallback", {})
        td = fb.get(f"team-{fotmob_id}", {})
        squad_root = td.get("squad", {}).get("squad", [])
        kept = 0
        for grp in squad_root:
            title = (grp.get("title") or "").strip().lower()
            if title not in ("keepers", "goalkeepers", "defenders", "midfielders", "forwards", "attackers"):
                continue
            for m in grp.get("members", []) or []:
                pid = m.get("id")
                tv = m.get("transferValue")
                if not pid or tv is None:
                    continue
                pid = int(pid)
                if pid in used:
                    continue
                used.add(pid)
                rows.append((pid, int(tv)))
                kept += 1
        sys.stderr.write(f"{kept} values\n")
        time.sleep(0.3)

    sys.stderr.write(f"\nTotal transfer values: {len(rows)}\n")

    print("-- Per-player transfer value (EUR) from Fotmob squad payloads.")
    print("-- Used by the projection module to differentiate stars from backups")
    print("-- within the same team+position group (Mbappé ~€129M, Kanté ~€2M).")
    print()
    print("ALTER TABLE players ADD COLUMN transfer_value INTEGER;")
    print()
    if rows:
        # Bulk update via a CASE expression keeps this to one statement.
        print("UPDATE players SET transfer_value = CASE fotmob_id")
        for fid, tv in rows:
            print(f"  WHEN {fid} THEN {tv}")
        print("  ELSE transfer_value END")
        print(f"WHERE fotmob_id IN ({','.join(str(r[0]) for r in rows)});")
    print()
    print(f"-- Total rows updated: {len(rows)}")


if __name__ == "__main__":
    main()
