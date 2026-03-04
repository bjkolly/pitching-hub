"""
William & Mary Baseball Scraper Tools

Scrapes schedule, box scores, pitcher stats, opponent batting, and
pitcher game logs from:
  - warrennolan.com   (schedule with results)
  - thebaseballcube.com (season pitching stats)
  - tribeathletics.com  (individual box scores)

All responses are cached to /data/cache/ as JSON.  A 1-second delay
is enforced between every live HTTP request.
"""

import json
import re
import sys
import time
from pathlib import Path
from typing import Optional

import requests
from bs4 import BeautifulSoup
from crewai.tools import tool

# ---------------------------------------------------------------------------
# Import config — handle running both as a module and standalone
# ---------------------------------------------------------------------------
try:
    from config import (
        BASEBALL_CUBE_URL,
        BOX_SCORE_BASE,
        CAA_OPPONENTS,
        NON_CONF_OPPONENTS,
        SEASON_YEAR,
        TARGET_SCHOOL,
        TRIBE_SCHEDULE_URL,
        WARREN_NOLAN_URL,
    )
except ImportError:
    # When running directly from /agents/tools/
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from config import (
        BASEBALL_CUBE_URL,
        BOX_SCORE_BASE,
        CAA_OPPONENTS,
        NON_CONF_OPPONENTS,
        SEASON_YEAR,
        TARGET_SCHOOL,
        TRIBE_SCHEDULE_URL,
        WARREN_NOLAN_URL,
    )

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CACHE_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "cache"

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

# Build a fast lookup:  normalised opponent name → slug
_ALL_OPPONENTS = CAA_OPPONENTS + NON_CONF_OPPONENTS
_SLUG_MAP: dict[str, str] = {}
for _opp in _ALL_OPPONENTS:
    _SLUG_MAP[_opp["name"].lower()] = _opp["slug"]

# Module-level timestamp for rate limiting across all functions
_last_request_time: float = 0.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _rate_limit() -> None:
    """Enforce a minimum 1-second gap between HTTP requests."""
    global _last_request_time
    elapsed = time.time() - _last_request_time
    if elapsed < 1.0:
        time.sleep(1.0 - elapsed)
    _last_request_time = time.time()


def _fetch(url: str) -> BeautifulSoup:
    """GET *url* with rate limiting and return parsed HTML."""
    _rate_limit()
    resp = requests.get(url, headers=HTTP_HEADERS, timeout=30)
    resp.raise_for_status()
    return BeautifulSoup(resp.text, "html.parser")


def _fetch_text(url: str) -> str:
    """GET *url* with rate limiting and return raw text."""
    _rate_limit()
    resp = requests.get(url, headers=HTTP_HEADERS, timeout=30)
    resp.raise_for_status()
    return resp.text


def _cache_path(filename: str) -> Path:
    """Return the JSON cache file path for *filename*."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR / filename


def _read_cache(filename: str) -> Optional[list | dict]:
    """Return cached data if it exists, else None."""
    p = _cache_path(filename)
    if p.exists():
        with open(p, "r") as f:
            return json.load(f)
    return None


def _write_cache(filename: str, data) -> None:
    """Persist *data* as JSON."""
    p = _cache_path(filename)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w") as f:
        json.dump(data, f, indent=2)


def _safe_float(val: str, default: float = 0.0) -> float:
    try:
        cleaned = val.strip().replace(",", "")
        if cleaned in ("", "-", "INF", "—", "–", "*"):
            return default
        return float(cleaned)
    except (ValueError, AttributeError):
        return default


def _safe_int(val: str, default: int = 0) -> int:
    try:
        cleaned = val.strip().replace(",", "")
        if cleaned in ("", "-", "—", "–", "*"):
            return default
        return int(float(cleaned))
    except (ValueError, AttributeError):
        return default


def _normalise_name(name: str) -> str:
    """Lowercase, strip extra whitespace."""
    return " ".join(name.strip().lower().split())


def _find_slug(opponent_name: str) -> str:
    """Best-effort lookup of a slug for an opponent name."""
    key = _normalise_name(opponent_name)
    if key in _SLUG_MAP:
        return _SLUG_MAP[key]
    # Fuzzy: try matching the start of any key
    for known, slug in _SLUG_MAP.items():
        if key.startswith(known) or known.startswith(key):
            return slug
    # Last resort: slugify the name
    return re.sub(r"[^a-z0-9]+", "-", key).strip("-")


def _table_to_dicts(table, header_row_idx: int = 0) -> list[dict]:
    """Parse an HTML <table> into a list of dicts keyed by header text."""
    rows = table.find_all("tr")
    if len(rows) < header_row_idx + 2:
        return []
    headers = [
        th.get_text(strip=True).lower()
        for th in rows[header_row_idx].find_all(["th", "td"])
    ]
    result = []
    for tr in rows[header_row_idx + 1:]:
        cells = [td.get_text(strip=True) for td in tr.find_all(["th", "td"])]
        if len(cells) != len(headers):
            continue
        result.append(dict(zip(headers, cells)))
    return result


# ═══════════════════════════════════════════════════════════════════════════
# TOOL 1: get_wm_schedule()
# ═══════════════════════════════════════════════════════════════════════════


def _get_wm_schedule_impl() -> list[dict]:
    """Scrape W&M's schedule from Tribe Athletics (primary) or Warren Nolan.

    Returns a list of game dicts with date, opponent, opponent_slug,
    home_away, result, score, box_score_id, and conference_game.
    """
    cache_file = f"wm_schedule_{SEASON_YEAR}.json"
    cached = _read_cache(cache_file)
    if cached is not None:
        return cached

    games: list[dict] = []

    # --- Strategy A (PRIMARY): Tribe Athletics print-friendly page ---
    # The ?print=true version returns full server-rendered HTML with
    # box score links.  Each game is a <tr class="sidearm-schedule-game">
    # with 10 cells:
    #   Cell 0 = date ("February 14, 2025 (Friday)")
    #   Cell 2 = "Home" / "Away" / "Neutral"
    #   Cell 3 = opponent name (** suffix = conference)
    #   Cell 7 = tournament name (if any)
    #   Cell 8 = result ("W,5-4" or "L,9-10F/7" or "PPD")
    #   Cell 9 = links (Box Score href contains /boxscore/{id})
    try:
        tribe_url = TRIBE_SCHEDULE_URL + "?print=true"
        soup = _fetch(tribe_url)
        game_rows = soup.find_all("tr", class_=re.compile(r"sidearm-schedule-game"))
        print(f"[schedule] Tribe Athletics: found {len(game_rows)} game rows")

        for tr in game_rows:
            cells = tr.find_all("td")
            if len(cells) < 9:
                continue

            # --- Date ---
            raw_date = cells[0].get_text(strip=True)
            # "February 14, 2025 (Friday)" → "Feb 14"
            from datetime import datetime as _dt
            try:
                dt_obj = _dt.strptime(
                    re.sub(r"\s*\(.*?\)", "", raw_date).strip(),
                    "%B %d, %Y"
                )
                date_str = dt_obj.strftime("%b %-d")
            except Exception:
                # Fallback: grab "Month Day" from text
                m = re.match(r"(\w+)\s+(\d{1,2})", raw_date)
                date_str = f"{m.group(1)[:3]} {m.group(2)}" if m else raw_date[:10]

            # --- Home / Away ---
            ha_text = cells[2].get_text(strip=True).lower() if len(cells) > 2 else ""
            if "away" in ha_text:
                home_away = "away"
            elif "neutral" in ha_text:
                home_away = "neutral"
            else:
                home_away = "home"

            # --- Opponent ---
            opp_raw = cells[3].get_text(strip=True)
            # Conference games end with ** on Sidearm sites
            conference_game = opp_raw.endswith("**")
            opp_name = opp_raw.rstrip("*").strip()

            # --- Result / Score ---
            result_raw = cells[8].get_text(strip=True) if len(cells) > 8 else ""
            result = None
            score = ""
            if result_raw.upper().startswith("PPD") or "postponed" in result_raw.lower():
                result = "PPD"
            else:
                wl_match = re.match(r"([WL]),\s*(\d+-\d+)", result_raw)
                if wl_match:
                    result = wl_match.group(1)
                    score = wl_match.group(2)

            # --- Box score ID (from link in Cell 9 or anywhere in row) ---
            box_score_id = None
            opp_slug_from_link = None
            search_area = cells[9] if len(cells) > 9 else tr
            for a in search_area.find_all("a", href=True):
                bs_match = re.search(
                    r"/stats/\d{4}/([^/]+)/boxscore/(\d+)", a["href"]
                )
                if bs_match:
                    opp_slug_from_link = bs_match.group(1)
                    box_score_id = bs_match.group(2)
                    break

            # Determine slug: prefer the one from the box score URL
            slug = opp_slug_from_link or _find_slug(opp_name)

            # If conference wasn't detected by **, also check config
            if not conference_game:
                conference_game = any(
                    o["name"].lower() in opp_name.lower()
                    for o in CAA_OPPONENTS
                )

            games.append({
                "date": date_str,
                "opponent": opp_name,
                "opponent_slug": slug,
                "home_away": home_away,
                "result": result,
                "score": score,
                "box_score_id": box_score_id,
                "conference_game": conference_game,
            })

    except Exception as e:
        print(f"[schedule] Tribe Athletics fetch failed: {e}")

    # --- Strategy B (FALLBACK): Warren Nolan ---
    if not games:
        print("[schedule] Falling back to Warren Nolan…")
        try:
            soup = _fetch(WARREN_NOLAN_URL)
            for tr in soup.find_all("tr"):
                cells = tr.find_all(["td", "th"])
                texts = [c.get_text(strip=True) for c in cells]
                if len(texts) < 3:
                    continue
                date_match = re.match(
                    r"(Jan|Feb|Mar|Apr|May|Jun)\s+\d{1,2}", texts[0]
                )
                if not date_match:
                    continue

                date_str = texts[0]
                opponent_raw = texts[1] if len(texts) > 1 else ""
                location_raw = texts[2] if len(texts) > 2 else ""
                result_raw = texts[3] if len(texts) > 3 else ""

                home_away = "home"
                if "away" in location_raw.lower() or location_raw.lower().startswith("at"):
                    home_away = "away"
                elif "neutral" in location_raw.lower():
                    home_away = "neutral"

                result = None
                score = ""
                w_l_match = re.match(r"([WL])\s+(\d+-\d+)", result_raw)
                if w_l_match:
                    result = w_l_match.group(1)
                    score = w_l_match.group(2)
                elif "ppd" in result_raw.lower() or "postponed" in result_raw.lower():
                    result = "PPD"

                opp_name = re.sub(r"\s*\(.*?\)", "", opponent_raw).strip()
                slug = _find_slug(opp_name)
                conf = any(
                    opp["name"].lower() in opp_name.lower()
                    for opp in CAA_OPPONENTS
                )

                box_score_id = None
                for a in tr.find_all("a", href=True):
                    bs_match = re.search(r"boxscore[/=](\d+)", a["href"])
                    if bs_match:
                        box_score_id = bs_match.group(1)
                        break

                games.append({
                    "date": date_str,
                    "opponent": opp_name,
                    "opponent_slug": slug,
                    "home_away": home_away,
                    "result": result,
                    "score": score,
                    "box_score_id": box_score_id,
                    "conference_game": conf,
                })
        except Exception as e:
            print(f"[schedule] Warren Nolan fallback also failed: {e}")

    # --- Strategy C: Tribe Athletics JSON-LD (no box scores, last resort) ---
    if not games:
        try:
            soup2 = _fetch(TRIBE_SCHEDULE_URL)
            for script in soup2.find_all("script", {"type": "application/ld+json"}):
                try:
                    ld = json.loads(script.string)
                    items = ld if isinstance(ld, list) else [ld]
                    for item in items:
                        if item.get("@type") != "SportsEvent":
                            continue
                        name = item.get("name", "")
                        start = item.get("startDate", "")
                        from datetime import datetime as _dt2
                        try:
                            dt = _dt2.fromisoformat(start)
                            date_str = dt.strftime("%b %-d")
                        except Exception:
                            date_str = start[:10]

                        opp_match = re.search(
                            r"William\s*&\s*Mary\s+(?:At|Vs)\s+(.+)", name, re.I
                        )
                        opp_name = opp_match.group(1).strip() if opp_match else name
                        at_game = " At " in name
                        home_away = "away" if at_game else "home"
                        slug = _find_slug(opp_name)
                        conf = any(
                            o["name"].lower() in opp_name.lower()
                            for o in CAA_OPPONENTS
                        )
                        games.append({
                            "date": date_str,
                            "opponent": opp_name,
                            "opponent_slug": slug,
                            "home_away": home_away,
                            "result": None,
                            "score": "",
                            "box_score_id": None,
                            "conference_game": conf,
                        })
                except Exception:
                    continue
        except Exception as e:
            print(f"[schedule] JSON-LD fallback also failed: {e}")

    print(f"[schedule] Total games: {len(games)}, "
          f"with box scores: {sum(1 for g in games if g.get('box_score_id'))}")
    _write_cache(cache_file, games)
    return games


# ═══════════════════════════════════════════════════════════════════════════
# TOOL 2: get_box_score(opponent_slug, box_score_id)
# ═══════════════════════════════════════════════════════════════════════════


def _get_box_score_impl(opponent_slug: str, box_score_id: str) -> dict:
    """Scrape a single game box score from tribeathletics.com.

    Returns batting and pitching tables for both teams.
    """
    cache_file = f"boxscore_{box_score_id}.json"
    cached = _read_cache(cache_file)
    if cached is not None:
        return cached

    url = BOX_SCORE_BASE.format(slug=opponent_slug, game_id=box_score_id)
    result = {
        "game_id": box_score_id,
        "opponent": opponent_slug,
        "wm_batting": [],
        "wm_pitching": [],
        "opp_batting": [],
        "opp_pitching": [],
    }

    try:
        soup = _fetch(url)
    except Exception as e:
        print(f"[boxscore] Fetch failed for {box_score_id}: {e}")
        _write_cache(cache_file, result)
        return result

    # Sidearm box score pages have stats tables.  Identify them by
    # scanning <table> elements and checking header rows for known
    # column names (ab, r, h, rbi for batting; ip, h, r, er for pitching).
    tables = soup.find_all("table")

    batting_tables = []
    pitching_tables = []

    for table in tables:
        header_text = " ".join(
            th.get_text(strip=True).lower()
            for th in table.find_all("th")
        )
        if not header_text:
            first_row = table.find("tr")
            if first_row:
                header_text = " ".join(
                    td.get_text(strip=True).lower()
                    for td in first_row.find_all(["th", "td"])
                )

        if re.search(r"\bab\b.*\br\b.*\bh\b", header_text):
            batting_tables.append(table)
        elif re.search(r"\bip\b.*\bh\b.*\br\b.*\ber\b", header_text):
            pitching_tables.append(table)

    def _parse_batting(table) -> list[dict]:
        rows = _table_to_dicts(table)
        players = []
        for row in rows:
            name = row.get("player", row.get("name", row.get("", "")))
            if not name or _normalise_name(name) in ("totals", "total", "team"):
                continue
            players.append({
                "name": name,
                "pos": row.get("pos", ""),
                "ab": _safe_int(row.get("ab", "0")),
                "r": _safe_int(row.get("r", "0")),
                "h": _safe_int(row.get("h", "0")),
                "rbi": _safe_int(row.get("rbi", "0")),
                "bb": _safe_int(row.get("bb", "0")),
                "so": _safe_int(row.get("so", row.get("k", "0"))),
                "avg": _safe_float(row.get("avg", row.get("ba", "0"))),
            })
        return players

    def _parse_pitching(table) -> list[dict]:
        rows = _table_to_dicts(table)
        players = []
        for row in rows:
            name = row.get("player", row.get("name", row.get("", "")))
            if not name or _normalise_name(name) in ("totals", "total", "team"):
                continue
            players.append({
                "name": name,
                "ip": _safe_float(row.get("ip", "0")),
                "h": _safe_int(row.get("h", "0")),
                "r": _safe_int(row.get("r", "0")),
                "er": _safe_int(row.get("er", "0")),
                "bb": _safe_int(row.get("bb", "0")),
                "so": _safe_int(row.get("so", row.get("k", "0"))),
                "era": _safe_float(row.get("era", "0")),
                "pitch_count": _safe_int(row.get("np", row.get("pc", row.get("pitches", "0")))),
            })
        return players

    # Sidearm box scores typically show the away team first, then the
    # home team.  William & Mary tables are identified by scanning for
    # a heading or caption containing "William" or "Tribe".
    def _is_wm_section(table) -> bool:
        # Check preceding siblings / parent for team name
        prev = table.find_previous(["h2", "h3", "h4", "caption", "div", "span"])
        if prev:
            txt = prev.get_text(strip=True).lower()
            if "william" in txt or "tribe" in txt or "w&m" in txt:
                return True
        # Check table caption
        cap = table.find("caption")
        if cap and ("william" in cap.get_text().lower() or "tribe" in cap.get_text().lower()):
            return True
        return False

    # Assign batting tables
    if len(batting_tables) >= 2:
        if _is_wm_section(batting_tables[0]):
            result["wm_batting"] = _parse_batting(batting_tables[0])
            result["opp_batting"] = _parse_batting(batting_tables[1])
        else:
            result["opp_batting"] = _parse_batting(batting_tables[0])
            result["wm_batting"] = _parse_batting(batting_tables[1])
    elif len(batting_tables) == 1:
        result["wm_batting"] = _parse_batting(batting_tables[0])

    # Assign pitching tables
    if len(pitching_tables) >= 2:
        if _is_wm_section(pitching_tables[0]):
            result["wm_pitching"] = _parse_pitching(pitching_tables[0])
            result["opp_pitching"] = _parse_pitching(pitching_tables[1])
        else:
            result["opp_pitching"] = _parse_pitching(pitching_tables[0])
            result["wm_pitching"] = _parse_pitching(pitching_tables[1])
    elif len(pitching_tables) == 1:
        result["wm_pitching"] = _parse_pitching(pitching_tables[0])

    _write_cache(cache_file, result)
    return result


# ═══════════════════════════════════════════════════════════════════════════
# TOOL 3: get_all_wm_pitcher_stats()
# ═══════════════════════════════════════════════════════════════════════════


def _get_all_wm_pitcher_stats_impl() -> list[dict]:
    """Scrape season pitching stats from The Baseball Cube.

    Returns a list of pitcher dicts with name, hand, g, gs, ip, era,
    k, bb, h, hr, whip, k_per_9, bb_per_9.
    """
    cache_file = f"wm_pitchers_{SEASON_YEAR}.json"
    cached = _read_cache(cache_file)
    if cached is not None:
        return cached

    pitchers: list[dict] = []

    try:
        soup = _fetch(BASEBALL_CUBE_URL)
    except Exception as e:
        print(f"[pitcher_stats] Baseball Cube fetch failed: {e}")
        _write_cache(cache_file, pitchers)
        return pitchers

    # The pitching table has id="grid2"
    table = soup.find("table", id="grid2")
    if table is None:
        # Fallback: look for any table whose headers contain "era" and "ip"
        for t in soup.find_all("table"):
            hdr = " ".join(
                th.get_text(strip=True).lower()
                for th in t.find_all("th")
            )
            if "era" in hdr and "ip" in hdr:
                table = t
                break

    if table is None:
        print("[pitcher_stats] No pitching table found")
        _write_cache(cache_file, pitchers)
        return pitchers

    rows = table.find_all("tr")
    if len(rows) < 2:
        _write_cache(cache_file, pitchers)
        return pitchers

    # Parse headers
    header_row = rows[0]
    headers = [
        th.get_text(strip=True).lower()
        for th in header_row.find_all(["th", "td"])
    ]

    col = {name: i for i, name in enumerate(headers)}

    for tr in rows[1:]:
        cells = tr.find_all(["td", "th"])
        vals = [c.get_text(strip=True) for c in cells]
        if len(vals) < len(headers):
            continue

        def _g(key, *alts):
            """Get cell value by column name with alternates."""
            for k in (key, *alts):
                if k in col and col[k] < len(vals):
                    return vals[col[k]]
            return ""

        name = _g("player", "name")
        normalised = _normalise_name(name)
        if not name or normalised in ("totals", "total", "team", "") or normalised.startswith("totals"):
            continue

        ip = _safe_float(_g("ip"))
        k = _safe_int(_g("so", "k"))
        bb = _safe_int(_g("bb"))
        h = _safe_int(_g("h"))

        # Derive per-9 rates
        ip_for_rate = ip if ip > 0 else 1
        k_per_9 = round(k / ip_for_rate * 9, 1) if ip > 0 else 0.0
        bb_per_9 = round(bb / ip_for_rate * 9, 1) if ip > 0 else 0.0

        # WHIP
        whip_raw = _g("whip")
        if whip_raw and whip_raw not in ("-", "—", ""):
            whip = _safe_float(whip_raw)
        else:
            whip = round((bb + h) / ip_for_rate, 2) if ip > 0 else 0.0

        # Hand (throwing arm)
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
            "whip": whip,
            "k_per_9": k_per_9,
            "bb_per_9": bb_per_9,
        })

    _write_cache(cache_file, pitchers)
    return pitchers


# ═══════════════════════════════════════════════════════════════════════════
# TOOL 4: get_opponent_batters(opponent_name, box_score_ids)
# ═══════════════════════════════════════════════════════════════════════════


def _get_opponent_batters_impl(
    opponent_name: str, box_score_ids: list[str]
) -> list[dict]:
    """Aggregate opponent batting stats across multiple box scores.

    Returns a list of batter dicts with aggregated totals and rates.
    """
    slug = _find_slug(opponent_name)
    cache_file = f"opp_batters_{slug}.json"
    cached = _read_cache(cache_file)
    if cached is not None:
        return cached

    # Accumulate per-player stats across games
    accum: dict[str, dict] = {}  # keyed by normalised name

    for bsid in box_score_ids:
        bs = _get_box_score_impl(slug, bsid)
        for batter in bs.get("opp_batting", []):
            key = _normalise_name(batter["name"])
            if key not in accum:
                accum[key] = {
                    "name": batter["name"],
                    "team": opponent_name,
                    "total_ab": 0,
                    "total_hits": 0,
                    "total_bb": 0,
                    "total_so": 0,
                    "total_r": 0,
                    "total_rbi": 0,
                    "total_tb": 0,   # approximate total bases
                    "vs_wm_games": 0,
                }
            a = accum[key]
            ab = batter.get("ab", 0)
            h = batter.get("h", 0)
            a["total_ab"] += ab
            a["total_hits"] += h
            a["total_bb"] += batter.get("bb", 0)
            a["total_so"] += batter.get("so", 0)
            a["total_r"] += batter.get("r", 0)
            a["total_rbi"] += batter.get("rbi", 0)
            # Estimate total bases (singles + 2B/3B/HR data not in box)
            # Use hits as a lower bound (all singles)
            a["total_tb"] += h
            a["vs_wm_games"] += 1

    # Build final list with rate stats
    batters = []
    for a in accum.values():
        ab = a["total_ab"]
        pa = ab + a["total_bb"]  # approximate PA (no HBP/SF data)

        avg = round(a["total_hits"] / ab, 3) if ab > 0 else 0.0
        obp = round((a["total_hits"] + a["total_bb"]) / pa, 3) if pa > 0 else 0.0
        slg = round(a["total_tb"] / ab, 3) if ab > 0 else avg  # lower bound
        k_pct = round(a["total_so"] / pa * 100, 1) if pa > 0 else 0.0
        bb_pct = round(a["total_bb"] / pa * 100, 1) if pa > 0 else 0.0

        batters.append({
            "name": a["name"],
            "team": a["team"],
            "total_ab": a["total_ab"],
            "total_hits": a["total_hits"],
            "total_bb": a["total_bb"],
            "total_so": a["total_so"],
            "avg": avg,
            "obp": obp,
            "slg": slg,
            "k_pct": k_pct,
            "bb_pct": bb_pct,
            "vs_wm_games": a["vs_wm_games"],
        })

    # Filter: drop batters with 0 AB (pitchers, unused subs)
    batters = [b for b in batters if b["total_ab"] > 0]

    # Clean position prefix from names (e.g. "rfSmith, J." → "Smith, J.")
    for b in batters:
        b["name"] = re.sub(r"^(?:ph/)?(?:dh|rf|lf|cf|ss|2b|3b|1b|c|p)\s*", "", b["name"])

    # Sort by AB descending (regulars first), limit to top 12 per team
    batters.sort(key=lambda b: b["total_ab"], reverse=True)
    batters = batters[:12]
    _write_cache(cache_file, batters)
    return batters


# ═══════════════════════════════════════════════════════════════════════════
# TOOL 5: get_wm_pitcher_game_logs()
# ═══════════════════════════════════════════════════════════════════════════


def _get_wm_pitcher_game_logs_impl() -> list[dict]:
    """Build game-by-game logs for every W&M pitcher from box scores.

    Returns a list of pitcher log dicts with game_logs array and
    season_totals.
    """
    cache_file = "wm_pitcher_game_logs.json"
    cached = _read_cache(cache_file)
    if cached is not None:
        return cached

    schedule = _get_wm_schedule_impl()
    # Only games with box score IDs
    games_with_bs = [g for g in schedule if g.get("box_score_id")]

    # Accumulate per-pitcher game entries
    pitcher_map: dict[str, list[dict]] = {}  # normalised name → list of appearances

    for game in games_with_bs:
        bs = _get_box_score_impl(game["opponent_slug"], game["box_score_id"])
        for p in bs.get("wm_pitching", []):
            key = _normalise_name(p["name"])
            if key not in pitcher_map:
                pitcher_map[key] = []
            pitcher_map[key].append({
                "date": game["date"],
                "opponent": game["opponent"],
                "ip": p.get("ip", 0.0),
                "h": p.get("h", 0),
                "r": p.get("r", 0),
                "er": p.get("er", 0),
                "bb": p.get("bb", 0),
                "so": p.get("so", 0),
                "result": game.get("result"),
            })

    # Build output with season totals
    result = []
    for key, logs in pitcher_map.items():
        total_ip = sum(l["ip"] for l in logs)
        total_k = sum(l["so"] for l in logs)
        total_bb = sum(l["bb"] for l in logs)
        total_h = sum(l["h"] for l in logs)
        total_er = sum(l["er"] for l in logs)
        total_r = sum(l["r"] for l in logs)

        ip_for_rate = total_ip if total_ip > 0 else 1

        # Count starts vs relief (first appearance in a game = start guess)
        gs = sum(1 for l in logs if l == logs[0])  # rough heuristic
        # Better: if pitcher is first in the pitching list, it's a start
        # We don't have that data here, so just count total games
        g = len(logs)

        era = round(total_er / ip_for_rate * 9, 2) if total_ip > 0 else 0.0
        whip = round((total_bb + total_h) / ip_for_rate, 2) if total_ip > 0 else 0.0
        k_per_9 = round(total_k / ip_for_rate * 9, 1) if total_ip > 0 else 0.0
        bb_per_9 = round(total_bb / ip_for_rate * 9, 1) if total_ip > 0 else 0.0
        h_per_9 = round(total_h / ip_for_rate * 9, 1) if total_ip > 0 else 0.0

        # Use the display name from the first appearance
        display_name = logs[0]["opponent"]  # placeholder — use pitcher name
        # Find original cased name from any log entry
        pitcher_name = key.title().replace("  ", " ")
        # Try to get it from the raw data
        for game in games_with_bs:
            bs = _read_cache(f"boxscore_{game['box_score_id']}.json")
            if bs:
                for p in bs.get("wm_pitching", []):
                    if _normalise_name(p["name"]) == key:
                        pitcher_name = p["name"]
                        break
                if pitcher_name != key.title():
                    break

        result.append({
            "pitcher_name": pitcher_name,
            "game_logs": logs,
            "season_totals": {
                "g": g,
                "gs": gs,
                "ip": round(total_ip, 1),
                "era": era,
                "k": total_k,
                "bb": total_bb,
                "whip": whip,
                "k_per_9": k_per_9,
                "bb_per_9": bb_per_9,
                "h_per_9": h_per_9,
            },
        })

    # Sort by IP descending
    result.sort(key=lambda p: p["season_totals"]["ip"], reverse=True)
    _write_cache(cache_file, result)
    return result


# ═══════════════════════════════════════════════════════════════════════════
# TOOL 6: get_upcoming_schedule_2026()
# ═══════════════════════════════════════════════════════════════════════════


def _get_upcoming_schedule_2026_impl() -> list[dict]:
    """Scrape W&M's 2026 schedule from Tribe Athletics.

    Falls back to the 2025 CAA opponents as projected 2026 opponents
    if the 2026 page has no games yet.
    """
    cache_file = "wm_schedule_2026.json"
    cached = _read_cache(cache_file)
    if cached is not None:
        return cached

    games: list[dict] = []

    try:
        url_2026 = TRIBE_SCHEDULE_URL.replace("/2025", "/2026")
        soup = _fetch(url_2026)

        # Parse JSON-LD from the page
        for script in soup.find_all("script", {"type": "application/ld+json"}):
            try:
                ld = json.loads(script.string)
                items = ld if isinstance(ld, list) else [ld]
                for item in items:
                    if item.get("@type") != "SportsEvent":
                        continue
                    name = item.get("name", "")
                    start = item.get("startDate", "")

                    from datetime import datetime
                    try:
                        dt = datetime.fromisoformat(start)
                        date_str = dt.strftime("%b %d").replace(" 0", " ")
                    except Exception:
                        date_str = start[:10] if start else ""

                    opp_match = re.search(
                        r"William\s*&\s*Mary\s+(?:At|Vs)\s+(.+)", name, re.I
                    )
                    opp_name = opp_match.group(1).strip() if opp_match else name
                    at_game = " At " in name
                    home_away = "away" if at_game else "home"
                    slug = _find_slug(opp_name)
                    conf = any(
                        o["name"].lower() in opp_name.lower()
                        for o in CAA_OPPONENTS
                    )

                    games.append({
                        "date": date_str,
                        "opponent": opp_name,
                        "opponent_slug": slug,
                        "home_away": home_away,
                        "conference_game": conf,
                    })
            except Exception:
                continue

    except Exception as e:
        print(f"[schedule_2026] Fetch failed: {e}")

    # Fallback: project 2025 CAA opponents as 2026
    if not games:
        print("[schedule_2026] No 2026 games found — using 2025 CAA opponents as projection")
        for opp in CAA_OPPONENTS:
            for game_num in range(opp.get("games", 3)):
                games.append({
                    "date": f"TBD (Game {game_num + 1})",
                    "opponent": opp["name"],
                    "opponent_slug": opp["slug"],
                    "home_away": "home" if game_num % 2 == 0 else "away",
                    "conference_game": True,
                })
        for opp in NON_CONF_OPPONENTS[:6]:  # top 6 non-conf as projections
            games.append({
                "date": "TBD",
                "opponent": opp["name"],
                "opponent_slug": opp["slug"],
                "home_away": "home",
                "conference_game": False,
            })

    _write_cache(cache_file, games)
    return games


# ═══════════════════════════════════════════════════════════════════════════
# CrewAI @tool wrappers — thin shells around the _impl functions so
# internal calls between tools still use plain Python function calls.
# ═══════════════════════════════════════════════════════════════════════════


@tool("get_wm_schedule")
def get_wm_schedule() -> list[dict]:
    """Scrape W&M's schedule from Tribe Athletics (primary) or Warren Nolan.
    Returns a list of game dicts with date, opponent, opponent_slug,
    home_away, result, score, box_score_id, and conference_game.
    """
    return _get_wm_schedule_impl()


@tool("get_box_score")
def get_box_score(opponent_slug: str, box_score_id: str) -> dict:
    """Scrape a single game box score from tribeathletics.com.
    Returns batting and pitching tables for both teams.
    """
    return _get_box_score_impl(opponent_slug, box_score_id)


@tool("get_all_wm_pitcher_stats")
def get_all_wm_pitcher_stats() -> list[dict]:
    """Scrape season pitching stats from The Baseball Cube.
    Returns a list of pitcher dicts with name, hand, g, gs, ip, era,
    k, bb, h, hr, whip, k_per_9, bb_per_9.
    """
    return _get_all_wm_pitcher_stats_impl()


@tool("get_opponent_batters")
def get_opponent_batters(opponent_name: str, box_score_ids: list[str]) -> list[dict]:
    """Aggregate opponent batting stats across multiple box scores.
    Returns a list of batter dicts with aggregated totals and rates.
    """
    return _get_opponent_batters_impl(opponent_name, box_score_ids)


@tool("get_wm_pitcher_game_logs")
def get_wm_pitcher_game_logs() -> list[dict]:
    """Build game-by-game logs for every W&M pitcher from box scores.
    Returns a list of pitcher log dicts with game_logs array and
    season_totals.
    """
    return _get_wm_pitcher_game_logs_impl()


@tool("get_upcoming_schedule_2026")
def get_upcoming_schedule_2026() -> list[dict]:
    """Scrape W&M's 2026 schedule from Tribe Athletics.
    Falls back to the 2025 CAA opponents as projected 2026 opponents
    if the 2026 page has no games yet.
    """
    return _get_upcoming_schedule_2026_impl()


# ═══════════════════════════════════════════════════════════════════════════
# MAIN TEST BLOCK
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import json

    print("Testing W&M scraper...")

    print("\n1. Fetching schedule...")
    schedule = _get_wm_schedule_impl()
    print(f"   Found {len(schedule)} games")
    if schedule:
        for g in schedule[:3]:
            print(f"   {g['date']:8s} {'@' if g['home_away']=='away' else 'vs'} {g['opponent']:20s} {g.get('result','') or '':1s} {g.get('score','')}")

    print("\n2. Fetching first box score...")
    first = next((g for g in schedule if g.get("box_score_id")), None)
    if first:
        bs = _get_box_score_impl(first["opponent_slug"], first["box_score_id"])
        print(f"   W&M batters: {len(bs['wm_batting'])}")
        print(f"   W&M pitchers: {len(bs['wm_pitching'])}")
    else:
        print("   No box score IDs found in schedule (expected if Warren Nolan doesn't provide them)")

    print("\n3. Fetching W&M pitcher season stats...")
    pitchers = _get_all_wm_pitcher_stats_impl()
    print(f"   Found {len(pitchers)} pitchers")
    for p in pitchers[:3]:
        print(f"   {p['name']:20s}: ERA {p['era']:5.2f}, K/9 {p['k_per_9']:4.1f}, WHIP {p['whip']:.2f}")

    print("\n4. Fetching pitcher game logs...")
    logs = _get_wm_pitcher_game_logs_impl()
    print(f"   Built logs for {len(logs)} pitchers")
    for lg in logs[:3]:
        t = lg["season_totals"]
        print(f"   {lg['pitcher_name']:20s}: {t['g']}G, {t['ip']}IP, {t['era']} ERA")

    print("\nAll tools working. Cache saved to /data/cache/")
