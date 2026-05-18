"""Generate migration 0010_seed_players_fotmob.sql from Fotmob squads.

For each of the 48 WC 2026 teams, fetches the team's Fotmob squad page via
the Next.js `_next/data` endpoint (unsigned, returns SWR-cached team data),
parses the squad list, and emits a REPLACE migration that wipes the noisy
Wikidata players seed and inserts the current Fotmob roster.

Player photos use Fotmob's CDN pattern:
  https://images.fotmob.com/image_resources/playerimages/{fotmob_id}.png

Also persists `fotmob_player_id` on each row so per-match ratings ingestion
(later — when we wire Fotmob's matchDetails endpoint) joins cleanly.

Run with:
  PYTHONIOENCODING=utf-8 py scripts/build_players_fotmob.py \
    > migrations/0010_seed_players_fotmob.sql
"""
import json
import re
import sys
import time
import unicodedata
import urllib.parse
import urllib.request
from collections import OrderedDict

# our team_id -> (fotmob team_id, slug used in the URL path)
# Sourced from Fotmob's WC league overview (league_id=77).
WC_TEAMS: "OrderedDict[str, tuple[int, str]]" = OrderedDict([
    ("alg", (6317, "algeria")),
    ("arg", (6706, "argentina")),
    ("aus", (6716, "australia")),
    ("aut", (8255, "austria")),
    ("bel", (8263, "belgium")),
    ("bih", (10106, "bosnia-and-herzegovina")),
    ("bra", (8256, "brazil")),
    ("can", (5810, "canada")),
    ("cpv", (5888, "cape-verde")),
    ("col", (8258, "colombia")),
    ("cro", (10155, "croatia")),
    ("cuw", (287981, "curacao")),
    ("cze", (8496, "czechia")),
    ("cod", (6321, "dr-congo")),
    ("ecu", (6707, "ecuador")),
    ("egy", (10255, "egypt")),
    ("eng", (8491, "england")),
    ("fra", (6723, "france")),
    ("ger", (8570, "germany")),
    ("gha", (6714, "ghana")),
    ("hai", (5934, "haiti")),
    ("irn", (6711, "iran")),
    ("irq", (5819, "iraq")),
    ("civ", (6709, "ivory-coast")),
    ("jpn", (6715, "japan")),
    ("jor", (5816, "jordan")),
    ("mex", (6710, "mexico")),
    ("mar", (6262, "morocco")),
    ("ned", (6708, "netherlands")),
    ("nzl", (5820, "new-zealand")),
    ("nor", (8492, "norway")),
    ("pan", (5922, "panama")),
    ("par", (6724, "paraguay")),
    ("por", (8361, "portugal")),
    ("qat", (5902, "qatar")),
    ("ksa", (7795, "saudi-arabia")),
    ("sco", (8498, "scotland")),
    ("sen", (6395, "senegal")),
    ("rsa", (6316, "south-africa")),
    ("kor", (7804, "south-korea")),
    ("esp", (6720, "spain")),
    ("swe", (8520, "sweden")),
    ("sui", (6717, "switzerland")),
    ("tun", (6719, "tunisia")),
    ("tur", (6595, "turkiye")),
    ("uru", (5796, "uruguay")),
    ("usa", (6713, "usa")),
    ("uzb", (8700, "uzbekistan")),
])

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)

# Map Fotmob position-group titles to our 2-letter position codes.
TITLE_TO_POS = {
    "keepers": "GK",
    "goalkeepers": "GK",
    "defenders": "DF",
    "midfielders": "MF",
    "forwards": "FW",
    "attackers": "FW",
}


def fetch_build_id() -> str:
    req = urllib.request.Request(
        "https://www.fotmob.com/",
        headers={"User-Agent": USER_AGENT, "Accept-Encoding": "gzip"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        import gzip, io
        raw = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
        text = raw.decode("utf-8", "ignore")
    m = re.search(r'"buildId":"([^"]+)"', text)
    if not m:
        raise RuntimeError("buildId not found on Fotmob homepage")
    return m.group(1)


def fetch_team_squad(build_id: str, fotmob_id: int, slug: str) -> dict | None:
    url = (
        f"https://www.fotmob.com/_next/data/{build_id}/en/teams/{fotmob_id}/squad/{slug}.json"
    )
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip",
            "Referer": "https://www.fotmob.com/",
        },
    )
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                import gzip
                raw = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                return json.loads(raw.decode("utf-8", "ignore"))
        except Exception as e:
            sys.stderr.write(f"    attempt {attempt + 1} failed: {e}\n")
            time.sleep(2 * (attempt + 1))
    return None


def extract_players(payload: dict, fotmob_team_id: int) -> list[dict]:
    fallback = payload.get("pageProps", {}).get("fallback", {})
    team_data = fallback.get(f"team-{fotmob_team_id}", {})
    squad_root = team_data.get("squad", {})
    groups = squad_root.get("squad", [])
    out = []
    for grp in groups:
        title = (grp.get("title") or "").strip().lower()
        pos = TITLE_TO_POS.get(title)
        if not pos:
            continue  # skip coaches, staff, etc.
        for m in grp.get("members", []) or []:
            pid = m.get("id")
            name = (m.get("name") or "").strip()
            if not pid or not name:
                continue
            out.append({
                "fotmob_id": int(pid),
                "name": name,
                "position": pos,
                "age": m.get("age"),
                "dob": m.get("dateOfBirth"),
                # Player market value in EUR. Drives per-player projection
                # spread — Mbappé ~€129M, a backup keeper ~€500K.
                "transfer_value": m.get("transferValue"),
            })
    return out


def slugify(name: str) -> str:
    normalized = unicodedata.normalize("NFKD", name)
    ascii_only = "".join(c for c in normalized if not unicodedata.combining(c))
    chars = []
    for c in ascii_only.lower():
        if c.isalnum():
            chars.append(c)
        elif c in (" ", "-", "'"):
            chars.append("-")
    slug = "".join(chars)
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug.strip("-")


def sq(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def main() -> None:
    sys.stderr.write("Fetching Fotmob buildId...\n")
    build_id = fetch_build_id()
    sys.stderr.write(f"  buildId = {build_id}\n\n")

    sys.stderr.write(f"Fetching squads for {len(WC_TEAMS)} teams...\n")
    all_rows: list[str] = []
    used_slugs: set[str] = set()
    used_fotmob_ids: set[int] = set()
    total = 0

    for our_id, (fotmob_id, slug) in WC_TEAMS.items():
        sys.stderr.write(f"  [{our_id}] fotmob_id={fotmob_id}... ")
        sys.stderr.flush()
        payload = fetch_team_squad(build_id, fotmob_id, slug)
        if not payload:
            sys.stderr.write("FAILED\n")
            continue
        players = extract_players(payload, fotmob_id)
        kept = 0
        for p in players:
            if p["fotmob_id"] in used_fotmob_ids:
                continue
            used_fotmob_ids.add(p["fotmob_id"])

            slug_id = slugify(p["name"])
            if not slug_id:
                slug_id = f"player-{p['fotmob_id']}"
            if slug_id in used_slugs:
                slug_id = f"{slug_id}-{our_id}"
            used_slugs.add(slug_id)

            photo_url = (
                f"https://images.fotmob.com/image_resources/playerimages/{p['fotmob_id']}.png"
            )
            all_rows.append(
                f"  ({sq(slug_id)}, {sq(p['name'])}, {sq(our_id)}, {sq(p['position'])}, "
                f"{sq(photo_url)}, {p['fotmob_id']}, 1)"
            )
            kept += 1
            total += 1
        sys.stderr.write(f"{len(players)} squad, {kept} new\n")
        time.sleep(0.35)

    sys.stderr.write(f"\nTotal players: {total}\n")

    # Emit migration. UPSERT so re-runs (e.g. when final squads land 1-2 weeks
    # pre-kickoff) refresh data without dropping fantasy_rosters / fantasy_lineups
    # for owners who've already drafted. Cuts are handled by first marking
    # every player out-of-squad, then the upsert flips the survivors back to
    # in_squad=1 — anyone Fotmob no longer lists stays at in_squad=0 and
    # disappears from the draft pool.
    print("-- WC 2026 squads from Fotmob's Next.js page-data endpoint.")
    print("-- 48 national teams via league 77 overview. Photos from images.fotmob.com.")
    print("-- Generated by scripts/build_players_fotmob.py — re-runnable.")
    print()
    print("-- Step 1: provisionally cut every player. The UPSERT below flips")
    print("--         the survivors back to in_squad = 1.")
    print("UPDATE players SET in_squad = 0;")
    print()
    if all_rows:
        print(
            "INSERT INTO players (id, name, team_id, position, photo_url, fotmob_id, in_squad) VALUES"
        )
        print(",\n".join(all_rows))
        print("ON CONFLICT(id) DO UPDATE SET")
        print("    name = excluded.name,")
        print("    team_id = excluded.team_id,")
        print("    position = excluded.position,")
        print("    photo_url = excluded.photo_url,")
        print("    fotmob_id = excluded.fotmob_id,")
        print("    in_squad = 1;")
    print()
    print(f"-- Total players: {total}")


if __name__ == "__main__":
    main()
