"""Add WC-bound veterans / stars who weren't in our seed snapshot.

Most of these guys (Ronaldo, Salah, Neymar, Saka, Griezmann, Brozović,
Thiago Silva) are nailed-on starters for their countries but the seeder
pulled rosters from club-side data + a Fotmob snapshot that doesn't always
include them. We add them now and let `build_player_form.py` fill their
stats on the next pass.

After the 2026-05-28 final-26 announcement, we'll either confirm or remove
each entry — for now, false-positives are harmless (in_squad=1 just means
"draftable", they project low if not really playing).

Run:  py scripts/add_missing_stars.py
"""
import json
import re
import sqlite3
import sys
import time
import urllib.request
import urllib.error
import gzip

UA = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "Chrome/130.0.0.0 Safari/537.36"
    ),
    "Accept-Encoding": "gzip",
    "Referer": "https://www.fotmob.com/",
}


def slugify(name: str) -> str:
    # Match scripts/build_players_fotmob.py's slug rule so IDs line
    # up with the hardcoded `is_guaranteed_starter` list in projection.rs.
    s = name.lower()
    s = (
        s.replace("á", "a").replace("é", "e").replace("í", "i").replace("ó", "o")
         .replace("ú", "u").replace("ñ", "n").replace("ç", "c").replace("ã", "a")
         .replace("õ", "o").replace("ü", "u").replace("ö", "o").replace("ä", "a")
         .replace("ß", "ss").replace("'", "").replace(".", "")
    )
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s


def fetch_url(url: str) -> bytes | None:
    req = urllib.request.Request(url, headers=UA)
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                raw = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                return raw
        except Exception as e:
            sys.stderr.write(f"  attempt {attempt+1}: {e}\n")
            time.sleep(1.0 * (attempt + 1))
    return None


def search_player(name: str) -> list[dict]:
    # Fotmob suggest API: returns a dict with a `squadMemberSuggest` array.
    # Each entry has `options[*].payload.{id, teamName}`.
    url = f"https://apigw.fotmob.com/searchapi/suggest?term={urllib.parse.quote(name)}&hits=8"
    raw = fetch_url(url)
    if not raw:
        return []
    try:
        data = json.loads(raw.decode("utf-8", "ignore"))
    except Exception:
        return []
    hits: list[dict] = []
    for entry in data.get("squadMemberSuggest", []):
        for opt in entry.get("options", []):
            p = opt.get("payload") or {}
            if p.get("id"):
                hits.append({
                    "id": p["id"],
                    "name": entry.get("text"),
                    "teamName": p.get("teamName"),
                })
    return hits


# (display_name, team_id, position) — Fotmob ID resolved at runtime.
WANTED = [
    ("Cristiano Ronaldo", "por", "FW"),
    ("Mohamed Salah", "egy", "FW"),
    ("Bukayo Saka", "eng", "FW"),
    ("Antoine Griezmann", "fra", "FW"),
    ("Marcelo Brozović", "cro", "MF"),
    ("Neymar Jr", "bra", "FW"),
    ("Thiago Silva", "bra", "DF"),
    ("Marquinhos", "bra", "DF"),
    ("Casemiro", "bra", "MF"),  # in_squad?
    ("Dani Carvajal", "esp", "DF"),
    ("Toni Kroos", "ger", "MF"),  # retired but harmless
    ("İlkay Gündoğan", "ger", "MF"),
    ("Manuel Neuer", "ger", "GK"),
    ("Antonio Rüdiger", "ger", "DF"),
    ("Jamal Musiala", "ger", "MF"),
    ("Niclas Füllkrug", "ger", "FW"),
    ("Memphis Depay", "ned", "FW"),
    ("Virgil van Dijk", "ned", "DF"),
    ("Frenkie de Jong", "ned", "MF"),
    ("Romelu Lukaku", "bel", "FW"),
    ("Hakim Ziyech", "mar", "FW"),
    ("Achraf Hakimi", "mar", "DF"),
]


def main() -> None:
    con = sqlite3.connect("fotmob.db", timeout=30)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=30000")
    cur = con.cursor()

    added = 0
    skipped = 0
    failed = 0
    for display_name, team_id, position in WANTED:
        slug = slugify(display_name)
        existing = cur.execute(
            "SELECT id FROM players WHERE id=? OR (name=? AND team_id=?)",
            (slug, display_name, team_id),
        ).fetchone()
        if existing:
            sys.stderr.write(f"  EXISTS: {display_name} ({existing[0]})\n")
            skipped += 1
            continue

        sys.stderr.write(f"  search {display_name}... ")
        sys.stderr.flush()
        hits = search_player(display_name)
        if not hits:
            sys.stderr.write("no hits\n")
            failed += 1
            continue
        # Prefer the first hit (Fotmob ranks by relevance/popularity).
        h = hits[0]
        fid = h.get("id")
        if not fid:
            sys.stderr.write("no fotmob id\n")
            failed += 1
            continue
        sys.stderr.write(f"fid={fid} ({h.get('teamName')})\n")

        cur.execute(
            """INSERT INTO players (id, name, team_id, position, fotmob_id, in_squad, transfer_value)
               VALUES (?, ?, ?, ?, ?, 1, NULL)""",
            (slug, display_name, team_id, position, int(fid)),
        )
        added += 1
        time.sleep(0.4)  # be polite to Fotmob

    con.commit()
    con.close()
    sys.stderr.write(f"\nDone. added={added}  skipped={skipped}  failed={failed}\n")
    sys.stderr.write("Next: run scripts/build_player_form.py to fetch their stats.\n")


if __name__ == "__main__":
    main()
