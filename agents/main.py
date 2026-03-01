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
        llm=LLM_MODEL,
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
        llm=LLM_MODEL,
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
        output_log_file=str(
            Path(__file__).resolve().parent / "crew_run.log"
        ),
    )


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

    # Build components
    agents = build_agents()
    tasks = build_tasks(agents)
    crew = build_crew(agents, tasks)

    # Run
    print("\nKicking off crew…\n")
    result = crew.kickoff(
        inputs={
            "target_school": TARGET_SCHOOL,
            "ncaa_team_id": TARGET_NCAA_ID,
            "season_year": SEASON_YEAR,
        }
    )

    # ---- Post-process: ensure output file is valid JSON ----
    raw = result.raw
    try:
        # The LLM sometimes wraps JSON in markdown fences
        cleaned = raw.strip()
        if cleaned.startswith("```"):
            # Strip ```json ... ```
            lines = cleaned.split("\n")
            lines = [l for l in lines if not l.strip().startswith("```")]
            cleaned = "\n".join(lines)
        parsed = json.loads(cleaned)

        # Ensure we have a list
        if isinstance(parsed, dict) and "pitchers" in parsed:
            parsed = parsed["pitchers"]
        if not isinstance(parsed, list):
            parsed = [parsed]

        OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(OUTPUT_FILE, "w") as f:
            json.dump(parsed, f, indent=2)

        print(f"\n✅ Wrote {len(parsed)} pitcher(s) to {OUTPUT_FILE}")

    except (json.JSONDecodeError, TypeError) as e:
        # Write raw output as fallback so nothing is lost
        OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(OUTPUT_FILE, "w") as f:
            f.write(raw)
        print(f"\n⚠  JSON parse failed ({e}); raw output saved to {OUTPUT_FILE}")

    print("\nDone.")


if __name__ == "__main__":
    main()
