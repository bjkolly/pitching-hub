TARGET_SCHOOL = "William & Mary"
TARGET_SCHOOL_SLUG = "William-Mary"
TARGET_NCAA_ID = "20486"
TARGET_CONFERENCE = "CAA"
SEASON_YEAR = 2025
# Primary data sources
TRIBE_SCHEDULE_URL = "https://tribeathletics.com/sports/baseball/schedule/2025"
TRIBE_STATS_URL = "https://tribeathletics.com/sports/baseball/stats/2025"
BASEBALL_CUBE_URL = "https://www.thebaseballcube.com/content/stats_college/2025~20486/"
WARREN_NOLAN_URL = "https://www.warrennolan.com/baseball/2025/schedule/William-Mary"
# 2025 schedule opponents with slugs for scraping
CAA_OPPONENTS = [
    {"name": "NC A&T",       "slug": "north-carolina-a-t",   "games": 3},
    {"name": "Hofstra",      "slug": "hofstra",              "games": 3},
    {"name": "Elon",         "slug": "elon",                 "games": 3},
    {"name": "UNCW",         "slug": "uncw",                 "games": 3},
    {"name": "Campbell",     "slug": "campbell",             "games": 3},
    {"name": "Stony Brook",  "slug": "stony-brook",          "games": 3},
    {"name": "Charleston",   "slug": "charleston",           "games": 3},
    {"name": "Northeastern", "slug": "northeastern",         "games": 3},
    {"name": "Towson",       "slug": "towson",               "games": 3},
]
NON_CONF_OPPONENTS = [
    {"name": "Rhode Island", "slug": "rhode-island"},
    {"name": "Richmond",     "slug": "richmond"},
    {"name": "George Mason", "slug": "george-mason"},
    {"name": "Virginia",     "slug": "virginia"},
    {"name": "East Carolina","slug": "east-carolina"},
    {"name": "Old Dominion", "slug": "old-dominion"},
    {"name": "Navy",         "slug": "navy"},
    {"name": "Duke",         "slug": "duke"},
    {"name": "Princeton",    "slug": "princeton"},
    {"name": "Penn State",   "slug": "penn-state"},
    {"name": "VCU",          "slug": "vcu"},
    {"name": "Marist",       "slug": "marist"},
    {"name": "Longwood",     "slug": "longwood"},
    {"name": "Boston College","slug": "boston-college"},
    {"name": "Toledo",       "slug": "toledo"},
    {"name": "VMI",          "slug": "vmi"},
]
# Box score base URL pattern (for scraping individual game data)
BOX_SCORE_BASE = "https://tribeathletics.com/sports/baseball/stats/2025/{slug}/boxscore/{game_id}"
# 2026 roster names (from tribeathletics.com/sports/baseball/roster)
# Only pitchers who appeared in 2025 stats AND are on the 2026 roster.
ROSTER_2026_PITCHERS = [
    "Zach Boyd",
    "Daniel Lingle",
    "Chad Yates",
    "Tyler Kelly",
    "Jack Weight",
    "Owen Pierce",
    "Tom Bourque",
    "Noah Hertzler",
    "Zack Potts",
]
OUTPUT_PATH = "../data/crew_session.json"
CACHE_PATH = "../data/cache/"
