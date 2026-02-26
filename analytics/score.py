#!/usr/bin/env python3
"""
score.py — Enriches /data/session.json in place.

Usage
-----
    python3 score.py <path_to_session.json>

Pipeline
--------
1. Load pitcher array from session.json
2. Normalise Stuff+ so the dataset mean = 100, σ ≈ 15  (same feel as ERA+/wRC+)
3. Recalculate each pitcher's composite score — exact port of calcScore() from the UI
4. If a pitcher has no history yet, generate a synthetic 10-start rolling history
5. Run APPM (Adaptive Pitcher Performance Model) on history → 3-start predictions
   with confidence bands, form status, risk flags, ceiling/floor
6. Re-sort by score descending and re-assign ranks
7. Write the enriched array back to session.json in place
8. Print a one-line JSON summary to stdout (consumed by the Express endpoint)

APPM Algorithm
--------------
Base prediction combines three signals, then applies modifiers:
  • EWMA base (α=0.35) — weights recent starts more heavily
  • Damped OLS trend  (γ=0.60 per step) — prevents runaway extrapolation
  • Regression-to-mean (λ=0.08 per step) — shrinks toward dataset mean

Modifiers:
  • Velocity modifier — avg velo 84–101 mph maps to ±4 pts (diminishes with horizon)

Uncertainty:
  • σ estimated from OLS residuals
  • 90% CI: ±1.645 × σ × horizon^0.55  (sub-linear growth)

Metadata output per pitcher:
  predBands   — list of {value, lower, upper} per forecast step
  formStatus  — 'hot' | 'cold' | 'neutral'  (last-3 vs overall avg)
  formDelta   — float, recent_avg - overall_avg
  trendStrength — 0–1, normalised |slope|
  riskFlag    — None | 'declining' | 'poor_form' | 'low_velo'
  ceiling     — highest upper bound across forecast horizon
  floor       — lowest lower bound across forecast horizon
"""

import sys
import json
import math
import random


# ── Hyper-parameters ───────────────────────────────────────────────────────────

ALPHA   = 0.35   # EWMA decay — heavier weight on recent starts
DAMP    = 0.60   # OLS-trend damping factor per forecast step
RTM     = 0.08   # regression-to-mean pull per step (toward dataset mean)
Z90     = 1.645  # z-score for 90% confidence interval
CI_EXP  = 0.55   # CI half-width ∝ horizon^CI_EXP (sub-linear growth)
VELO_ADJ_MAX = 4.0   # max pts adjustment from velocity
VELO_MID     = (84 + 101) / 2   # midpoint of velo range (92.5 mph)
SIGMA_FLOOR  = 2.0   # minimum σ to avoid zero-width bands
SLOPE_HOT    = -4.0  # form delta threshold: below → cold
SLOPE_COLD   =  4.0  # form delta threshold: above → hot
TREND_NORM   =  5.0  # slope magnitude that maps to trendStrength=1.0


# ── Helpers ────────────────────────────────────────────────────────────────────

def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def norm(v, a, b):
    """Map v linearly from [a, b] → [0, 1], clamped at both ends."""
    return clamp((v - a) / (b - a), 0.0, 1.0)


# ── Scoring ────────────────────────────────────────────────────────────────────

def calc_score(m):
    """
    Exact port of the UI's calcScore() function.

    Metric weights (sum ≈ 100 pts max):
        stuffPlus       22
        cswPct          18
        whiffPct        16
        tunnelingScore  10
        kPct - bbPct    12  (combined, ×0.5)
        1 - hardHitPct   7
        seqScore         6
        chasePct         5
        avgVelo          2
        avgSpin          2
    """
    return round(clamp(
          norm(m['stuffPlus'],       60,  140) * 22
        + norm(m['cswPct'],          17,   42) * 18
        + norm(m['whiffPct'],        11,   40) * 16
        + norm(m['tunnelingScore'],  28,   99) * 10
        + (norm(m['kPct'],           11,   38)
           - norm(m['bbPct'],       2.5,   16)) * 0.5 * 12
        + (1 - norm(m['hardHitPct'], 20,   52)) * 7
        + norm(m['seqScore'],        28,   98) * 6
        + norm(m['chasePct'],        20,   50) * 5
        + norm(m['avgVelo'],         84,  101) * 2
        + norm(m['avgSpin'],       1850, 2900) * 2,
        18, 99
    ))


# ── Stuff+ normalisation ───────────────────────────────────────────────────────

def normalize_stuff_plus(pitchers):
    """
    Normalise raw stuffPlus values so the league mean = 100 and σ ≈ 15.
    Modifies pitchers in-place and stashes the raw value as stuffPlusRaw.
    """
    vals = [p['metrics']['stuffPlus'] for p in pitchers]
    mean = sum(vals) / len(vals)
    std  = math.sqrt(sum((v - mean) ** 2 for v in vals) / len(vals)) or 1.0

    for p in pitchers:
        raw = p['metrics']['stuffPlus']
        p['metrics']['stuffPlusRaw'] = raw                              # preserve original
        p['metrics']['stuffPlus']    = round(clamp(
            100 + (raw - mean) / std * 15, 55, 148
        ))


# ── Linear regression ──────────────────────────────────────────────────────────

def ols(data):
    """
    Simple OLS on the sequence data, treating index as the x-variable.

    Returns (slope, intercept) so that  ŷ(x) = slope·x + intercept.
    """
    n = len(data)
    if n < 2:
        return 0.0, float(data[0]) if data else 50.0

    xm = (n - 1) / 2.0
    ym = sum(data) / n
    num = sum((i - xm) * (v - ym) for i, v in enumerate(data))
    den = sum((i - xm) ** 2 for i in range(n)) or 1.0

    slope     = num / den
    intercept = ym - slope * xm
    return slope, intercept


# ── History generation ─────────────────────────────────────────────────────────

def gen_history(score, n=10):
    """
    Build a synthetic n-start rolling history centred on `score`.
    Uses the same mean-reverting random-walk as the original client-side genH().
    """
    history, prev = [], score
    for _ in range(n):
        s = round(clamp(
            prev * 0.6 + score * 0.3 + random.uniform(-10, 10) * 0.5,
            18, 99
        ))
        history.append(s)
        prev = s
    return history


# ── APPM — Adaptive Pitcher Performance Model ──────────────────────────────────

def ewma_last(data, alpha=ALPHA):
    """
    Exponentially weighted moving average over `data` (oldest → newest).
    Returns the final EWMA value.
    """
    val = float(data[0])
    for v in data[1:]:
        val = alpha * v + (1 - alpha) * val
    return val


def gen_advanced_predictions(pitcher, league_mean, n=3):
    """
    APPM: Adaptive Pitcher Performance Model.

    Blends EWMA base, damped OLS trend, and regression-to-mean into each
    forecast step, then overlays a velocity modifier and computes 90% CI bands.

    Parameters
    ----------
    pitcher     : pitcher dict (must contain 'history' and 'metrics')
    league_mean : float, dataset-average composite score (for RTM shrinkage)
    n           : int, number of forecast steps (default 3)

    Returns
    -------
    dict with keys:
        predictions   list[int]   — point forecast values (backward-compatible)
        predBands     list[dict]  — [{value, lower, upper}, …]
        formStatus    str         — 'hot' | 'cold' | 'neutral'
        formDelta     float       — recent_avg − overall_avg
        trendStrength float       — 0–1, normalised |OLS slope|
        riskFlag      str | None  — 'declining' | 'poor_form' | 'low_velo' | None
        ceiling       int         — max upper bound across all forecast steps
        floor         int         — min lower bound across all forecast steps
    """
    history = pitcher.get('history') or []
    m       = pitcher['metrics']

    # ── Degenerate case: no usable history ────────────────────────────────
    if len(history) < 2:
        base = int(history[0]) if history else m['score']
        bands = [
            {'value': base, 'lower': max(18, base - 8), 'upper': min(99, base + 8)}
            for _ in range(n)
        ]
        return {
            'predictions':   [b['value'] for b in bands],
            'predBands':     bands,
            'formStatus':    'neutral',
            'formDelta':     0.0,
            'trendStrength': 0.0,
            'riskFlag':      None,
            'ceiling':       bands[-1]['upper'],
            'floor':         bands[-1]['lower'],
        }

    # ── EWMA base ──────────────────────────────────────────────────────────
    ewma_base = ewma_last(history)

    # ── OLS slope + σ from residuals ──────────────────────────────────────
    slope, intercept = ols(history)
    residuals = [history[i] - (slope * i + intercept) for i in range(len(history))]
    sigma = math.sqrt(sum(r ** 2 for r in residuals) / len(residuals))
    sigma = max(sigma, SIGMA_FLOOR)

    # ── Velocity modifier ─────────────────────────────────────────────────
    # Maps velo linearly: midpoint(92.5) → 0, 101 → +VELO_ADJ_MAX, 84 → -VELO_ADJ_MAX
    velo_adj = clamp(
        (m['avgVelo'] - VELO_MID) / (101 - VELO_MID) * VELO_ADJ_MAX,
        -VELO_ADJ_MAX, VELO_ADJ_MAX
    )

    # ── Form detection ────────────────────────────────────────────────────
    recent_3    = history[-3:] if len(history) >= 3 else history
    overall_avg = sum(history) / len(history)
    recent_avg  = sum(recent_3) / len(recent_3)
    form_delta  = round(recent_avg - overall_avg, 1)

    if form_delta >= SLOPE_COLD:
        form_status = 'hot'
    elif form_delta <= SLOPE_HOT:
        form_status = 'cold'
    else:
        form_status = 'neutral'

    # ── Trend strength (0–1) ──────────────────────────────────────────────
    trend_strength = round(clamp(abs(slope) / TREND_NORM, 0.0, 1.0), 2)

    # ── Generate n forecast steps ─────────────────────────────────────────
    pred_bands = []
    for step in range(1, n + 1):
        # 1. EWMA base + damped OLS trend
        damped_slope = slope * (DAMP ** step)
        point = ewma_base + damped_slope * step

        # 2. Regression-to-mean shrinkage (grows with horizon)
        rtm_weight = min(RTM * step, 0.35)   # cap at 35% pull
        point = point * (1.0 - rtm_weight) + league_mean * rtm_weight

        # 3. Velocity modifier (diminishes with horizon via damping)
        point += velo_adj * (DAMP ** step)

        # 4. Clamp to valid score range
        point = clamp(point, 18.0, 99.0)

        # 5. Confidence interval (90%, sub-linear width growth)
        ci_half = Z90 * sigma * (step ** CI_EXP)
        lower   = int(round(clamp(point - ci_half, 18, 99)))
        upper   = int(round(clamp(point + ci_half, 18, 99)))
        value   = int(round(point))

        pred_bands.append({'value': value, 'lower': lower, 'upper': upper})

    # ── Risk flag ─────────────────────────────────────────────────────────
    risk_flag = None
    if slope < -1.5:
        risk_flag = 'declining'
    elif form_status == 'cold' and m['score'] < 50:
        risk_flag = 'poor_form'
    elif m['avgVelo'] < 88.0:
        risk_flag = 'low_velo'

    # ceiling/floor span the full horizon
    ceiling = max(b['upper'] for b in pred_bands)
    floor   = min(b['lower'] for b in pred_bands)

    return {
        'predictions':   [b['value'] for b in pred_bands],
        'predBands':     pred_bands,
        'formStatus':    form_status,
        'formDelta':     form_delta,
        'trendStrength': trend_strength,
        'riskFlag':      risk_flag,
        'ceiling':       ceiling,
        'floor':         floor,
    }


# ── Entry point ────────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 2:
        print(json.dumps({'error': 'Usage: score.py <path_to_session.json>'}))
        sys.exit(1)

    path = sys.argv[1]

    # ── Load ──────────────────────────────────────────────────────────────
    try:
        with open(path, 'r') as f:
            pitchers = json.load(f)
    except FileNotFoundError:
        print(json.dumps({'error': f'File not found: {path}'}))
        sys.exit(1)
    except json.JSONDecodeError as exc:
        print(json.dumps({'error': f'Invalid JSON: {exc}'}))
        sys.exit(1)

    if not pitchers:
        print(json.dumps({'error': 'Dataset is empty'}))
        sys.exit(1)

    # ── Step 1 — Normalise Stuff+ ─────────────────────────────────────────
    normalize_stuff_plus(pitchers)

    for p in pitchers:
        m = p['metrics']

        # ── Step 2 — Recalculate composite score ──────────────────────────
        m['score'] = calc_score(m)

        # ── Step 3 — Synthetic history (only when missing) ────────────────
        if not p.get('history'):
            p['history'] = gen_history(m['score'])

    # ── Step 4 — Compute dataset mean for RTM ────────────────────────────
    #   (use updated scores from step 2)
    dataset_mean = sum(p['metrics']['score'] for p in pitchers) / len(pitchers)

    # ── Step 5 — APPM predictions ─────────────────────────────────────────
    for p in pitchers:
        appm = gen_advanced_predictions(p, dataset_mean)

        # Flatten APPM output onto pitcher object (backward-compatible)
        p['predictions']   = appm['predictions']
        p['predBands']     = appm['predBands']
        p['formStatus']    = appm['formStatus']
        p['formDelta']     = appm['formDelta']
        p['trendStrength'] = appm['trendStrength']
        p['riskFlag']      = appm['riskFlag']
        p['ceiling']       = appm['ceiling']
        p['floor']         = appm['floor']

    # ── Step 6 — Re-sort and re-rank ─────────────────────────────────────
    pitchers.sort(key=lambda p: p['metrics']['score'], reverse=True)
    for i, p in enumerate(pitchers):
        p['rank'] = i + 1

    # ── Step 7 — Write enriched JSON back to session.json ─────────────────
    try:
        with open(path, 'w') as f:
            json.dump(pitchers, f, indent=2)
    except OSError as exc:
        print(json.dumps({'error': f'Could not write file: {exc}'}))
        sys.exit(1)

    # ── Step 8 — Summary to stdout ────────────────────────────────────────
    n         = len(pitchers)
    avg_score = round(sum(p['metrics']['score'] for p in pitchers) / n, 1)
    avg_sp    = round(sum(p['metrics']['stuffPlus'] for p in pitchers) / n, 1)
    hot_count = sum(1 for p in pitchers if p.get('formStatus') == 'hot')
    cold_count = sum(1 for p in pitchers if p.get('formStatus') == 'cold')
    flagged   = sum(1 for p in pitchers if p.get('riskFlag'))

    print(json.dumps({
        'status':      'ok',
        'pitchers':    n,
        'avg_score':   avg_score,
        'avg_stuff':   avg_sp,       # should be ~100 after normalisation
        'dataset_mean': round(dataset_mean, 1),
        'hot':         hot_count,
        'cold':        cold_count,
        'flagged':     flagged,
    }))


if __name__ == '__main__':
    main()
