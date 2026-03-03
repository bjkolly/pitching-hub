# Import School Feature — Implementation Plan

## Overview
Add an "Import School" feature to the app where a user can search for any NCAA D1 baseball school, and the app automatically scrapes pitcher data from The Baseball Cube, runs analysis (derived metrics + enrichPitchers), and imports the team into the dropdown — all from within the UI.

## Architecture

### New Files
1. **`server/scraper.js`** — Node.js port of Python batch_scraper.py (uses `cheerio` for HTML parsing)
2. **`data/school_registry.json`** — Static registry of ~300 NCAA D1 baseball schools with Baseball Cube NCAA IDs

### Modified Files
3. **`server/index.js`** — Three new API endpoints + dual-path team data resolution for Vercel
4. **`client/index.html`** — Import panel UI (slide-in panel matching scout panel pattern)
5. **`vercel.json`** — Add school_registry.json to includeFiles
6. **`package.json`** — Add `cheerio` dependency

---

## Phase 1: Foundation

### 1a. Install `cheerio` (Node.js HTML parser, replaces Python's BeautifulSoup)

### 1b. Build School Registry (`data/school_registry.json`)
- Scrape The Baseball Cube college listing to build a comprehensive list of all D1 baseball programs
- Each entry: `{ name, aliases[], ncaa_id, conf }`
- Enables fuzzy search by name and common abbreviations

### 1c. Create `server/scraper.js`
Port from Python `agents/batch_scraper.py` to Node.js:
- `scrapePitchers(ncaaId, teamName, onProgress)` — Fetch Baseball Cube, parse pitching table, extract stats
- `computeMetrics(raw)` — Same formulas: stuffPlus, cswPct, whiffPct, kPct, bbPct, score
- `buildPitcherEntry(idx, raw)` — Build session-compatible pitcher JSON
- `importTeam(ncaaId, teamName, onProgress)` — Full pipeline orchestrator
- `searchSchools(query, registry)` — Fuzzy search with Levenshtein distance

---

## Phase 2: Server API (`server/index.js`)

### `GET /api/schools/search?q=<query>`
- Fuzzy search against school_registry.json (name + aliases)
- Scoring: exact match (100) → starts-with (90) → includes (80) → Levenshtein ≤2 (60)
- Returns top 10 matches

### `POST /api/schools/resolve` (LLM fallback)
- When fuzzy search returns no results, calls Claude API with query + full school list
- Claude resolves ambiguous queries ("the tigers from Baton Rouge" → LSU)
- Gracefully skipped if ANTHROPIC_API_KEY not set
- Returns matched school from registry

### `POST /api/schools/import` (SSE streaming)
- Body: `{ ncaa_id, name, slug? }`
- Streams progress events matching existing SSE pattern from `/api/run-agents`
- Steps: fetch → parse → filter → metrics → enrich → write → manifest update
- Writes to `data/teams/` (local) or `/tmp/pitching-hub/teams/` (Vercel)
- Updates manifest.json with new team entry

### Dual-path team data resolution
- Modify `readTeamManifest()` and `readTeamData()` to check both repo dir and runtime /tmp dir
- Ensures newly imported teams are visible on Vercel immediately

---

## Phase 3: Client UI (`client/index.html`)

### Import Panel (slide-in, matches scout panel pattern)
- "📥 Import School" button in header
- 420px slide-in panel from right with:
  - Search input with debounced autocomplete (300ms)
  - Results list showing school name, conference, "Import" button
  - "Already imported" badge for existing teams
  - SSE progress log + progress bar during import
  - Auto-switches to imported team in dropdown on success

### LLM Fallback UX
- If fuzzy search returns zero results → shows "Asking AI..." → calls `/api/schools/resolve`
- If LLM finds a match → shows it with "AI-resolved match:" label
- If no match at all → "No schools found. Try a different name."

---

## Phase 4: Deploy

### `vercel.json`
- Add `data/school_registry.json` to includeFiles

### Environment Variables
- `ANTHROPIC_API_KEY` (optional) — enables LLM fallback for ambiguous searches

---

## SSE Event Format

| Event | Data | When |
|-------|------|------|
| `status` | `{ step, message }` | Import begins |
| `progress` | `{ step, message, pct }` | Each pipeline stage (10→40→50→70→95%) |
| `done` | `{ success, slug, name, pitchers }` | Complete |
| `done` | `{ success: false, error }` | Failure |

---

## Implementation Order
1. `npm install cheerio`
2. Create `data/school_registry.json` (one-time scrape of Baseball Cube college listing)
3. Create `server/scraper.js` (port scraper + search to Node.js)
4. Add API endpoints to `server/index.js`
5. Add import panel UI to `client/index.html`
6. Update `vercel.json`
7. Test locally, commit, push, deploy
