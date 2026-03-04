"""
Pitching Hub - CrewAI Agent Entry Point

Orchestrates four specialised agents that scout opponent batters,
profile our pitchers, align matchups, and predict season performance.
Final output is written to ../data/crew_session.json in the same
schema the hex UI already consumes.
"""

import json
import sys
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from pydantic import BaseModel, Field

load_dotenv(override=True)

from crewai import Agent, Crew, LLM, Process, Task

from config import (
    TARGET_SCHOOL,
    TARGET_NCAA_ID,
    SEASON_YEAR,
    OUTPUT_PATH,
    CAA_OPPONENTS,
    ROSTER_2026_PITCHERS,
)
from tools.ncaa_scraper import (
    get_wm_schedule,
    get_box_score,
    get_all_wm_pitcher_stats,
    get_opponent_batters,
    get_wm_pitcher_game_logs,
    get_upcoming_schedule_2026,
)

# ---------------------------------------------------------------------------
# Resolve output path relative to *this* file so it always lands in /data/
# ---------------------------------------------------------------------------
OUTPUT_FILE = (Path(__file__).resolve().parent / OUTPUT_PATH).resolve()

# ---------------------------------------------------------------------------
# LLM model — CrewAI supports "anthropic/model-name" natively
# ---------------------------------------------------------------------------
LLM_MODEL = LLM(
    model="anthropic/claude-sonnet-4-20250514",
    max_tokens=16384,
)

# Same model for scouting tasks (data is pre-filtered to reduce token volume)
LLM_SCOUT = LLM_MODEL

# ---------------------------------------------------------------------------
# Pydantic models for structured task output
#
# These mirror the session.json schema the hex dashboard expects:
#   [ { id, name, hand, role, metrics, pitchData, history,
#       predictions, rank, predBands, formStatus, ... }, ... ]
# ---------------------------------------------------------------------------


class PredBand(BaseModel):
    value: float = 0
    lower: float = 0
    upper: float = 0


class PitchType(BaseModel):
    n: str = ""
    c: str = "#4a9eff"


class PitchDataPoint(BaseModel):
    x: float = 0
    y: float = 0
    ti: int = 0
    c: str = "#4a9eff"
    res: str = "b"


class PitchData(BaseModel):
    pitches: list[PitchDataPoint] = Field(default_factory=list)
    types: list[PitchType] = Field(default_factory=list)


class PitcherMetrics(BaseModel):
    stuffPlus: float = 100
    cswPct: float = 0
    whiffPct: float = 0
    kPct: float = 0
    bbPct: float = 0
    hardHitPct: float = 0
    tunnelingScore: float = 50
    seqScore: float = 50
    avgVelo: float = 0
    avgSpin: float = 0
    ivb: float = 0
    hBreak: float = 0
    chasePct: float = 0
    zonePct: float = 0
    score: float = 50
    stuffPlusRaw: float = 50


class GameProjection(BaseModel):
    opponent: str = ""
    date: str = ""
    projected_era: float = 0
    projected_k: float = 0
    projected_bb: float = 0
    confidence: float = 0
    key_matchups: list[str] = Field(default_factory=list)


class SeasonSummary(BaseModel):
    projected_era: float = 0
    projected_k9: float = 0
    projected_bb9: float = 0
    win_probability_avg: float = 0
    stuff_plus_projection: float = 100


class PitcherSessionEntry(BaseModel):
    """One element of the top-level session array the hex UI renders."""
    id: int = 0
    name: str = ""
    hand: str = "RHP"
    role: str = "SP"
    metrics: PitcherMetrics = Field(default_factory=PitcherMetrics)
    pitchData: PitchData = Field(default_factory=PitchData)
    history: list[float] = Field(default_factory=list)
    predictions: list[float] = Field(default_factory=list)
    rank: int = 0
    predBands: list[PredBand] = Field(default_factory=list)
    formStatus: str = "neutral"
    formDelta: float = 0
    trendStrength: float = 0
    riskFlag: Optional[str] = None
    ceiling: float = 0
    floor: float = 0
    # Extra fields from the crew (the UI ignores unknown keys gracefully)
    game_by_game: list[GameProjection] = Field(default_factory=list)
    season_summary: SeasonSummary = Field(default_factory=SeasonSummary)


class CrewSessionOutput(BaseModel):
    """The complete JSON array written to crew_session.json."""
    pitchers: list[PitcherSessionEntry]


# ---------------------------------------------------------------------------
# Agent definitions
# ---------------------------------------------------------------------------


def build_agents() -> dict[str, Agent]:
    """Create and return the four specialised agents."""

    opponent_batter_scout = Agent(
        role="College Baseball Batter Analyst",
        goal=(
            f"Scrape and analyse batting statistics for all batters on "
            f"opponent teams scheduled against {TARGET_SCHOOL} in the "
            f"{SEASON_YEAR} season.  Use get_wm_schedule() to get the "
            f"schedule, identify games with box scores, then call "
            f"get_opponent_batters() for each opponent to aggregate their "
            f"batting profiles with: name, team, AVG, OBP, SLG, "
            f"K%, BB%, and vs-W&M game counts."
        ),
        backstory=(
            "You are an elite college baseball advance-scout who has "
            "spent 15 years breaking down opposing line-ups.  You are "
            "meticulous with data, always double-checking scraped numbers "
            "against source pages.  You understand that box-score data "
            "may be incomplete for some games, and you handle those "
            "cases gracefully by noting missing fields rather than "
            "fabricating values."
        ),
        llm=LLM_SCOUT,
        tools=[get_wm_schedule, get_box_score, get_opponent_batters],
        verbose=True,
        allow_delegation=False,
        max_iter=30,
    )

    target_pitcher_scout = Agent(
        role="College Baseball Pitcher Analyst",
        goal=(
            f"Analyse all pitchers on the {TARGET_SCHOOL} roster using "
            f"{SEASON_YEAR} performance data.  Use "
            f"get_all_wm_pitcher_stats() to get season stats from The "
            f"Baseball Cube, and get_wm_pitcher_game_logs() to build "
            f"game-by-game logs.  Build a pitcher profile for each arm "
            f"that matches the composite scoring schema: stuffPlus, "
            f"cswPct, whiffPct, kPct, bbPct, hardHitPct, "
            f"tunnelingScore, seqScore, avgVelo, avgSpin, ivb, hBreak, "
            f"chasePct, zonePct, and an overall score."
        ),
        backstory=(
            "You are a data-driven pitching analyst who combines "
            "traditional scouting with modern metrics.  You understand "
            "that college stats only provide basic counting stats "
            "(ERA, K, BB, IP, H, HR), so you derive advanced metrics "
            "through well-documented formulas:\n"
            "  - kPct   = K / BF * 100\n"
            "  - bbPct  = BB / BF * 100\n"
            "  - whiffPct is estimated from K-rate and league baselines\n"
            "  - cswPct (called-strike + whiff %) is estimated as "
            "    0.6 * whiffPct + 12  (college baseline ~27%)\n"
            "  - stuffPlus is seeded at 100 and adjusted by K-BB% and "
            "    ERA relative to the D1 average (~4.60 ERA in 2024)\n"
            "  - hardHitPct defaults to league average when unavailable\n"
            "  - tunnelingScore and seqScore default to 50 (neutral)\n"
            "When exact data is unavailable, you clearly mark the value "
            "as an estimate and explain your derivation."
        ),
        llm=LLM_SCOUT,
        tools=[get_all_wm_pitcher_stats, get_wm_pitcher_game_logs],
        verbose=True,
        allow_delegation=False,
        max_iter=20,
    )

    matchup_aligner = Agent(
        role="Baseball Matchup Specialist",
        goal=(
            f"For each {TARGET_SCHOOL} pitcher, identify which opponent "
            f"batters they are likely to face based on the {SEASON_YEAR} "
            f"schedule and probable lineup positions.  Calculate a matchup "
            f"score for each pitcher-vs-batter pairing that reflects the "
            f"pitcher's edge (or disadvantage).  Output a matchup matrix "
            f"with: pitcher_id, batter_id, projected_pa, matchup_score, "
            f"and edge (pitcher / batter / neutral)."
        ),
        backstory=(
            "You are a matchup-obsessed strategist who lives at the "
            "intersection of advance scouting and game planning.  You "
            "score matchups on a 0-100 scale:\n"
            "  - 60+  = pitcher advantage  (edge: pitcher)\n"
            "  - 40-59 = neutral           (edge: neutral)\n"
            "  - <40  = batter advantage   (edge: batter)\n"
            "Scoring factors you weigh:\n"
            "  1. Pitcher K% vs batter K%  (strikeout differential)\n"
            "  2. Pitcher BB% vs batter BB% (walk differential)\n"
            "  3. Batter SLG vs pitcher HR-allowed rate\n"
            "  4. Platoon advantage (L/R splits when available)\n"
            "You project plate-appearance counts by distributing a "
            "starter's ~25 BF and a reliever's ~8 BF across the "
            "opponent lineup proportionally to each batter's GP/GS."
        ),
        llm=LLM_MODEL,
        tools=[],  # works entirely from context
        verbose=True,
        allow_delegation=False,
        max_iter=25,
    )

    season_predictor = Agent(
        role="Predictive Baseball Analytics Engine",
        goal=(
            f"Using all matchup data, predict each {TARGET_SCHOOL} "
            f"pitcher's performance for every scheduled game in the "
            f"{SEASON_YEAR} season.  Output per-game projections "
            f"(projected ERA, K, BB, confidence, key matchups) AND a "
            f"season-long performance forecast (projected ERA, K/9, "
            f"BB/9, win probability average, stuff-plus projection).  "
            f"Format the final output as a JSON array matching the "
            f"pitching hub session schema so the hex dashboard can "
            f"render it directly."
        ),
        backstory=(
            "You are an AI-powered projection engine that synthesises "
            "scouting reports and matchup matrices into actionable "
            "forecasts.  Your methodology:\n\n"
            "GAME-BY-GAME:\n"
            "  - Base projection = pitcher's season-rate stats\n"
            "  - Adjustment = weighted average of matchup scores for "
            "    that game's opponent lineup\n"
            "  - Confidence = f(sample size, data completeness)\n\n"
            "SEASON ROLL-UP:\n"
            "  - projected_era   = IP-weighted mean of game ERAs\n"
            "  - projected_k9    = total K / total IP * 9\n"
            "  - projected_bb9   = total BB / total IP * 9\n"
            "  - win_prob_avg    = average game-level win probability\n"
            "  - stuff_plus_proj = regressed stuffPlus from scout agent\n\n"
            "OUTPUT FORMAT per pitcher (JSON):\n"
            "  { id, name, hand, role, metrics: {...}, pitchData: {...},\n"
            "    history: [last N scores], predictions: [next 3],\n"
            "    rank, predBands: [{value, lower, upper}, ...],\n"
            "    formStatus, formDelta, trendStrength, riskFlag,\n"
            "    ceiling, floor,\n"
            "    game_by_game: [{opponent, date, projected_era, "
            "projected_k, projected_bb, confidence, key_matchups}],\n"
            "    season_summary: {projected_era, projected_k9, "
            "projected_bb9, win_probability_avg, "
            "stuff_plus_projection} }\n"
            "Return a JSON array of these objects — one per pitcher."
        ),
        llm=LLM_MODEL,
        tools=[],  # works entirely from context
        verbose=True,
        allow_delegation=False,
        max_iter=30,
    )

    return {
        "batter_scout": opponent_batter_scout,
        "pitcher_scout": target_pitcher_scout,
        "matchup_aligner": matchup_aligner,
        "season_predictor": season_predictor,
    }


# ---------------------------------------------------------------------------
# Task definitions
# ---------------------------------------------------------------------------


def build_tasks(agents: dict[str, Agent]) -> list[Task]:
    """Create the four sequential tasks wired to the correct agents."""

    # ---- Task 1: Scout opponent batters ----
    task_scout_batters = Task(
        description=(
            f"1. Call get_wm_schedule() to retrieve the full {SEASON_YEAR} "
            f"schedule for {TARGET_SCHOOL}.\n"
            f"2. Group the games by opponent.  For each opponent that has "
            f"at least one box_score_id, collect all their box_score_ids "
            f"into a list.\n"
            f"3. For EACH opponent, call get_opponent_batters(opponent_name, "
            f"box_score_ids) to aggregate their batting across all games "
            f"played against W&M.\n"
            f"4. Compile a consolidated list of batter profiles.  Each "
            f"profile must include:\n"
            f"   - name, team (opponent name)\n"
            f"   - avg, obp, slg\n"
            f"   - k_pct (K%), bb_pct (BB%)\n"
            f"   - total_ab, vs_wm_games\n"
            f"   - pull_pct (default 40.0 if unavailable)\n"
            f"   - hard_hit_pct (default 33.0 if unavailable)\n"
            f"Return the full list as a JSON array."
        ),
        expected_output=(
            "A JSON array of batter profile objects.  Example element:\n"
            '{"name": "Smith, J.", "team": "Elon", '
            '"avg": 0.312, "obp": 0.401, "slg": 0.498, "k_pct": 18.5, '
            '"bb_pct": 12.3, "total_ab": 24, "vs_wm_games": 3, '
            '"pull_pct": 40.0, "hard_hit_pct": 33.0}'
        ),
        agent=agents["batter_scout"],
    )

    # ---- Task 2: Profile our pitchers (filtered to 2026 roster) ----
    roster_names = ", ".join(ROSTER_2026_PITCHERS)
    task_scout_pitchers = Task(
        description=(
            f"1. Call get_all_wm_pitcher_stats() to get season stats for "
            f"every pitcher on the {TARGET_SCHOOL} roster from The "
            f"Baseball Cube.  This returns name, hand, g, gs, ip, era, "
            f"k, bb, h, hr, whip, k_per_9, bb_per_9.\n"
            f"2. **IMPORTANT FILTER**: Only keep pitchers whose name "
            f"matches one of the following 2026 returning roster names "
            f"(ignore case, match last name if format differs): "
            f"{roster_names}.\n"
            f"   Discard all other pitchers — they are not returning "
            f"for 2026.\n"
            f"3. Call get_wm_pitcher_game_logs() to get game-by-game "
            f"appearance logs with per-outing IP, K, BB, ER.\n"
            f"4. For each RETURNING pitcher, compute the following derived metrics:\n"
            f"   - kPct   = k_per_9 / 9 * 100  (approximate)\n"
            f"   - bbPct  = bb_per_9 / 9 * 100  (approximate)\n"
            f"   - whiffPct = estimate from K-rate: kPct * 1.25\n"
            f"   - cswPct = 0.6 * whiffPct + 12\n"
            f"   - stuffPlus = 100 + (kPct - bbPct - 10) * 3 "
            f"     + (4.60 - ERA) * 5   (clamp 70-160)\n"
            f"   - hardHitPct = 33.0  (league avg placeholder)\n"
            f"   - tunnelingScore = 50, seqScore = 50  (neutral default)\n"
            f"   - avgVelo, avgSpin, ivb, hBreak = 0  (no trackman data)\n"
            f"   - chasePct = 20  (default), zonePct = 50  (default)\n"
            f"   - score = round(stuffPlus * 0.35 + (100 - bbPct * 5) * "
            f"     0.25 + kPct * 0.25 + (100 - hardHitPct * 2) * 0.15)\n"
            f"   - stuffPlusRaw = stuffPlus / 1.6  (raw seed)\n"
            f"5. Assign hand (RHP/LHP) from the scraped data, else RHP.\n"
            f"6. Assign role: SP if gs > g/2, else RP.\n"
            f"7. Build the history array from game-log ERA values "
            f"   (last 10 outings, mapped to score scale).\n"
            f"8. Build predictions as [score, score+1, score+2] (trend up).\n"
            f"9. Build predBands as 3 entries with +/-4 bounds.\n"
            f"You should produce exactly {len(ROSTER_2026_PITCHERS)} pitcher "
            f"profiles (one per returning pitcher).\n"
            f"Return a JSON array of pitcher profile objects."
        ),
        expected_output=(
            f"A JSON array of exactly {len(ROSTER_2026_PITCHERS)} pitcher "
            f"profile objects (only returning 2026 pitchers).  Example element:\n"
            '{"id": 1, "name": "Jones, A.", "hand": "RHP", "role": "SP", '
            '"metrics": {"stuffPlus": 112, "cswPct": 30.5, '
            '"whiffPct": 28.2, "kPct": 24.1, "bbPct": 7.2, '
            '"hardHitPct": 33.0, "tunnelingScore": 50, "seqScore": 50, '
            '"avgVelo": 0, "avgSpin": 0, "ivb": 0, "hBreak": 0, '
            '"chasePct": 20, "zonePct": 50, "score": 58, '
            '"stuffPlusRaw": 70}, '
            '"pitchData": {"pitches": [], "types": []}, '
            '"history": [58,58,58,58,58,58,58,58,58,58], '
            '"predictions": [58,59,60], '
            '"rank": 1, '
            '"predBands": [{"value":58,"lower":54,"upper":62}, '
            '{"value":59,"lower":53,"upper":63}, '
            '{"value":60,"lower":52,"upper":64}], '
            '"formStatus": "neutral", "formDelta": 0, '
            '"trendStrength": 0.1, "riskFlag": null, '
            '"ceiling": 66, "floor": 50}'
        ),
        agent=agents["pitcher_scout"],
    )

    # ---- Task 3: Align matchups ----
    task_matchups = Task(
        description=(
            "You will receive two pieces of context:\n"
            "  (A) The opponent-batter scouting report — a JSON array of "
            "batter profiles with team, avg, obp, slg, k_pct, bb_pct.\n"
            "  (B) The pitcher scouting report — a JSON array of pitcher "
            "profiles with kPct, bbPct, ERA, stuffPlus, game logs, etc.\n\n"
            "Your job:\n"
            f"1. Use the {TARGET_SCHOOL} schedule (from context) "
            f"to determine which opponent team each pitcher will face in "
            f"each game.\n"
            "2. For each pitcher × opponent-batter pair, compute a "
            "matchup_score (0-100):\n"
            "   matchup_score = 50\n"
            "     + (pitcher_kPct - batter_k_pct) * 0.8\n"
            "     + (batter_bb_pct - pitcher_bbPct) * -0.6\n"
            "     + (0.420 - batter_slg) * 40\n"
            "     + (pitcher_stuffPlus - 100) * 0.2\n"
            "   Clamp to 0-100.\n"
            "3. Label edge: 'pitcher' if score >= 60, 'batter' if < 40, "
            "else 'neutral'.\n"
            "4. Estimate projected_pa per batter per game:\n"
            "   - Starter faces ~25 BF; reliever ~8 BF.\n"
            "   - Distribute proportionally to batter GP/GS.\n"
            "Return a JSON array of matchup objects."
        ),
        expected_output=(
            "A JSON array of matchup objects.  Example:\n"
            '{"pitcher_id": 1, "pitcher_name": "Jones, A.", '
            '"batter_id": 12345, "batter_name": "Smith, J.", '
            '"batter_team": "Alabama", "game_date": "02/14/2025", '
            '"projected_pa": 3, "matchup_score": 64, '
            '"edge": "pitcher"}'
        ),
        agent=agents["matchup_aligner"],
        context=[task_scout_batters, task_scout_pitchers],
    )

    # ---- Task 4: Season predictions → session JSON ----
    task_predictions = Task(
        description=(
            "You have the matchup matrix plus the pitcher profiles from "
            "prior tasks.  Your job is to produce the FINAL output — a "
            "JSON array that the pitching-hub hex dashboard can render.\n\n"
            "FOR EACH PITCHER:\n"
            "1. game_by_game projections:\n"
            "   For each scheduled game the pitcher is expected to appear in:\n"
            "   - projected_era = pitcher's base ERA adjusted by the mean "
            "     matchup_score for that game's opponent batters:\n"
            "       adj_factor = (50 - avg_matchup_score) * 0.03\n"
            "       projected_era = base_era + adj_factor  (clamp >= 0)\n"
            "   - projected_k = (kPct / 100) * expected_BF_for_game\n"
            "   - projected_bb = (bbPct / 100) * expected_BF_for_game\n"
            "   - confidence = min(1.0, games_played / 15) * 0.85\n"
            "   - key_matchups = top 3 batter names with highest |score - 50|\n"
            "2. season_summary:\n"
            "   - projected_era = IP-weighted mean of game ERAs\n"
            "   - projected_k9 = total projected K / total projected IP * 9\n"
            "   - projected_bb9 = total projected BB / total projected IP * 9\n"
            "   - win_probability_avg = mean matchup_score / 100\n"
            "   - stuff_plus_projection = pitcher's stuffPlus * 0.85 + 15 "
            "     (regress to mean)\n\n"
            "3. Merge game_by_game and season_summary INTO the pitcher "
            "profile objects from Task 2 so the final JSON has ALL fields "
            "the hex UI needs:\n"
            "   id, name, hand, role, metrics, pitchData, history,\n"
            "   predictions, rank, predBands, formStatus, formDelta,\n"
            "   trendStrength, riskFlag, ceiling, floor,\n"
            "   game_by_game, season_summary\n\n"
            "4. Rank pitchers by their overall 'score' metric (best = 1).\n"
            "5. Return ONLY the JSON array — no markdown, no commentary."
        ),
        expected_output=(
            "A valid JSON array of pitcher session objects matching the "
            "pitching-hub schema.  Each object must contain: id, name, "
            "hand, role, metrics (with stuffPlus, cswPct, whiffPct, kPct, "
            "bbPct, hardHitPct, tunnelingScore, seqScore, avgVelo, "
            "avgSpin, ivb, hBreak, chasePct, zonePct, score, stuffPlusRaw), "
            "pitchData (with pitches and types arrays), history (list of "
            "floats), predictions (list of 3 floats), rank (int), "
            "predBands (list of 3 {value, lower, upper}), formStatus, "
            "formDelta, trendStrength, riskFlag, ceiling, floor, "
            "game_by_game (list of game projection dicts), and "
            "season_summary dict."
        ),
        agent=agents["season_predictor"],
        context=[task_scout_pitchers, task_matchups],
        output_file=str(OUTPUT_FILE),
    )

    return [task_scout_batters, task_scout_pitchers, task_matchups, task_predictions]


# ---------------------------------------------------------------------------
# Crew assembly and execution
# ---------------------------------------------------------------------------


def build_crew(agents: dict[str, Agent], tasks: list[Task]) -> Crew:
    """Assemble the sequential crew."""
    return Crew(
        agents=list(agents.values()),
        tasks=tasks,
        process=Process.sequential,
        verbose=True,
        cache=True,
        max_rpm=5,  # Throttle API calls to stay under rate limits
        output_log_file=str(
            Path(__file__).resolve().parent / "crew_run.log"
        ),
    )


def _pre_compute_scouting_data() -> dict:
    """Run data-heavy scouting in pure Python (no LLM needed).

    Returns a dict with 'batters_json' and 'pitchers_json' strings that
    can be injected directly into agent task descriptions.
    """
    from tools.ncaa_scraper import (
        _get_wm_schedule_impl,
        _get_opponent_batters_impl,
        _get_all_wm_pitcher_stats_impl,
        _get_wm_pitcher_game_logs_impl,
    )
    from collections import defaultdict
    import math

    # ── Step 1: Schedule + opponent batters ──
    print("\n📋 Step 1: Fetching schedule…")
    schedule = _get_wm_schedule_impl()
    print(f"   {len(schedule)} games, "
          f"{sum(1 for g in schedule if g.get('box_score_id'))} with box scores")

    # Group box score IDs by opponent
    opp_bs: dict[str, list[str]] = defaultdict(list)
    for g in schedule:
        if g.get("box_score_id"):
            opp_bs[g["opponent"]].append(g["box_score_id"])

    print(f"\n📋 Step 2: Scraping batters for {len(opp_bs)} opponents…")
    all_batters = []
    for i, (opp_name, bs_ids) in enumerate(sorted(opp_bs.items()), 1):
        print(f"   [{i}/{len(opp_bs)}] {opp_name} ({len(bs_ids)} games)…")
        batters = _get_opponent_batters_impl(opp_name, bs_ids)
        all_batters.extend(batters)
    print(f"   Total batters: {len(all_batters)}")

    # ── Step 2: Pitcher stats + game logs ──
    print(f"\n📋 Step 3: Fetching pitcher stats…")
    pitcher_stats = _get_all_wm_pitcher_stats_impl()
    print(f"   Found {len(pitcher_stats)} pitchers on roster")

    print(f"\n📋 Step 4: Building pitcher game logs…")
    game_logs = _get_wm_pitcher_game_logs_impl()
    print(f"   Built logs for {len(game_logs)} pitchers")

    # ── Step 3: Pre-compute pitcher profiles (filtering to 2026 roster) ──
    print(f"\n📋 Step 5: Computing pitcher profiles for 2026 returnees…")
    roster_lower = [n.lower() for n in ROSTER_2026_PITCHERS]

    pitcher_profiles = []
    pid = 1
    for p in pitcher_stats:
        name_lower = p["name"].lower().strip()
        # Match by full name or last name
        matched = False
        for rn in roster_lower:
            if name_lower == rn or name_lower.split(",")[0].strip() in rn or rn.split()[-1] in name_lower:
                matched = True
                break
        if not matched:
            continue

        ip = p.get("ip", 0)
        era = p.get("era", 0)
        k = p.get("k", 0)
        bb = p.get("bb", 0)
        h = p.get("h", 0)
        g = p.get("g", 0)
        gs = p.get("gs", 0)
        ip_for_rate = ip if ip > 0 else 1

        # Derived metrics
        kPct = round(k / ip_for_rate * 9 / 9 * 100, 1) if ip > 0 else 0
        bbPct = round(bb / ip_for_rate * 9 / 9 * 100, 1) if ip > 0 else 0
        whiffPct = round(kPct * 1.25, 1)
        cswPct = round(0.6 * whiffPct + 12, 1)
        stuffPlus = round(max(70, min(160,
            100 + (kPct - bbPct - 10) * 3 + (4.60 - era) * 5
        )))
        score = round(
            stuffPlus * 0.35
            + (100 - bbPct * 5) * 0.25
            + kPct * 0.25
            + (100 - 33.0 * 2) * 0.15
        )

        hand = p.get("hand") or "RHP"
        role = "SP" if gs > g / 2 else "RP"

        # Game log history
        pitcher_log = next(
            (lg for lg in game_logs
             if lg["pitcher_name"].lower().strip() == name_lower
             or name_lower.split(",")[0].strip() in lg["pitcher_name"].lower()),
            None,
        )
        history = []
        if pitcher_log:
            for entry in pitcher_log["game_logs"][-10:]:
                outing_era = entry.get("er", 0) / max(entry.get("ip", 1), 0.1) * 9
                h_score = max(20, min(90, score + (4.60 - outing_era) * 3))
                history.append(round(h_score, 1))
        if not history:
            history = [float(score)] * 5

        predictions = [score, score + 1, score + 2]
        predBands = [
            {"value": score, "lower": score - 4, "upper": score + 4},
            {"value": score + 1, "lower": score - 5, "upper": score + 5},
            {"value": score + 2, "lower": score - 6, "upper": score + 6},
        ]

        pitcher_profiles.append({
            "id": pid,
            "name": p["name"],
            "hand": hand,
            "role": role,
            "metrics": {
                "stuffPlus": stuffPlus,
                "cswPct": cswPct,
                "whiffPct": whiffPct,
                "kPct": kPct,
                "bbPct": bbPct,
                "hardHitPct": 33.0,
                "tunnelingScore": 50,
                "seqScore": 50,
                "avgVelo": 0, "avgSpin": 0, "ivb": 0, "hBreak": 0,
                "chasePct": 20, "zonePct": 50,
                "score": score,
                "stuffPlusRaw": round(stuffPlus / 1.6, 1),
            },
            "pitchData": {"pitches": [], "types": []},
            "history": history,
            "predictions": predictions,
            "rank": pid,
            "predBands": predBands,
            "formStatus": "neutral",
            "formDelta": 0,
            "trendStrength": 0.1,
            "riskFlag": None,
            "ceiling": min(score + 12, 95),
            "floor": max(score - 12, 20),
            "game_by_game": [],
            "season_summary": {
                "projected_era": era,
                "projected_k9": p.get("k_per_9", 0),
                "projected_bb9": p.get("bb_per_9", 0),
                "win_probability_avg": 0.5,
                "stuff_plus_projection": stuffPlus,
            },
        })
        pid += 1

    # Rank by score
    pitcher_profiles.sort(key=lambda p: p["metrics"]["score"], reverse=True)
    for i, pp in enumerate(pitcher_profiles, 1):
        pp["rank"] = i
        pp["id"] = i

    print(f"   Built {len(pitcher_profiles)} pitcher profiles")

    return {
        "batters": all_batters,
        "pitchers": pitcher_profiles,
        "schedule": schedule,
    }


def main():
    print("=" * 60)
    print("  Pitching Hub — CrewAI Scouting Pipeline")
    print("=" * 60)
    print(f"  Target school : {TARGET_SCHOOL}")
    print(f"  NCAA team ID  : {TARGET_NCAA_ID or '(not set)'}")
    print(f"  Season        : {SEASON_YEAR}")
    print(f"  Output file   : {OUTPUT_FILE}")
    print("=" * 60)

    if not TARGET_NCAA_ID:
        print(
            "\n⚠  TARGET_NCAA_ID is blank in config.py.  Set it to your "
            "school's numeric ID from stats.ncaa.org before running.\n"
            "Example: TARGET_NCAA_ID = 736  (Vanderbilt)\n"
        )
        sys.exit(1)

    # ── Phase 1: Pre-compute scouting data (no LLM, pure scraping) ──
    print("\n🔧 Phase 1: Pre-computing scouting data (no LLM)…\n")
    data = _pre_compute_scouting_data()

    # If we have valid pitcher profiles, we can write directly
    # The LLM-based matchup/prediction phases are optional enrichment
    pitcher_profiles = data["pitchers"]

    if not pitcher_profiles:
        print("\n⚠  No pitcher profiles generated. Check config ROSTER_2026_PITCHERS.")
        sys.exit(1)

    # ── Phase 2: LLM enrichment (matchup analysis + predictions) ──
    # Build a compact summary for the LLM
    batter_summary = []
    for b in data["batters"][:100]:  # top 100 batters (by AB)
        batter_summary.append(
            f"{b['name']} ({b['team']}): AVG={b['avg']:.3f} OBP={b['obp']:.3f} "
            f"SLG={b['slg']:.3f} K%={b['k_pct']:.1f} BB%={b['bb_pct']:.1f} "
            f"AB={b['total_ab']} G={b['vs_wm_games']}"
        )

    pitcher_summary = []
    for p in pitcher_profiles:
        m = p["metrics"]
        pitcher_summary.append(
            f"{p['name']} ({p['hand']}/{p['role']}): score={m['score']} "
            f"stuffPlus={m['stuffPlus']} K%={m['kPct']:.1f} BB%={m['bbPct']:.1f} "
            f"ERA={p['season_summary']['projected_era']:.2f}"
        )

    schedule_summary = []
    for g in data["schedule"]:
        if g.get("box_score_id"):
            schedule_summary.append(
                f"{g['date']} {'@' if g['home_away'] == 'away' else 'vs'} "
                f"{g['opponent']} {g.get('result', '') or ''} {g.get('score', '')}"
            )

    print(f"\n🤖 Phase 2: LLM matchup analysis…")
    print(f"   {len(batter_summary)} batters, {len(pitcher_summary)} pitchers, "
          f"{len(schedule_summary)} games\n")

    # Create a focused matchup + prediction crew (only 2 agents, 2 tasks)
    enrichment_agent = Agent(
        role="Baseball Matchup & Prediction Analyst",
        goal=(
            f"Analyse matchups between {TARGET_SCHOOL} pitchers and "
            f"opponent batters, then produce game-by-game predictions "
            f"for each pitcher."
        ),
        backstory=(
            "You are an elite analytics engine that combines pitcher "
            "profiles with opponent batting data to produce actionable "
            "matchup insights and season forecasts."
        ),
        llm=LLM_MODEL,
        tools=[],
        verbose=True,
        allow_delegation=False,
        max_iter=15,
    )

    enrichment_task = Task(
        description=(
            f"Here is the pre-computed scouting data for {TARGET_SCHOOL} "
            f"({SEASON_YEAR} season):\n\n"
            f"=== PITCHERS ({len(pitcher_summary)}) ===\n"
            + "\n".join(pitcher_summary) + "\n\n"
            f"=== TOP OPPONENT BATTERS ({len(batter_summary)}) ===\n"
            + "\n".join(batter_summary[:50]) + "\n\n"  # limit to 50
            f"=== SCHEDULE ({len(schedule_summary)} games) ===\n"
            + "\n".join(schedule_summary[:30]) + "\n\n"  # limit to 30
            "YOUR TASK:\n"
            "For each pitcher, produce a JSON object with:\n"
            "- game_by_game: array of up to 10 games with "
            "{opponent, date, projected_era, projected_k, projected_bb, "
            "confidence, key_matchups}\n"
            "- season_summary: {projected_era, projected_k9, projected_bb9, "
            "win_probability_avg, stuff_plus_projection}\n\n"
            "Return a JSON array with one object per pitcher, keyed by name:\n"
            '[{"name": "...", "game_by_game": [...], "season_summary": {...}}, ...]'
        ),
        expected_output=(
            "A JSON array of pitcher enrichment objects with game_by_game "
            "projections and season_summary."
        ),
        agent=enrichment_agent,
    )

    enrichment_crew = Crew(
        agents=[enrichment_agent],
        tasks=[enrichment_task],
        process=Process.sequential,
        verbose=True,
        cache=True,
        max_rpm=3,
        output_log_file=str(Path(__file__).resolve().parent / "crew_run.log"),
    )

    try:
        result = enrichment_crew.kickoff()
        raw = result.raw.strip()

        # Parse LLM enrichment output
        if raw.startswith("```"):
            lines = raw.split("\n")
            lines = [l for l in lines if not l.strip().startswith("```")]
            raw = "\n".join(lines)

        enrichments = json.loads(raw)
        if isinstance(enrichments, dict) and "pitchers" in enrichments:
            enrichments = enrichments["pitchers"]
        if not isinstance(enrichments, list):
            enrichments = [enrichments]

        # Merge enrichments into pitcher profiles
        for enr in enrichments:
            enr_name = enr.get("name", "").lower()
            for pp in pitcher_profiles:
                if pp["name"].lower() == enr_name or enr_name in pp["name"].lower():
                    if "game_by_game" in enr:
                        pp["game_by_game"] = enr["game_by_game"]
                    if "season_summary" in enr:
                        pp["season_summary"].update(enr["season_summary"])
                    break

        print(f"\n✅ Merged enrichments for {len(enrichments)} pitchers")

    except Exception as e:
        print(f"\n⚠  LLM enrichment failed ({e}); using base profiles")

    # ── Phase 3: Write output ──
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_FILE, "w") as f:
        json.dump(pitcher_profiles, f, indent=2)

    print(f"\n✅ Wrote {len(pitcher_profiles)} pitcher(s) to {OUTPUT_FILE}")
    print("\nDone.")


if __name__ == "__main__":
    main()
