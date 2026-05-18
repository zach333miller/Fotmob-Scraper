"""Refresh injury / availability flags for every in-squad player.

Reads `status` + `injuryInformation` from each Fotmob player profile and
maps to one of: out / doubt / susp / NULL. Always re-runs every player —
unlike form data, injury status changes daily, so we want this fresh.

Run:  py scripts/pull_injuries.py
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
SLEEP_S = 0.25


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


import datetime as _dt
import re as _re

# Fotmob exposes injury info for the player's CURRENT club season. Most
# of those guys will be healthy by WC kickoff (their club season ends
# late May; the WC starts June 11). We only want to flag a player when
# we can confidently say their projected return date is AFTER WC kickoff
# — otherwise we'd scare owners off Yamal / Modric / Salah etc. who are
# very likely to play.
WC_KICKOFF = _dt.date(2026, 6, 11)
# A small buffer day before kickoff. If they're due back Jun 8, that's
# tight but they'll have a chance — call it "doubt", not "out".
WC_DOUBT_FROM = _dt.date(2026, 6, 1)

_MONTHS = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3,
    "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7,
    "august": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12,
}


def _parse_return_date(text: str) -> _dt.date | None:
    """Best-effort parse of Fotmob's `expectedReturnFallback` free-text.

    Returns the LATEST plausible day in the named window, so we can ask
    "is the player back BY WC kickoff?". The "Early / Mid / Late" prefix
    matters a lot:
      "Early June 2026"  → latest ~June 10  (i.e. before kickoff June 11)
      "Mid June 2026"    → latest ~June 20
      "Late June 2026"   → latest June 30
      bare "June 2026"   → latest June 30  (could be any time that month)

    Returns None when nothing parseable (vague text like "Doubtful" or
    "A few weeks"). Those don't get flagged — we don't speculate.
    """
    if not text:
        return None
    t = text.lower().replace("-", " ")
    m = _re.search(r"\b(\d{4})\b", t)
    if not m:
        return None
    year = int(m.group(1))
    month_num: int | None = None
    for word, num in _MONTHS.items():
        if _re.search(rf"\b{word}\b", t):
            month_num = num
            break
    if month_num is None:
        return None
    # Latest plausible day given the qualifier (or whole month if bare).
    if "early" in t:
        day = 10
    elif "mid" in t:
        day = 20
    else:
        # "Late X" or bare "X" — end of month.
        if month_num == 12:
            return _dt.date(year + 1, 1, 1) - _dt.timedelta(days=1)
        return _dt.date(year, month_num + 1, 1) - _dt.timedelta(days=1)
    return _dt.date(year, month_num, day)


def classify_injury(p: dict) -> tuple[str | None, str | None]:
    """Returns (injury_status, injury_note) — but only when there's reason
    to believe the player will miss the World Cup, not just their club season.

    A current "Out for season" usually means the 2025-26 club season ending
    in May — which is BEFORE the WC kicks off — so we don't surface it.
    We only flag when the expected return date falls at or after WC kickoff.
    """
    status = (p.get("status") or "").lower()
    info = p.get("injuryInformation")

    # Non-active Fotmob statuses (retired, suspended) — always relevant.
    if status and status not in ("active", ""):
        return (status[:8], None)

    if not info:
        return (None, None)

    name = info.get("name") or ""
    er = info.get("expectedReturn") or {}
    fallback = er.get("expectedReturnFallback") or ""

    return_date = _parse_return_date(fallback)

    if return_date and return_date >= WC_KICKOFF:
        # Confirmed they won't be back in time.
        note = " · ".join(b for b in (name, fallback) if b).strip(" ·") or None
        return ("out", note)

    if return_date and return_date >= WC_DOUBT_FROM:
        # Return falls in the WC window — too late for full prep, flag doubt.
        note = " · ".join(b for b in (name, fallback) if b).strip(" ·") or None
        return ("doubt", note)

    # Either return is before WC ("Out for season" = end of May 2026, fit
    # for WC) OR fallback is vague ("A few weeks", "Doubtful", "Unknown")
    # and we can't responsibly call it a WC issue. Leave as NULL so the
    # UI doesn't scare owners off players who'll likely be fine.
    return (None, None)


def main() -> None:
    con = sqlite3.connect("fotmob.db", timeout=30)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=30000")
    cur = con.cursor()

    rows = cur.execute(
        "SELECT id, name, fotmob_id FROM players \
         WHERE fotmob_id IS NOT NULL AND in_squad = 1 ORDER BY id"
    ).fetchall()
    sys.stderr.write(f"  {len(rows)} players to refresh\n")

    sys.stderr.write("Fetching Fotmob buildId...\n")
    build_id = fetch_build_id()
    sys.stderr.write(f"  buildId = {build_id}\n\n")

    flagged = 0
    cleared = 0
    failed = 0
    for i, (pid, name, fotmob_id) in enumerate(rows, 1):
        if i % 50 == 0:
            con.commit()
        payload = fetch_player(build_id, int(fotmob_id))
        if not payload:
            failed += 1
            time.sleep(SLEEP_S)
            continue
        p = payload.get("pageProps", {}).get("fallback", {}).get(f"player:{fotmob_id}")
        if not p:
            failed += 1
            time.sleep(SLEEP_S)
            continue
        st, note = classify_injury(p)
        cur.execute(
            "UPDATE players SET injury_status = ?, injury_note = ?, \
             injury_updated_at = datetime('now') WHERE id = ?",
            (st, note, pid),
        )
        if st:
            flagged += 1
            sys.stderr.write(f"  [{i}/{len(rows)}] {name[:30]:30s} -> {st} ({note or '-'})\n")
        else:
            cleared += 1
        time.sleep(SLEEP_S)

    con.commit()
    con.close()
    sys.stderr.write(f"\nDone — {flagged} flagged, {cleared} healthy, {failed} failed\n")


if __name__ == "__main__":
    main()
