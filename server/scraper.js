/**
 * Node.js scraper module — port of agents/batch_scraper.py
 *
 * Fetches pitcher stats from The Baseball Cube, computes derived metrics,
 * and builds session-compatible pitcher entries for the hex dashboard.
 *
 * Uses cheerio for HTML parsing (replaces Python's BeautifulSoup).
 */

import * as cheerio from 'cheerio';
import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

const __filename = fileURLToPath(import.meta.url);
const __dirname  = path.dirname(__filename);

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const SEASON_YEAR = 2025;
const BASE_URL    = 'https://www.thebaseballcube.com/content/stats_college';
const D1_AVG_ERA  = 4.60;

const HTTP_HEADERS = {
  'User-Agent':
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) ' +
    'AppleWebKit/537.36 (KHTML, like Gecko) ' +
    'Chrome/125.0.0.0 Safari/537.36',
  'Accept':
    'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
  'Accept-Language': 'en-US,en;q=0.5',
  'Connection':      'keep-alive',
};

const SCHOOL_REGISTRY_PATH = path.join(__dirname, '../data/school_registry.json');

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function safeFloat(val, def = 0) {
  if (val == null) return def;
  const s = String(val).trim().replace(/,/g, '');
  if (!s || s === '-' || s === '\u2014' || s === '\u2013' || s === '*' || s === 'INF') return def;
  const n = parseFloat(s);
  return Number.isFinite(n) ? n : def;
}

function safeInt(val, def = 0) {
  if (val == null) return def;
  const s = String(val).trim().replace(/,/g, '');
  if (!s || s === '-' || s === '\u2014' || s === '\u2013' || s === '*') return def;
  const n = Math.round(parseFloat(s));
  return Number.isFinite(n) ? n : def;
}

function levenshtein(a, b) {
  const m = a.length, n = b.length;
  const dp = Array.from({ length: m + 1 }, () => new Array(n + 1).fill(0));
  for (let i = 0; i <= m; i++) dp[i][0] = i;
  for (let j = 0; j <= n; j++) dp[0][j] = j;
  for (let i = 1; i <= m; i++) {
    for (let j = 1; j <= n; j++) {
      dp[i][j] = a[i - 1] === b[j - 1]
        ? dp[i - 1][j - 1]
        : 1 + Math.min(dp[i - 1][j], dp[i][j - 1], dp[i - 1][j - 1]);
    }
  }
  return dp[m][n];
}

// ---------------------------------------------------------------------------
// Scraping
// ---------------------------------------------------------------------------

/**
 * Fetch and parse pitcher stats from The Baseball Cube.
 * @param {string} ncaaId  Baseball Cube NCAA ID
 * @param {string} teamName  Human-readable name (for logging/progress)
 * @param {function} onProgress  callback({step, message, pct})
 * @returns {Promise<object[]>}  Array of raw pitcher stat objects
 */
export async function scrapePitchers(ncaaId, teamName, onProgress = () => {}) {
  const url = `${BASE_URL}/${SEASON_YEAR}~${ncaaId}/`;
  onProgress({ step: 'fetch', message: `Fetching ${teamName} from Baseball Cube...`, pct: 10 });

  const resp = await fetch(url, {
    headers: HTTP_HEADERS,
    signal: AbortSignal.timeout(30_000),
  });
  if (!resp.ok) throw new Error(`HTTP ${resp.status} fetching ${teamName}`);

  const html = await resp.text();
  const $ = cheerio.load(html);

  onProgress({ step: 'parse', message: 'Parsing pitching table...', pct: 25 });

  // Find pitching table — primary: id="grid2", fallback: any table with era+ip headers
  let table = $('table#grid2');
  if (!table.length) {
    $('table').each((_i, t) => {
      const hdr = $(t).find('th').map((_j, th) => $(th).text().trim().toLowerCase()).get().join(' ');
      if (hdr.includes('era') && hdr.includes('ip')) { table = $(t); return false; }
    });
  }
  if (!table.length) throw new Error(`No pitching table found for ${teamName}`);

  // Parse headers
  const headers = [];
  table.find('tr').first().find('th, td').each((_i, el) => {
    headers.push($(el).text().trim().toLowerCase());
  });
  const col = {};
  headers.forEach((name, i) => { col[name] = i; });

  // Parse rows
  const pitchers = [];
  table.find('tr').slice(1).each((_i, tr) => {
    const cells = $(tr).find('td, th').map((_j, c) => $(c).text().trim()).get();
    if (cells.length < headers.length) return;

    const _g = (key, ...alts) => {
      for (const k of [key, ...alts]) {
        if (col[k] !== undefined && col[k] < cells.length) return cells[col[k]];
      }
      return '';
    };

    const name = _g('player', 'name');
    const norm = name.trim().toLowerCase().replace(/\s+/g, ' ');
    if (!name || ['totals', 'total', 'team', ''].includes(norm) || norm.startsWith('totals')) return;

    const ip = safeFloat(_g('ip'));
    const k  = safeInt(_g('so', 'k'));
    const bb = safeInt(_g('bb'));
    const h  = safeInt(_g('h'));
    const ipR = ip > 0 ? ip : 1;

    const handRaw = _g('th', 'throws', 't');
    let hand = null;
    if (handRaw) {
      if (handRaw.toUpperCase().startsWith('L')) hand = 'LHP';
      else if (handRaw.toUpperCase().startsWith('R')) hand = 'RHP';
    }

    pitchers.push({
      name, hand,
      g:  safeInt(_g('g', 'gp')),
      gs: safeInt(_g('gs')),
      ip, era: safeFloat(_g('era')),
      k, bb, h,
      hr: safeInt(_g('hr')),
      k_per_9: ip > 0 ? Math.round(k / ipR * 9 * 10) / 10 : 0,
      bb_per_9: ip > 0 ? Math.round(bb / ipR * 9 * 10) / 10 : 0,
    });
  });

  onProgress({ step: 'scraped', message: `Found ${pitchers.length} pitchers`, pct: 40 });
  return pitchers;
}

// ---------------------------------------------------------------------------
// Derived metrics (same formulas as batch_scraper.py)
// ---------------------------------------------------------------------------

export function computeMetrics(raw) {
  const { ip, k, bb, h, era } = raw;

  const bf = ip > 0 ? 3 * ip + h + bb : 1;
  const bfSafe = Math.max(bf, 1);

  const kPct    = Math.round(k / bfSafe * 100 * 10) / 10;
  const bbPct   = Math.round(bb / bfSafe * 100 * 10) / 10;
  const whiffPct = Math.round(kPct * 1.25 * 10) / 10;
  const cswPct   = Math.round((0.6 * whiffPct + 12) * 10) / 10;

  let stuffPlus = 100 + (kPct - bbPct - 10) * 3 + (D1_AVG_ERA - era) * 5;
  stuffPlus = Math.round(Math.max(70, Math.min(160, stuffPlus)));

  let stuffPlusRaw = Math.round(100 + (kPct - bbPct - 10) * 3);
  stuffPlusRaw = Math.max(30, Math.min(150, stuffPlusRaw));

  const hardHitPct = 33.0;
  const score = Math.round(
    stuffPlus * 0.35 +
    (100 - bbPct * 5) * 0.25 +
    kPct * 0.25 +
    (100 - hardHitPct * 2) * 0.15
  );

  return {
    stuffPlus, cswPct, whiffPct, kPct, bbPct,
    hardHitPct, tunnelingScore: 50, seqScore: 50,
    avgVelo: 0, avgSpin: 0, ivb: 0, hBreak: 0,
    chasePct: 20, zonePct: 50,
    score, stuffPlusRaw,
  };
}

// ---------------------------------------------------------------------------
// Build session-compatible pitcher entries
// ---------------------------------------------------------------------------

export function buildPitcherEntry(idx, raw) {
  const metrics = computeMetrics(raw);
  const score   = metrics.score;

  const gs = raw.gs || 0;
  const g  = Math.max(raw.g || 1, 1);
  const role = gs > 0 && gs / g > 0.4 ? 'SP' : 'RP';

  // Format name as "Last, F."
  let displayName;
  const parts = raw.name.split(',');
  if (parts.length >= 2) {
    const last  = parts[0].trim();
    const first = parts[1].trim();
    displayName = first ? `${last}, ${first[0]}.` : last;
  } else {
    const words = raw.name.trim().split(/\s+/);
    displayName = words.length >= 2
      ? `${words[words.length - 1]}, ${words[0][0]}.`
      : raw.name;
  }

  const hand   = raw.hand || 'RHP';
  const spread = Math.max(4, Math.round(score * 0.07));

  return {
    id: idx,
    name: displayName,
    hand, role, metrics,
    pitchData: { pitches: [], types: [] },
    history: Array(10).fill(score),
    predictions: [score, score + 1, score + 1],
    rank: 0,
    predBands: [
      { value: score,     lower: score - spread, upper: score + spread },
      { value: score + 1, lower: score - spread + 1, upper: score + spread + 1 },
      { value: score + 1, lower: score - spread + 1, upper: score + spread + 1 },
    ],
    formStatus: 'neutral',
    formDelta: 0,
    trendStrength: 0.1,
    riskFlag: null,
    ceiling: score + spread,
    floor: Math.max(0, score - spread),
    game_by_game: [],
    season_summary: {
      projected_era: raw.era,
      projected_k9: raw.k_per_9,
      projected_bb9: raw.bb_per_9,
      win_probability_avg: 0.5,
      stuff_plus_projection: metrics.stuffPlus,
    },
  };
}

// ---------------------------------------------------------------------------
// Full import pipeline
// ---------------------------------------------------------------------------

export async function importTeam(ncaaId, teamName, onProgress = () => {}) {
  const rawPitchers = await scrapePitchers(ncaaId, teamName, onProgress);

  const withIP = rawPitchers.filter(p => p.ip > 0);
  onProgress({ step: 'filter', message: `${withIP.length} pitchers with IP > 0`, pct: 50 });

  if (!withIP.length) throw new Error(`No pitchers with innings found for ${teamName}`);

  const entries = withIP.map((raw, idx) => buildPitcherEntry(idx, raw));
  onProgress({ step: 'metrics', message: `Computed metrics for ${entries.length} pitchers`, pct: 70 });

  entries.sort((a, b) => b.metrics.score - a.metrics.score);
  entries.forEach((e, i) => { e.rank = i + 1; e.id = i; });

  onProgress({ step: 'ranked', message: `${entries.length} pitchers ranked and ready`, pct: 85 });
  return entries;
}

// ---------------------------------------------------------------------------
// Fuzzy school search
// ---------------------------------------------------------------------------

let _registry = null;

function loadRegistry() {
  if (_registry) return _registry;
  try {
    _registry = JSON.parse(fs.readFileSync(SCHOOL_REGISTRY_PATH, 'utf8'));
  } catch {
    _registry = [];
  }
  return _registry;
}

/**
 * Search the school registry with fuzzy matching.
 * Returns top 10 results sorted by relevance score.
 */
export function searchSchools(query) {
  const registry = loadRegistry();
  if (!query) return [];

  const q = query.trim().toLowerCase();
  const scored = [];

  for (const school of registry) {
    const nameLc = school.name.toLowerCase();
    const aliasesLc = (school.aliases || []).map(a => a.toLowerCase());
    const allNames = [nameLc, ...aliasesLc];

    let best = 0;

    for (const n of allNames) {
      if (n === q)                  { best = Math.max(best, 100); continue; }
      if (n.startsWith(q))          { best = Math.max(best, 90);  continue; }
      if (n.includes(q))            { best = Math.max(best, 80);  continue; }
      if (q.length >= 3) {
        const d = levenshtein(q, n);
        if (d <= 1) best = Math.max(best, 75);
        else if (d <= 2) best = Math.max(best, 60);
        else if (d <= 3) best = Math.max(best, 45);
      }
    }

    // Also check if all query words appear in the name
    if (best < 70) {
      const words = q.split(/\s+/);
      const joined = allNames.join(' ');
      if (words.length > 1 && words.every(w => joined.includes(w))) {
        best = Math.max(best, 78);
      }
    }

    if (best > 0) {
      scored.push({ ...school, _score: best });
    }
  }

  scored.sort((a, b) => b._score - a._score);
  return scored.slice(0, 10).map(({ _score, ...rest }) => rest);
}
