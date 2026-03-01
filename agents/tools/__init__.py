"""
William & Mary Baseball Scraping Tools

Six functions for scraping schedule, box scores, pitcher stats,
opponent batting, game logs, and projected schedules.
"""

from .ncaa_scraper import (
    get_wm_schedule,
    get_box_score,
    get_all_wm_pitcher_stats,
    get_opponent_batters,
    get_wm_pitcher_game_logs,
    get_upcoming_schedule_2026,
)

__all__ = [
    "get_wm_schedule",
    "get_box_score",
    "get_all_wm_pitcher_stats",
    "get_opponent_batters",
    "get_wm_pitcher_game_logs",
    "get_upcoming_schedule_2026",
]
