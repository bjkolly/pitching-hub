/**
 * trackman.js
 * -----------
 * TrackmanClient wraps the Trackman Baseball REST API (staging + production).
 * normalizePitches() is the shared pitch-row → pitcher-object transformer used
 * by both the live API path and the CSV upload path.
 *
 * Required env vars (see .env.example):
 *   TRACKMAN_API_KEY    Bearer token from the Trackman developer portal
 *   TRACKMAN_TEAM_ID    Organisation / team identifier
 *
 * Optional:
 *   TRACKMAN_BASE_URL   Override the default staging URL below
 *
 * ─── Assumed REST API shape ──────────────────────────────────────────────────
 *
 * GET /pitching/sessions?teamId=&date=YYYY-MM-DD
 * GET /pitching/live?teamId=
 *
 * Both endpoints are expected to return one of:
 *   • { pitches: [ ...pitchRow ] }          ← most common
 *   • { session: { pitches: [ ... ] } }
 *   • { data: [ ...pitchRow ] }
 *   • [ ...pitchRow ]                       ← flat array
 *
 * Each pitchRow (camelCase from API, PascalCase from CSV — both handled):
 *   pitcher / Pitcher                string
 *   pitcherThrows / PitcherThrows    "Right" | "Left"
 *   taggedPitchType / TaggedPitchType string
 *   relSpeed / RelSpeed              number   (mph)
 *   spinRate / SpinRate              number   (rpm)
 *   inducedVertBreak / InducedVertBreak  number (in)
 *   horzBreak / HorzBreak            number   (in)
 *   plateLocHeight / PlateLocHeight  number   (ft)
 *   plateLocSide / PlateLocSide      number   (ft)
 *   pitchCall / PitchCall            string
 *   exitSpeed / ExitSpeed            number | null (mph)
 */

// ── Staging base URL ──────────────────────────────────────────────────────────
// ⚠️  Replace with the URL from your Trackman developer portal if different.
//     Common patterns:
//       https://staging-api.trackmanbaseball.com/v1
//       https://portal.trackmanbaseball.com/api/v1
const STAGING_URL = 'https://staging-api.trackmanbaseball.com/v1';

// ── Pitch-type colour palette (mirrors the UI constants) ──────────────────────
const PITCH_TYPE_MAP = {
  Fastball:         { n: '4-Seam FB',  c: '#4a9eff' },
  'Four-Seam':      { n: '4-Seam FB',  c: '#4a9eff' },
  FourSeamFastBall: { n: '4-Seam FB',  c: '#4a9eff' },
  Sinker:           { n: 'Sinker',     c: '#f04e5e' },
  TwoSeamFastBall:  { n: 'Sinker',     c: '#f04e5e' },
  Cutter:           { n: 'Cutter',     c: '#ffe044' },
  Curveball:        { n: 'Curveball',  c: '#a476ff' },
  CurveBall:        { n: 'Curveball',  c: '#a476ff' },
  Slider:           { n: 'Slider',     c: '#ff7c40' },
  ChangeUp:         { n: 'Changeup',   c: '#3dd68c' },
  Changeup:         { n: 'Changeup',   c: '#3dd68c' },
  Splitter:         { n: 'Splitter',   c: '#2ec4b6' },
  Knuckleball:      { n: 'Knuckleball',c: '#ff9ac0' },
};

function _pitchType(raw = '') {
  return PITCH_TYPE_MAP[raw] || { n: raw || 'Unknown', c: '#8899bb' };
}

// ── Shared math helpers ───────────────────────────────────────────────────────
const _clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
const _avg   = arr => arr.length ? arr.reduce((s, x) => s + x, 0) / arr.length : 0;
const _float = (v, fb = 0) => { const n = parseFloat(v); return isNaN(n) ? fb : n; };

// Read a field that may arrive as camelCase (API) or PascalCase (CSV).
const _f = (row, camel, pascal) => {
  const v = row[camel] ?? row[pascal];
  return v !== undefined && v !== null && v !== '' ? v : undefined;
};

// ── Composite score (exact port of the UI's calcScore) ────────────────────────
function _calcScore(m) {
  const n = (v, a, b) => _clamp((v - a) / (b - a), 0, 1);
  return Math.round(_clamp(
    n(m.stuffPlus, 60, 140) * 22 + n(m.cswPct, 17, 42) * 18 + n(m.whiffPct, 11, 40) * 16 +
    n(m.tunnelingScore, 28, 99) * 10 + (n(m.kPct, 11, 38) - n(m.bbPct, 2.5, 16)) * 0.5 * 12 +
    (1 - n(m.hardHitPct, 20, 52)) * 7 + n(m.seqScore, 28, 98) * 6 + n(m.chasePct, 20, 50) * 5 +
    n(m.avgVelo, 84, 101) * 2 + n(m.avgSpin, 1850, 2900) * 2,
    18, 99
  ));
}

// ── Shared normalizer ─────────────────────────────────────────────────────────
/**
 * normalizePitches(rawPitches)
 * ----------------------------
 * Converts a flat array of pitch rows (from the API or a parsed CSV) into the
 * pitcher-object array that session.json and the UI expect.
 *
 * Handles both camelCase field names (REST API) and PascalCase (CSV headers).
 *
 * @param  {object[]} rawPitches
 * @returns {PitcherArray}  Sorted by score desc, with rank assigned.
 */
export function normalizePitches(rawPitches) {
  // ── Group by pitcher name ─────────────────────────────────────────────────
  const byPitcher = {};
  for (const row of rawPitches) {
    const name = (_f(row, 'pitcher', 'Pitcher') || '').trim();
    if (!name) continue;
    (byPitcher[name] ??= []).push(row);
  }

  const pitchers = [];
  let id = 1;

  for (const [name, pitches] of Object.entries(byPitcher)) {
    const total = pitches.length;
    const hand  = _f(pitches[0], 'pitcherThrows', 'PitcherThrows') === 'Left' ? 'LHP' : 'RHP';

    // ── Velocity / spin / movement ──────────────────────────────────────────
    const velos   = pitches.map(p => _float(_f(p, 'relSpeed',           'RelSpeed'))).filter(v => v > 0);
    const spins   = pitches.map(p => _float(_f(p, 'spinRate',           'SpinRate'))).filter(v => v > 0);
    const ivbs    = pitches.map(p => _float(_f(p, 'inducedVertBreak',   'InducedVertBreak'))).filter(v => !isNaN(v));
    const hBreaks = pitches.map(p => _float(_f(p, 'horzBreak',          'HorzBreak'))).filter(v => !isNaN(v));

    const avgVelo = _clamp(_avg(velos),               84, 101);
    const avgSpin = _clamp(Math.round(_avg(spins)),  1850, 2900);
    const ivb     = _clamp(Math.abs(_avg(ivbs)),       7,   25);
    const hBreak  = _clamp(Math.abs(_avg(hBreaks)),    4,   23);

    // ── Pitch-call breakdown ────────────────────────────────────────────────
    const call    = p => _f(p, 'pitchCall', 'PitchCall') || '';
    const called   = pitches.filter(p => call(p) === 'StrikeCalled').length;
    const swinging = pitches.filter(p => call(p) === 'StrikeSwinging').length;
    const balls    = pitches.filter(p => call(p) === 'BallCalled' || call(p) === 'BallIntentional').length;
    const fouls    = pitches.filter(p => call(p) === 'FoulBall').length;
    const inPlay   = pitches.filter(p => call(p) === 'InPlay').length;
    const swings   = swinging + fouls + inPlay;

    const cswPct   = _clamp((called + swinging) / total * 100, 17, 42);
    const whiffPct = _clamp(swings > 0 ? swinging / swings * 100 : 0, 11, 40);
    // K% and BB% — proxy from pitch-level data (no PA boundary tracking)
    const kPct     = _clamp(whiffPct * 0.65 + 5, 11, 38);
    const bbPct    = _clamp(total > 0 ? balls / total * 20 : 8, 2.5, 16);

    // ── Hard-hit % ──────────────────────────────────────────────────────────
    const inPlayRows = pitches.filter(p => call(p) === 'InPlay');
    const hardHits   = inPlayRows.filter(p => _float(_f(p, 'exitSpeed', 'ExitSpeed')) >= 95).length;
    const hardHitPct = _clamp(inPlayRows.length > 0 ? hardHits / inPlayRows.length * 100 : 35, 20, 52);

    // ── Zone / chase ─────────────────────────────────────────────────────────
    const side   = p => _float(_f(p, 'plateLocSide',   'PlateLocSide'));
    const height = p => _float(_f(p, 'plateLocHeight', 'PlateLocHeight'), 2.5);

    const inZone  = p => Math.abs(side(p)) <= 0.7083 && height(p) >= 1.5 && height(p) <= 3.5;
    const isChase = p => !inZone(p) && (call(p) === 'StrikeSwinging' || call(p) === 'FoulBall');

    const outZone  = pitches.filter(p => !inZone(p));
    const chasePct = _clamp(outZone.length > 0 ? outZone.filter(isChase).length / outZone.length * 100 : 30, 20, 50);
    const zonePct  = _clamp(pitches.filter(inZone).length / total * 100, 38, 62);

    // ── Raw Stuff+ (score.py normalises this to league avg = 100) ───────────
    const stuffPlus = _clamp(Math.round(
      60 +
      _clamp((avgVelo - 84)   / 17,   0, 1) * 35 +
      _clamp((avgSpin - 1850) / 1050, 0, 1) * 25 +
      _clamp((ivb - 7)        / 18,   0, 1) * 20
    ), 55, 140);

    // ── Tunneling & sequencing (synthetic from available metrics) ────────────
    const tunnelingScore = _clamp(Math.round(
      40 + _clamp(ivb / 25, 0, 1) * 25 +
           _clamp(Math.abs(hBreak) / 23, 0, 1) * 20 +
           _clamp((whiffPct - 11) / 29, 0, 1) * 15
    ), 28, 99);

    const seqScore = _clamp(Math.round(
      40 + _clamp((cswPct - 17) / 25, 0, 1) * 25 +
           _clamp((chasePct - 20) / 30, 0, 1) * 20 +
           _clamp(total / 100, 0, 1) * 15
    ), 28, 98);

    const metrics = {
      stuffPlus, cswPct, whiffPct, kPct, bbPct, hardHitPct,
      tunnelingScore, seqScore, avgVelo, avgSpin, ivb, hBreak,
      chasePct, zonePct,
    };
    metrics.score = _calcScore(metrics);

    // ── pitchData ────────────────────────────────────────────────────────────
    const typeOrder = {}, typeList = [];
    for (const p of pitches) {
      const { n, c } = _pitchType(_f(p, 'taggedPitchType', 'TaggedPitchType'));
      if (!(n in typeOrder)) { typeOrder[n] = typeList.length; typeList.push({ n, c }); }
    }
    const pitchRows = pitches.map(p => {
      const { n, c } = _pitchType(_f(p, 'taggedPitchType', 'TaggedPitchType'));
      const res = call(p) === 'StrikeSwinging' ? 'w' : call(p) === 'StrikeCalled' ? 'c' : 'h';
      return { x: _clamp(side(p), -1.4, 1.4), y: _clamp(height(p), 0.5, 4.5), ti: typeOrder[n], c, res };
    });

    // ── Role heuristic ───────────────────────────────────────────────────────
    const role = total > 60 ? 'SP' : total > 25 ? 'RP' : 'RP';

    pitchers.push({
      id: id++,
      name,
      hand,
      role,
      metrics,
      pitchData: { pitches: pitchRows, types: typeList },
      history:     [],
      predictions: [],
    });
  }

  pitchers.sort((a, b) => b.metrics.score - a.metrics.score);
  pitchers.forEach((p, i) => { p.rank = i + 1; });
  return pitchers;
}

// ── TrackmanClient ────────────────────────────────────────────────────────────
export class TrackmanClient {
  constructor() {
    this.apiKey  = process.env.TRACKMAN_API_KEY  || '';
    this.teamId  = process.env.TRACKMAN_TEAM_ID  || '';
    this.baseUrl = (process.env.TRACKMAN_BASE_URL || STAGING_URL).replace(/\/$/, '');
  }

  /** True when both API key and team ID are present in the environment. */
  get isConfigured() {
    return Boolean(this.apiKey && this.teamId);
  }

  // ── Private ─────────────────────────────────────────────────────────────────

  async _get(path) {
    const url = `${this.baseUrl}${path}`;
    let res;
    try {
      res = await fetch(url, {
        headers: {
          Authorization:  `Bearer ${this.apiKey}`,
          Accept:         'application/json',
          'Content-Type': 'application/json',
          'X-Team-Id':    this.teamId,
        },
        signal: AbortSignal.timeout(10_000), // 10 s hard timeout
      });
    } catch (err) {
      throw new Error(`Network error reaching Trackman API: ${err.message}`);
    }

    if (!res.ok) {
      const body = await res.text().catch(() => '(no body)');
      throw new Error(`Trackman ${res.status} ${res.statusText} — ${body}`);
    }

    return res.json();
  }

  /**
   * _extractPitches(apiResponse)
   * ----------------------------
   * Locate the flat pitch array inside whatever envelope the API returns.
   * Extend this method if your API wraps data differently.
   */
  _extractPitches(apiResponse) {
    if (Array.isArray(apiResponse))                     return apiResponse;
    if (Array.isArray(apiResponse?.pitches))            return apiResponse.pitches;
    if (Array.isArray(apiResponse?.session?.pitches))   return apiResponse.session.pitches;
    if (Array.isArray(apiResponse?.data))               return apiResponse.data;
    throw new Error(
      'Unrecognised Trackman response shape — could not locate pitch array. ' +
      'Check _extractPitches() in server/trackman.js.'
    );
  }

  // ── Public API ───────────────────────────────────────────────────────────────

  /**
   * fetchSession(date)
   * ------------------
   * Retrieves all pitches logged in a single game/practice session.
   *
   * Endpoint:  GET /pitching/sessions?teamId=&date=YYYY-MM-DD
   *
   * @param  {string|Date} date  ISO date string or Date object.
   * @returns {Promise<PitcherArray>}
   */
  async fetchSession(date) {
    const iso = date instanceof Date
      ? date.toISOString().slice(0, 10)
      : String(date).slice(0, 10);

    const raw = await this._get(
      `/pitching/sessions?teamId=${encodeURIComponent(this.teamId)}&date=${iso}`
    );
    return normalizePitches(this._extractPitches(raw));
  }

  /**
   * fetchLive()
   * -----------
   * Returns the most recent pitch data for the team's active session.
   * Poll on a short interval (e.g. every 5 s) for live game updates.
   *
   * Endpoint:  GET /pitching/live?teamId=
   *
   * ⚠️  If Trackman delivers live data via webhook / push instead of polling,
   *     replace this method with a POST handler at  /api/webhook  and call
   *     normalizePitches() on the incoming payload there.
   *
   * @returns {Promise<PitcherArray>}
   */
  async fetchLive() {
    const raw = await this._get(
      `/pitching/live?teamId=${encodeURIComponent(this.teamId)}`
    );
    return normalizePitches(this._extractPitches(raw));
  }
}
