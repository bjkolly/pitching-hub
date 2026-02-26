/**
 * appm.js — Adaptive Pitcher Performance Model (pure JavaScript)
 *
 * A full port of analytics/score.py so the server runs on Vercel
 * (and any other environment) without a Python dependency.
 *
 * Public API
 * ----------
 *   enrichPitchers(pitchers)
 *     → { pitchers, datasetMean }
 *
 * Steps performed:
 *   1. Normalise Stuff+ → mean=100, σ≈15
 *   2. Recalculate composite scores
 *   3. Ensure history exists (generate synthetic if missing)
 *   4. Run APPM per pitcher → predictions + confidence bands + metadata
 *   5. Re-sort by score, re-assign ranks
 */

// ── Hyper-parameters ──────────────────────────────────────────────────────────
const ALPHA        = 0.35;        // EWMA decay
const DAMP         = 0.60;        // OLS trend damping per step
const RTM          = 0.08;        // regression-to-mean pull per step
const Z90          = 1.645;       // 90% confidence z-score
const CI_EXP       = 0.55;        // CI width ∝ horizon^CI_EXP
const VELO_ADJ_MAX = 4.0;         // max velocity modifier (pts)
const VELO_MID     = (84 + 101) / 2;   // 92.5 mph
const SIGMA_FLOOR  = 2.0;         // minimum residual σ
const TREND_NORM   = 5.0;         // slope that maps trendStrength → 1.0

// ── Helpers ───────────────────────────────────────────────────────────────────
const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
const norm  = (v, a, b)   => clamp((v - a) / (b - a), 0, 1);

// ── Composite score (exact mirror of UI calcScore) ────────────────────────────
function calcScore(m) {
  return Math.round(clamp(
      norm(m.stuffPlus,      60,  140) * 22
    + norm(m.cswPct,         17,   42) * 18
    + norm(m.whiffPct,       11,   40) * 16
    + norm(m.tunnelingScore, 28,   99) * 10
    + (norm(m.kPct,          11,   38) - norm(m.bbPct, 2.5, 16)) * 0.5 * 12
    + (1 - norm(m.hardHitPct,20,   52)) * 7
    + norm(m.seqScore,       28,   98) * 6
    + norm(m.chasePct,       20,   50) * 5
    + norm(m.avgVelo,        84,  101) * 2
    + norm(m.avgSpin,      1850, 2900) * 2,
    18, 99
  ));
}

// ── Stuff+ normalisation ──────────────────────────────────────────────────────
function normalizeStuffPlus(pitchers) {
  const vals = pitchers.map(p => p.metrics.stuffPlus);
  const mean = vals.reduce((a, b) => a + b, 0) / vals.length;
  const std  = Math.sqrt(vals.reduce((a, v) => a + (v - mean) ** 2, 0) / vals.length) || 1;
  for (const p of pitchers) {
    const raw = p.metrics.stuffPlus;
    p.metrics.stuffPlusRaw = raw;
    p.metrics.stuffPlus    = Math.round(clamp(100 + (raw - mean) / std * 15, 55, 148));
  }
}

// ── OLS linear regression ─────────────────────────────────────────────────────
function ols(data) {
  const n = data.length;
  if (n < 2) return [0, data[0] ?? 50];
  const xm  = (n - 1) / 2;
  const ym  = data.reduce((a, b) => a + b, 0) / n;
  const num = data.reduce((s, v, i) => s + (i - xm) * (v - ym), 0);
  const den = Array.from({ length: n }, (_, i) => (i - xm) ** 2).reduce((a, b) => a + b, 0) || 1;
  const slope = num / den;
  return [slope, ym - slope * xm];
}

// ── EWMA (oldest→newest, returns final value) ─────────────────────────────────
function ewmaLast(data, alpha = ALPHA) {
  let val = data[0];
  for (let i = 1; i < data.length; i++) val = alpha * data[i] + (1 - alpha) * val;
  return val;
}

// ── Synthetic history (mean-reverting random walk) ────────────────────────────
function genHistory(score, n = 10) {
  const history = [];
  let prev = score;
  for (let i = 0; i < n; i++) {
    const s = Math.round(clamp(
      prev * 0.6 + score * 0.3 + (Math.random() * 20 - 10) * 0.5,
      18, 99
    ));
    history.push(s);
    prev = s;
  }
  return history;
}

// ── APPM core ─────────────────────────────────────────────────────────────────
function genAdvancedPredictions(pitcher, leagueMean, n = 3) {
  const history = pitcher.history || [];
  const m       = pitcher.metrics;

  if (history.length < 2) {
    const base  = history[0] ?? m.score;
    const bands = Array.from({ length: n }, () => ({
      value: base,
      lower: Math.max(18, base - 8),
      upper: Math.min(99, base + 8),
    }));
    return {
      predictions: bands.map(b => b.value),
      predBands:   bands,
      formStatus:  'neutral',
      formDelta:   0,
      trendStrength: 0,
      riskFlag:    null,
      ceiling:     bands.at(-1).upper,
      floor:       bands.at(-1).lower,
    };
  }

  // ── Base signals ──────────────────────────────────────────────────────
  const ewmaBase           = ewmaLast(history);
  const [slope, intercept] = ols(history);
  const residuals          = history.map((v, i) => v - (slope * i + intercept));
  const sigma              = Math.max(
    Math.sqrt(residuals.reduce((a, r) => a + r * r, 0) / residuals.length),
    SIGMA_FLOOR
  );

  // ── Velocity modifier ─────────────────────────────────────────────────
  const veloAdj = clamp(
    (m.avgVelo - VELO_MID) / (101 - VELO_MID) * VELO_ADJ_MAX,
    -VELO_ADJ_MAX, VELO_ADJ_MAX
  );

  // ── Form detection ────────────────────────────────────────────────────
  const recent3    = history.slice(-3);
  const overallAvg = history.reduce((a, b) => a + b, 0) / history.length;
  const recentAvg  = recent3.reduce((a, b) => a + b, 0) / recent3.length;
  const formDelta  = Math.round((recentAvg - overallAvg) * 10) / 10;
  const formStatus = formDelta >= 4 ? 'hot' : formDelta <= -4 ? 'cold' : 'neutral';

  // ── Trend strength ────────────────────────────────────────────────────
  const trendStrength = Math.round(clamp(Math.abs(slope) / TREND_NORM, 0, 1) * 100) / 100;

  // ── Generate n forecast steps ─────────────────────────────────────────
  const predBands = [];
  for (let step = 1; step <= n; step++) {
    let point = ewmaBase + slope * (DAMP ** step) * step;
    // Regression-to-mean (capped at 35%)
    const rtmWeight = Math.min(RTM * step, 0.35);
    point = point * (1 - rtmWeight) + leagueMean * rtmWeight;
    // Velocity modifier (diminishes with horizon)
    point += veloAdj * (DAMP ** step);
    point  = clamp(point, 18, 99);
    // 90% CI
    const ciHalf = Z90 * sigma * step ** CI_EXP;
    predBands.push({
      value: Math.round(point),
      lower: Math.round(clamp(point - ciHalf, 18, 99)),
      upper: Math.round(clamp(point + ciHalf, 18, 99)),
    });
  }

  // ── Risk flag ─────────────────────────────────────────────────────────
  let riskFlag = null;
  if (slope < -1.5)                              riskFlag = 'declining';
  else if (formStatus === 'cold' && m.score < 50) riskFlag = 'poor_form';
  else if (m.avgVelo < 88)                        riskFlag = 'low_velo';

  return {
    predictions:   predBands.map(b => b.value),
    predBands,
    formStatus,
    formDelta,
    trendStrength,
    riskFlag,
    ceiling: Math.max(...predBands.map(b => b.upper)),
    floor:   Math.min(...predBands.map(b => b.lower)),
  };
}

// ── Public API ────────────────────────────────────────────────────────────────

/**
 * Enrich a pitcher array with APPM predictions and normalised Stuff+.
 * Modifies the array in-place and returns { pitchers, datasetMean }.
 */
export function enrichPitchers(pitchers) {
  if (!pitchers || !pitchers.length) return { pitchers: [], datasetMean: 50 };

  // 1. Normalise Stuff+
  normalizeStuffPlus(pitchers);

  // 2. Recalculate composite scores; generate history if absent
  for (const p of pitchers) {
    p.metrics.score = calcScore(p.metrics);
    if (!p.history?.length) p.history = genHistory(p.metrics.score);
  }

  // 3. Dataset mean for RTM shrinkage
  const datasetMean = pitchers.reduce((a, p) => a + p.metrics.score, 0) / pitchers.length;

  // 4. APPM per pitcher
  for (const p of pitchers) {
    Object.assign(p, genAdvancedPredictions(p, datasetMean));
  }

  // 5. Re-sort and re-rank
  pitchers.sort((a, b) => b.metrics.score - a.metrics.score);
  pitchers.forEach((p, i) => { p.rank = i + 1; });

  return { pitchers, datasetMean };
}
