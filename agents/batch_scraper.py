#!/usr/bin/env python3
"""
Batch scraper: fetch pitcher stats from The Baseball Cube for multiple teams
and generate app-compatible JSON files for the pitching hub dashboard.

Usage:
    cd agents
    source venv/bin/activate
    python batch_scraper.py
"""

import json
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
SEASON_YEAR = 2025
TEAMS_DIR = Path(__file__).resolve().parent.parent / "data" / "teams"

TEAMS = [
    {"name": "Campbell",       "slug": "campbell",      "ncaa_id": "20880", "conf": "CAA"},
    {"name": "Charleston",     "slug": "charleston",    "ncaa_id": "21014", "conf": "CAA"},
    {"name": "Elon",           "slug": "elon",          "ncaa_id": "20537", "conf": "CAA"},
    {"name": "Hofstra",        "slug": "hofstra",       "ncaa_id": "20393", "conf": "CAA"},
    {"name": "LSU",            "slug": "lsu",           "ncaa_id": "20004", "conf": "SEC"},
    {"name": "Monmouth",       "slug": "monmouth",      "ncaa_id": "22104", "conf": "CAA"},
    {"name": "NC A&T",         "slug": "nc-a-t",        "ncaa_id": "20412", "conf": "CAA"},
    {"name": "Northeastern",   "slug": "northeastern",  "ncaa_id": "20328", "conf": "CAA"},
    {"name": "Stony Brook",    "slug": "stony-brook",   "ncaa_id": "20631", "conf": "CAA"},
    {"name": "Towson",         "slug": "towson",        "ncaa_id": "20435", "conf": "CAA"},
    {"name": "UNCW",           "slug": "uncw",          "ncaa_id": "20652", "conf": "CAA"},
    # William & Mary already has data from CrewAI pipeline
]

HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Connection": "keep-alive",
}

_last_request_time: float = 0.0

# ---------------------------------------------------------------------------
# Helpers (mirrored from ncaa_scraper.py)
# ---------------------------------------------------------------------------


def _rate_limit():
    global _last_request_time
    elapsed = time.time() - _last_request_time
    if elapsed < 1.5:
        time.sleep(1.5 - elapsed)
    _last_request_time = time.time()


def _fetch(url: str) -> BeautifulSoup:
    _rate_limit()
    resp = requests.get(url, headers=HTTP_HEADERS, timeout=30)
    resp.raise_for_status()
    return BeautifulSoup(resp.text, "html.parser")


def _safe_float(val, default=0.0):
    try:
        cleaned = val.strip().replace(",", "")
        if cleaned in ("", "-", "INF", "\u2014", "\u2013", "*"):
            return default
        return float(cleaned)
    except (ValueError, AttributeError):
        return default


def _safe_int(val, default=0):
    try:
        cleaned = val.strip().replace(",", "")
        if cleaned in ("", "-", "\u2014", "\u2013", "*"):
            return default
        return int(float(cleaned))
    except (ValueError, AttributeError):
        return default


# ---------------------------------------------------------------------------
# Scraping
# ---------------------------------------------------------------------------


def scrape_pitchers(ncaa_id: str, team_name: str) -> list[dict]:
    """Scrape pitcher stats from The Baseball Cube for a team."""
    url = f"https://www.thebaseballcube.com/content/stats_college/{SEASON_YEAR}~{ncaa_id}/"
    print(f"  Fetching {url}")

    try:
        soup = _fetch(url)
    except Exception as e:
        print(f"  ERROR fetching {team_name}: {e}")
        return []

    # The pitching table has id="grid2"
    table = soup.find("table", id="grid2")
    if table is None:
        for t in soup.find_all("table"):
            hdr = " ".join(
                th.get_text(strip=True).lower() for th in t.find_all("th")
            )
            if "era" in hdr and "ip" in hdr:
                table = t
                break

    if table is None:
        print(f"  WARNING: No pitching table found for {team_name}")
        return []

    rows = table.find_all("tr")
    if len(rows) < 2:
        return []

    # Parse headers
    headers = [
        th.get_text(strip=True).lower()
        for th in rows[0].find_all(["th", "td"])
    ]
    col = {name: i for i, name in enumerate(headers)}

    pitchers = []
    for tr in rows[1:]:
        cells = tr.find_all(["td", "th"])
        vals = [c.get_text(strip=True) for c in cells]
        if len(vals) < len(headers):
            continue

        def _g(key, *alts):
            for k in (key, *alts):
                if k in col and col[k] < len(vals):
                    return vals[col[k]]
            return ""

        name = _g("player", "name")
        normalised = " ".join(name.strip().lower().split())
        if not name or normalised in ("totals", "total", "team", "") \
                or normalised.startswith("totals"):
            continue

        ip = _safe_float(_g("ip"))
        k = _safe_int(_g("so", "k"))
        bb = _safe_int(_g("bb"))
        h = _safe_int(_g("h"))

        ip_for_rate = ip if ip > 0 else 1
        k_per_9 = round(k / ip_for_rate * 9, 1) if ip > 0 else 0.0
        bb_per_9 = round(bb / ip_for_rate * 9, 1) if ip > 0 else 0.0

        hand_raw = _g("th", "throws", "t")
        hand = None
        if hand_raw:
            if hand_raw.upper().startswith("L"):
                hand = "LHP"
            elif hand_raw.upper().startswith("R"):
                hand = "RHP"

        pitchers.append({
            "name": name,
            "hand": hand,
            "g": _safe_int(_g("g", "gp")),
            "gs": _safe_int(_g("gs")),
            "ip": ip,
            "era": _safe_float(_g("era")),
            "k": k,
            "bb": bb,
            "h": h,
            "hr": _safe_int(_g("hr")),
            "k_per_9": k_per_9,
            "bb_per_9": bb_per_9,
        })

    return pitchers


# ---------------------------------------------------------------------------
# Derived metrics (same formulas used in the CrewAI pipeline)
# ---------------------------------------------------------------------------

D1_AVG_ERA = 4.60  # NCAA D1 average ERA baseline


def compute_metrics(raw: dict) -> dict:
    """Derive advanced metrics from raw pitcher stats."""
    ip = raw["ip"]
    k = raw["k"]
    bb = raw["bb"]
    h = raw["h"]
    era = raw["era"]

    # Estimate batters faced: BF ~ 3*IP + H + BB (no HBP data)
    bf = 3 * ip + h + bb if ip > 0 else 1
    bf = max(bf, 1)

    kPct = round(k / bf * 100, 1)
    bbPct = round(bb / bf * 100, 1)

    # Estimated whiff rate (correlated with K rate)
    whiffPct = round(kPct * 1.25, 1)

    # Called-strike + whiff % (college baseline ~27%)
    cswPct = round(0.6 * whiffPct + 12, 1)

    # Stuff+ (100 baseline, adjusted by K-BB differential and ERA vs D1 avg)
    stuffPlus = 100 + (kPct - bbPct - 10) * 3 + (D1_AVG_ERA - era) * 5
    stuffPlus = round(max(70, min(160, stuffPlus)))

    # stuffPlusRaw: Stuff+ without ERA adjustment
    stuffPlusRaw = round(100 + (kPct - bbPct - 10) * 3)
    stuffPlusRaw = max(30, min(150, stuffPlusRaw))

    # Overall composite score
    hardHitPct = 33.0  # default league average
    score = round(
        stuffPlus * 0.35
        + (100 - bbPct * 5) * 0.25
        + kPct * 0.25
        + (100 - hardHitPct * 2) * 0.15
    )

    return {
        "stuffPlus": stuffPlus,
        "cswPct": cswPct,
        "whiffPct": whiffPct,
        "kPct": kPct,
        "bbPct": bbPct,
        "hardHitPct": hardHitPct,
        "tunnelingScore": 50,
        "seqScore": 50,
        "avgVelo": 0,
        "avgSpin": 0,
        "ivb": 0,
        "hBreak": 0,
        "chasePct": 20,
        "zonePct": 50,
        "score": score,
        "stuffPlusRaw": stuffPlusRaw,
    }


# ---------------------------------------------------------------------------
# Build session-compatible JSON entries
# ---------------------------------------------------------------------------


def build_pitcher_entry(idx: int, raw: dict) -> dict:
    """Build a PitcherSessionEntry-compatible dict from raw stats."""
    metrics = compute_metrics(raw)
    score = metrics["score"]

    # Determine role from GS/G ratio
    gs = raw.get("gs", 0)
    g = max(raw.get("g", 1), 1)
    role = "SP" if gs > 0 and gs / g > 0.4 else "RP"

    # Format name as "Last, F."
    name_parts = raw["name"].split(",")
    if len(name_parts) >= 2:
        last = name_parts[0].strip()
        first = name_parts[1].strip()
        display_name = f"{last}, {first[0]}." if first else last
    else:
        parts = raw["name"].strip().split()
        if len(parts) >= 2:
            display_name = f"{parts[-1]}, {parts[0][0]}."
        else:
            display_name = raw["name"]

    hand = raw.get("hand") or "RHP"

    # History (last 10 scores) - stable at current score
    history = [score] * 10

    # Predictions (next 3) - slight upward trend
    predictions = [score, score + 1, score + 1]

    # Prediction bands
    spread = max(4, round(score * 0.07))
    predBands = [
        {"value": score,     "lower": score - spread, "upper": score + spread},
        {"value": score + 1, "lower": score - spread + 1, "upper": score + spread + 1},
        {"value": score + 1, "lower": score - spread + 1, "upper": score + spread + 1},
    ]

    return {
        "id": idx,
        "name": display_name,
        "hand": hand,
        "role": role,
        "metrics": metrics,
        "pitchData": {"pitches": [], "types": []},
        "history": history,
        "predictions": predictions,
        "rank": 0,
        "predBands": predBands,
        "formStatus": "neutral",
        "formDelta": 0,
        "trendStrength": 0.1,
        "riskFlag": None,
        "ceiling": score + spread,
        "floor": max(0, score - spread),
        "game_by_game": [],
        "season_summary": {
            "projected_era": raw["era"],
            "projected_k9": raw["k_per_9"],
            "projected_bb9": raw["bb_per_9"],
            "win_probability_avg": 0.5,
            "stuff_plus_projection": metrics["stuffPlus"],
        },
    }


# ---------------------------------------------------------------------------
# Team processing
# ---------------------------------------------------------------------------


def process_team(team: dict) -> bool:
    """Scrape, compute metrics, and write JSON for one team."""
    print(f"\n{'='*60}")
    print(f"Processing: {team['name']} ({team['conf']})")
    print(f"{'='*60}")

    raw_pitchers = scrape_pitchers(team["ncaa_id"], team["name"])
    if not raw_pitchers:
        print(f"  No pitchers found for {team['name']}")
        return False

    # Filter to pitchers with at least some innings
    pitchers_with_ip = [p for p in raw_pitchers if p["ip"] > 0]
    print(f"  Found {len(raw_pitchers)} total, {len(pitchers_with_ip)} with IP > 0")

    if not pitchers_with_ip:
        print(f"  No pitchers with IP > 0 for {team['name']}")
        return False

    # Build entries
    entries = []
    for idx, raw in enumerate(pitchers_with_ip):
        entry = build_pitcher_entry(idx, raw)
        entries.append(entry)

    # Sort by score descending, assign ranks
    entries.sort(key=lambda e: e["metrics"]["score"], reverse=True)
    for rank, entry in enumerate(entries, 1):
        entry["rank"] = rank
        entry["id"] = rank - 1

    # Write team JSON
    TEAMS_DIR.mkdir(parents=True, exist_ok=True)
    outfile = TEAMS_DIR / f"{team['slug']}.json"
    with open(outfile, "w") as f:
        json.dump(entries, f, indent=2)

    print(f"  Wrote {len(entries)} pitchers to {outfile.name}")
    return True


def update_manifest(successful_teams: list[dict]):
    """Update manifest.json with all team entries."""
    manifest_file = TEAMS_DIR / "manifest.json"
    if manifest_file.exists():
        with open(manifest_file) as f:
            manifest = json.load(f)
    else:
        manifest = []

    existing_slugs = {t["slug"] for t in manifest}

    for team in successful_teams:
        if team["slug"] not in existing_slugs:
            manifest.append({"name": team["name"], "slug": team["slug"]})

    manifest.sort(key=lambda t: t["name"])

    with open(manifest_file, "w") as f:
        json.dump(manifest, f, indent=2)

    print(f"\nManifest updated: {len(manifest)} teams total")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    print("=" * 60)
    print("Pitching Hub - Batch Team Scraper")
    print(f"Season: {SEASON_YEAR}")
    print(f"Teams to process: {len(TEAMS)}")
    print("=" * 60)

    successful = []
    for team in TEAMS:
        if process_team(team):
            successful.append(team)

    if successful:
        update_manifest(successful)

    print(f"\n{'='*60}")
    print(f"Done! Successfully processed {len(successful)}/{len(TEAMS)} teams")
    if len(successful) < len(TEAMS):
        failed = [t["name"] for t in TEAMS if t not in successful]
        print(f"Failed: {', '.join(failed)}")
    print("=" * 60)


if __name__ == "__main__":
    main()
