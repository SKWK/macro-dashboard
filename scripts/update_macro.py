"""
update_macro.py
===============
Advanced FRED data fetcher for the Macro Early Warning Dashboard.

Steps:
  1. Fetch 10+ years of data per series (for z-score normalization)
  2. Compute: latest value, period-over-period change, 3-month trend, z-score
  3. Run a logistic recession probability model
  4. Classify macro regime with multi-factor rules
  5. Generate tradable signals per regime
  6. Write public/macro.json (including chart history arrays)

Usage:
    FRED_API_KEY=<key> python scripts/update_macro.py
"""

import json
import math
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
from fredapi import Fred


# ---------------------------------------------------------------------------
# Series configuration
# ---------------------------------------------------------------------------

SERIES_CONFIG = {
    # Labor Market
    "ICSA":          {"label": "Initial Jobless Claims",          "group": "labor",     "unit": "persons", "higher_is_bad": True},
    "CCSA":          {"label": "Continuing Jobless Claims",       "group": "labor",     "unit": "persons", "higher_is_bad": True},
    "AWHAETP":       {"label": "Avg Weekly Hours (Private)",      "group": "labor",     "unit": "hours",   "higher_is_bad": False},
    # Credit Stress
    "DRCCLACBS":     {"label": "Credit Card Delinquency Rate",    "group": "credit",    "unit": "%",       "higher_is_bad": True},
    "DRCLACBS":      {"label": "Consumer Loan Delinquency Rate",  "group": "credit",    "unit": "%",       "higher_is_bad": True},
    "REVOLSL":       {"label": "Revolving Consumer Credit",       "group": "credit",    "unit": "$M",      "higher_is_bad": True},
    # Financial Conditions
    "BAMLH0A0HYM2":  {"label": "HY Option-Adjusted Spread",       "group": "financial", "unit": "bps",     "higher_is_bad": True, "scale": 100},
    "NFCI":          {"label": "Natl Financial Conditions Index", "group": "financial", "unit": "",        "higher_is_bad": True},
    # Bank Lending
    "DRTSCLCC":      {"label": "Tightening Stds – Credit Cards",  "group": "lending",   "unit": "% net",   "higher_is_bad": True},
    # Interest Rates
    "DGS2":          {"label": "2-Year Treasury Yield",           "group": "rates",     "unit": "%",       "higher_is_bad": None},
    "DGS10":         {"label": "10-Year Treasury Yield",          "group": "rates",     "unit": "%",       "higher_is_bad": None},
    "T10Y2Y":        {"label": "10Y–2Y Yield Curve Spread",       "group": "rates",     "unit": "%",       "higher_is_bad": False},
    # Liquidity
    "WALCL":         {"label": "Fed Balance Sheet",               "group": "liquidity", "unit": "$B",      "higher_is_bad": False, "scale": 0.001},
    "RRPONTSYD":     {"label": "Overnight Reverse Repo (RRP)",    "group": "liquidity", "unit": "$B",      "higher_is_bad": True},
    "WTREGEN":       {"label": "Treasury General Account (TGA)",  "group": "liquidity", "unit": "$B",      "higher_is_bad": True,  "scale": 0.001},
}

# Logistic model: positive weight → higher z-score raises recession risk
MODEL_WEIGHTS = {
    "ICSA":         +0.30,   # rising claims = more risk
    "T10Y2Y":       -0.25,   # inverted curve = more risk (negate z)
    "BAMLH0A0HYM2": +0.20,   # wider spreads = more risk
    "NFCI":         +0.15,   # tighter conditions = more risk
    "AWHAETP":      -0.10,   # falling hours = more risk (negate z)
}
# logistic(MODEL_INTERCEPT) ≈ 18% base rate when all z-scores = 0
MODEL_INTERCEPT = -1.5

# How many weeks of history to store for charts (≈2 years)
CHART_WEEKS = 104

# How far back to fetch for z-score normalization (10.5 years)
ZSCORE_DAYS = 365 * 10 + 180

# Indicators monitored by the Regime Shift Engine (subset of SERIES_CONFIG)
SHIFT_INDICATORS = {
    "T10Y2Y":       {"label": "Yield curve",          "higher_is_bad": False},
    "ICSA":         {"label": "Jobless claims",        "higher_is_bad": True},
    "BAMLH0A0HYM2": {"label": "HY credit spreads",    "higher_is_bad": True},
    "NFCI":         {"label": "Financial conditions",  "higher_is_bad": True},
    "AWHAETP":      {"label": "Avg weekly hours",      "higher_is_bad": False},
}

SHIFT_ALERT_MESSAGES = {
    "T10Y2Y":       "Yield curve flattening rapidly",
    "ICSA":         "Jobless claims rising sharply",
    "BAMLH0A0HYM2": "Credit spreads widening",
    "NFCI":         "Financial conditions tightening",
    "AWHAETP":      "Avg weekly hours declining",
}


# ---------------------------------------------------------------------------
# Utility functions
# ---------------------------------------------------------------------------

def get_api_key() -> str:
    key = os.environ.get("FRED_API_KEY", "").strip()
    if not key:
        print("ERROR: FRED_API_KEY environment variable is not set.", file=sys.stderr)
        sys.exit(1)
    return key


def logistic(x: float) -> float:
    try:
        return 1.0 / (1.0 + math.exp(-x))
    except OverflowError:
        return 0.0 if x < 0 else 1.0


def fetch_series(fred: Fred, series_id: str, start: datetime) -> pd.Series:
    try:
        data = fred.get_series(series_id, observation_start=start.strftime("%Y-%m-%d"))
        if data is None or data.empty:
            print(f"  WARNING: no data returned for {series_id}", file=sys.stderr)
            return pd.Series(dtype=float)
        return data.dropna().sort_index().astype(float)
    except Exception as exc:
        print(f"  WARNING: failed to fetch {series_id}: {exc}", file=sys.stderr)
        return pd.Series(dtype=float)


def resample_to_weekly(series: pd.Series) -> pd.Series:
    """Resample any frequency to weekly (last non-null observation per week)."""
    if series.empty:
        return series
    return series.resample("W").last().dropna()


def compute_z_score(full_series: pd.Series, current_value: float) -> float:
    """Z-score of current_value relative to the entire history of full_series."""
    if len(full_series) < 20:
        return 0.0
    mean = float(full_series.mean())
    std = float(full_series.std())
    if std < 1e-9:
        return 0.0
    return round(float((current_value - mean) / std), 3)


def compute_trend(series_id: str, weekly_series: pd.Series) -> str:
    """
    3-month trend: compare average of last 4 observations to average of
    4 observations from ~3 months earlier.
    Returns 'improving', 'deteriorating', or 'neutral'.
    """
    if len(weekly_series) < 8:
        return "neutral"

    recent_avg = float(weekly_series.iloc[-4:].mean())

    cutoff = weekly_series.index[-1] - pd.DateOffset(months=3)
    older_slice = weekly_series[weekly_series.index <= cutoff]
    if len(older_slice) < 4:
        older_slice = weekly_series.iloc[:4]
    older_avg = float(older_slice.iloc[-4:].mean())

    if abs(older_avg) < 1e-9:
        return "neutral"

    pct_change = (recent_avg - older_avg) / abs(older_avg)
    THRESHOLD = 0.015  # 1.5 % minimum move to call a trend

    if abs(pct_change) < THRESHOLD:
        return "neutral"

    cfg = SERIES_CONFIG.get(series_id, {})
    higher_is_bad = cfg.get("higher_is_bad", None)
    if higher_is_bad is None:
        return "neutral"

    # rising value: deteriorating if higher_is_bad, else improving
    if pct_change > 0:
        return "deteriorating" if higher_is_bad else "improving"
    else:
        return "improving" if higher_is_bad else "deteriorating"


# ---------------------------------------------------------------------------
# Macro regime classification
# ---------------------------------------------------------------------------

def classify_regime(
    z_scores: dict[str, float],
    latest: dict[str, float],
) -> tuple[str, int, list[str]]:
    """
    Score-based regime classifier.
    Returns (regime_name, risk_score, list_of_triggered_signals).
    """
    risk = 0
    details: list[str] = []

    # --- Labor market ---
    icsa = latest.get("ICSA", 0)
    if icsa > 280_000:
        risk += 1
        details.append(f"Initial claims above 280k ({icsa:,.0f})")
    if z_scores.get("ICSA", 0) > 1.0:
        risk += 1
        details.append(f"Claims elevated vs history (z={z_scores['ICSA']:.2f})")
    if z_scores.get("AWHAETP", 0) < -1.0:
        risk += 1
        details.append(f"Avg hours below trend (z={z_scores['AWHAETP']:.2f})")

    # --- Financial conditions ---
    hy = latest.get("BAMLH0A0HYM2", 0)
    if hy > 500:
        risk += 2
        details.append(f"HY spread above 500bps ({hy:.0f}bps)")
    elif hy > 400:
        risk += 1
        details.append(f"HY spread elevated above 400bps ({hy:.0f}bps)")

    nfci = latest.get("NFCI", 0)
    if nfci > 0.5:
        risk += 1
        details.append(f"Financial conditions tightening (NFCI={nfci:.2f})")

    # --- Yield curve ---
    t10y2y = latest.get("T10Y2Y", 0)
    if t10y2y < 0:
        risk += 2
        details.append(f"Yield curve inverted ({t10y2y:.2f}%)")
    elif t10y2y < 0.5:
        risk += 1
        details.append(f"Yield curve flat ({t10y2y:.2f}%, <50bps)")

    # --- Credit ---
    cc_dq = latest.get("DRCCLACBS", 0)
    if cc_dq > 3.0:
        risk += 1
        details.append(f"Credit card delinquency elevated ({cc_dq:.2f}%)")

    if z_scores.get("DRTSCLCC", 0) > 1.0:
        risk += 1
        details.append(f"Lending standards tightening (z={z_scores['DRTSCLCC']:.2f})")

    # --- Map risk score to regime ---
    if risk <= 2:
        regime = "Expansion"
    elif risk <= 4:
        regime = "Slowdown"
    elif risk <= 6:
        regime = "Late Cycle"
    else:
        regime = "Recession Risk"

    return regime, risk, details


# ---------------------------------------------------------------------------
# Recession probability: logistic model
# ---------------------------------------------------------------------------

def compute_recession_probability(z_scores: dict[str, float]) -> float:
    """
    Simple logistic model.  Inputs are z-scores of key macro series.
    Output: recession probability in [1, 99] percent.
    """
    linear = sum(
        weight * z_scores.get(sid, 0.0)
        for sid, weight in MODEL_WEIGHTS.items()
    )
    prob = logistic(linear + MODEL_INTERCEPT) * 100
    return round(max(1.0, min(99.0, prob)), 1)


# ---------------------------------------------------------------------------
# Credit stress level
# ---------------------------------------------------------------------------

def compute_credit_stress(z_scores: dict, latest: dict) -> str:
    pts = 0
    if z_scores.get("DRCCLACBS", 0) > 1.0:
        pts += 1
    if z_scores.get("DRCLACBS", 0) > 1.0:
        pts += 1
    if latest.get("BAMLH0A0HYM2", 0) > 400:
        pts += 1
    if z_scores.get("DRTSCLCC", 0) > 0.5:
        pts += 1
    if pts == 0:
        return "Low"
    if pts <= 2:
        return "Medium"
    return "High"


# ---------------------------------------------------------------------------
# Liquidity index
# ---------------------------------------------------------------------------

def compute_liquidity(z_scores: dict) -> tuple[float, str]:
    """
    Composite liquidity index from Fed balance sheet, reverse repo, and TGA.
    Fed QE (WALCL up)  → looser;  RRP & TGA drain reserves → tighter.
    Returns (liquidity_index, regime).
    """
    walcl_z = z_scores.get("WALCL", 0.0)
    rrp_z   = z_scores.get("RRPONTSYD", 0.0)
    tga_z   = z_scores.get("WTREGEN", 0.0)

    # Positive index = loose, negative = tight
    index = round(walcl_z - 0.5 * rrp_z - 0.5 * tga_z, 3)

    if index > 0.5:
        regime = "Loose"
    elif index < -0.5:
        regime = "Tight"
    else:
        regime = "Neutral"

    return index, regime


# ---------------------------------------------------------------------------
# Credit Impulse
# ---------------------------------------------------------------------------

def compute_credit_impulse(raw: dict) -> tuple[dict, dict]:
    """
    Credit impulse = 3-month credit growth minus 12-month credit growth (in $B).
    Uses REVOLSL (Revolving Consumer Credit, monthly, returned by FRED in $M).
    Calendar-based lookback ensures correct dates regardless of series frequency.

    Thresholds ($B):  > 0 = Positive,  >= -20 = Neutral,  < -20 = Negative
    Returns (top_level_dict, indicator_dict).
    """
    revolsl = raw.get("REVOLSL")

    _empty_top = {"value": None, "classification": "Unknown", "description": "Insufficient data."}
    _empty_ind = {
        "series_id": "CREDIT_IMPULSE", "label": "Credit Impulse",
        "group": "credit", "unit": "$B", "higher_is_bad": False,
        "latest_value": None, "prev_value": None, "period_change": None,
        "trend_3m": "neutral", "z_score": 0.0,
        "history_dates": [], "history_values": [],
    }

    if revolsl is None or len(revolsl) < 13:
        return _empty_top, _empty_ind

    # Build impulse series using calendar-based lookback
    vals, idx = [], []
    for i in range(len(revolsl)):
        dt       = revolsl.index[i]
        past_3m  = revolsl[revolsl.index <= (dt - pd.DateOffset(months=3))]
        past_12m = revolsl[revolsl.index <= (dt - pd.DateOffset(months=12))]
        if past_3m.empty or past_12m.empty:
            continue
        g3m  = float(revolsl.iloc[i] - past_3m.iloc[-1])
        g12m = float(revolsl.iloc[i] - past_12m.iloc[-1])
        vals.append((g3m - g12m) / 1000.0)   # millions → billions
        idx.append(dt)

    if not vals:
        return _empty_top, _empty_ind

    impulse = pd.Series(vals, index=idx)
    current = round(float(impulse.iloc[-1]), 3)
    prev    = round(float(impulse.iloc[-2]), 3) if len(impulse) >= 2 else None
    chg     = round(current - prev, 3)           if prev is not None  else None
    z       = compute_z_score(impulse, current)

    # Trend: positive impulse is good (higher = improving)
    trend = "neutral"
    if len(impulse) >= 8:
        recent_avg = float(impulse.iloc[-4:].mean())
        cutoff = impulse.index[-1] - pd.DateOffset(months=3)
        older_slice = impulse[impulse.index <= cutoff]
        if len(older_slice) < 4:
            older_slice = impulse.iloc[:4]
        older_avg = float(older_slice.iloc[-4:].mean())
        if abs(older_avg) >= 1e-9:
            pct_change = (recent_avg - older_avg) / abs(older_avg)
            if abs(pct_change) >= 0.015:
                trend = "improving" if pct_change > 0 else "deteriorating"

    if current > 0:
        classification = "Positive"
        description    = "Credit growth accelerating relative to last year."
    elif current >= -20.0:
        classification = "Neutral"
        description    = "Credit growth stable."
    else:
        classification = "Negative"
        description    = "Credit growth slowing relative to last year."

    chart = impulse.iloc[-CHART_WEEKS:]

    top_level = {
        "value":          current,
        "classification": classification,
        "description":    description,
    }
    indicator = {
        "series_id":      "CREDIT_IMPULSE",
        "label":          "Credit Impulse",
        "group":          "credit",
        "unit":           "$B",
        "higher_is_bad":  False,
        "latest_value":   current,
        "prev_value":     prev,
        "period_change":  chg,
        "trend_3m":       trend,
        "z_score":        z,
        "history_dates":  [d.strftime("%Y-%m-%d") for d in chart.index],
        "history_values": [round(float(v), 3) for v in chart.values],
    }
    return top_level, indicator


# ---------------------------------------------------------------------------
# Global Liquidity Index
# ---------------------------------------------------------------------------

def compute_global_liquidity(raw: dict) -> tuple[dict, dict]:
    """
    Net Dollar Liquidity = WALCL - RRPONTSYD - WTREGEN  (all in $B after scaling).
    Rising = supportive for risk assets; Falling = tightening financial conditions.

    Classification by 3M change:
      > +$100B  → Expanding
      < -$100B  → Contracting
      else      → Neutral

    Returns (top_level_dict, indicator_dict).
    """
    walcl = raw.get("WALCL")
    rrp   = raw.get("RRPONTSYD")
    tga   = raw.get("WTREGEN")

    _empty_top = {
        "value": None, "change_1m": None, "change_3m": None,
        "classification": "Unknown", "drivers": [],
    }
    _empty_ind = {
        "series_id": "GLOBAL_LIQUIDITY", "label": "Global Liquidity Index",
        "group": "liquidity", "unit": "$B", "higher_is_bad": False,
        "latest_value": None, "prev_value": None, "period_change": None,
        "trend_3m": "neutral", "z_score": 0.0,
        "history_dates": [], "history_values": [],
    }

    if walcl is None or rrp is None or tga is None:
        return _empty_top, _empty_ind

    combined = pd.concat([walcl, rrp, tga], axis=1).dropna()
    combined.columns = ["walcl", "rrp", "tga"]

    if len(combined) < 14:
        return _empty_top, _empty_ind

    # All three components are already in $B at this point:
    #   WALCL:     scale=0.001 applied at fetch ($M → $B)
    #   RRPONTSYD: FRED reports directly in $B
    #   WTREGEN:   scale=0.001 applied at fetch ($M → $B)
    walcl_b = combined["walcl"]
    rrp_b   = combined["rrp"]
    tga_b   = combined["tga"]

    net_liq = walcl_b - rrp_b - tga_b

    current = round(float(net_liq.iloc[-1]), 3)
    prev    = round(float(net_liq.iloc[-2]), 3) if len(net_liq) >= 2 else None

    idx_1m    = min(4,  len(net_liq) - 1)
    idx_3m    = min(13, len(net_liq) - 1)
    change_1m = round(float(net_liq.iloc[-1] - net_liq.iloc[-(idx_1m + 1)]), 3)
    change_3m = round(float(net_liq.iloc[-1] - net_liq.iloc[-(idx_3m + 1)]), 3)

    z = compute_z_score(net_liq, current)

    # Classification and trend are derived from the same 3M change so they
    # are always in sync on the card and in the indicator table.
    if change_3m > 100:
        classification = "Expanding"
        trend          = "improving"
    elif change_3m < -100:
        classification = "Contracting"
        trend          = "deteriorating"
    else:
        classification = "Neutral"
        trend          = "neutral"

    # Component drivers (1M changes, $B)
    walcl_1m = round(float(walcl_b.iloc[-1] - walcl_b.iloc[-(idx_1m + 1)]), 3)
    rrp_1m   = round(float(rrp_b.iloc[-1]   - rrp_b.iloc[-(idx_1m + 1)]), 3)
    tga_1m   = round(float(tga_b.iloc[-1]   - tga_b.iloc[-(idx_1m + 1)]), 3)

    drivers: list[str] = []
    if walcl_1m > 0:
        drivers.append(f"Fed balance sheet expanding (+${walcl_1m:.0f}B 1M)")
    elif walcl_1m < 0:
        drivers.append(f"Fed balance sheet contracting (${walcl_1m:.0f}B 1M)")
    if rrp_1m < 0:
        drivers.append(f"Reverse repo draining (${rrp_1m:.0f}B 1M, adds liquidity)")
    elif rrp_1m > 0:
        drivers.append(f"Reverse repo absorbing (+${rrp_1m:.0f}B 1M, drains liquidity)")
    if tga_1m < 0:
        drivers.append(f"Treasury cash declining (${tga_1m:.0f}B 1M, adds liquidity)")
    elif tga_1m > 0:
        drivers.append(f"Treasury cash building (+${tga_1m:.0f}B 1M, drains liquidity)")
    if not drivers:
        drivers.append("Net liquidity broadly stable across all three components")

    chart = net_liq.iloc[-CHART_WEEKS:]

    top_level = {
        "value":          current,
        "change_1m":      change_1m,
        "change_3m":      change_3m,
        "classification": classification,
        "drivers":        drivers,
    }
    indicator = {
        "series_id":      "GLOBAL_LIQUIDITY",
        "label":          "Global Liquidity Index",
        "group":          "liquidity",
        "unit":           "$B",
        "higher_is_bad":  False,
        "latest_value":   current,
        "prev_value":     prev,
        "period_change":  change_1m,
        "change_3m":      change_3m,
        "trend_3m":       trend,
        "z_score":        z,
        "history_dates":  [d.strftime("%Y-%m-%d") for d in chart.index],
        "history_values": [round(float(v), 3) for v in chart.values],
    }
    return top_level, indicator


# ---------------------------------------------------------------------------
# Current Conditions
# ---------------------------------------------------------------------------

def compute_current_conditions(
    raw: dict, z_scores: dict, latest: dict
) -> dict:
    """
    Classify current macro conditions using coincident/near-coincident indicators:
    ICSA, CCSA, AWHAETP, BAMLH0A0HYM2, NFCI.
    Score 0–10: ≤2 Healthy, ≤5 Softening, ≤8 Fragile, 9+ Recessionary.
    Returns {"label", "score", "drivers"}.
    """
    score   = 0
    drivers: list[str] = []

    # ICSA
    icsa_z = z_scores.get("ICSA", 0.0)
    if icsa_z > 1.5:
        score += 2
        drivers.append(f"Initial claims sharply elevated (z={icsa_z:.1f})")
    elif icsa_z > 0.5:
        score += 1
        drivers.append(f"Initial claims above historical average (z={icsa_z:.1f})")
    else:
        drivers.append(f"Initial claims remain low vs history (z={icsa_z:.1f})")

    # CCSA
    ccsa_z = z_scores.get("CCSA", 0.0)
    if ccsa_z > 1.5:
        score += 2
        drivers.append(f"Continuing claims sharply elevated (z={ccsa_z:.1f})")
    elif ccsa_z > 0.5:
        score += 1
        drivers.append(f"Continuing claims above historical average (z={ccsa_z:.1f})")
    else:
        drivers.append(f"Continuing claims contained (z={ccsa_z:.1f})")

    # AWHAETP
    awh_z = z_scores.get("AWHAETP", 0.0)
    awh_v = latest.get("AWHAETP")
    if awh_z < -1.5:
        score += 2
        drivers.append(f"Avg weekly hours declining sharply (z={awh_z:.1f})")
    elif awh_z < -0.5:
        score += 1
        drivers.append(f"Avg weekly hours below trend (z={awh_z:.1f})")
    else:
        v_str = f"{awh_v:.1f} hrs" if awh_v is not None else "stable"
        drivers.append(f"Avg weekly hours stable ({v_str})")

    # BAMLH0A0HYM2
    hy = latest.get("BAMLH0A0HYM2", 0.0)
    if hy > 500:
        score += 2
        drivers.append(f"HY spreads in stress territory ({hy:.0f} bps)")
    elif hy > 400:
        score += 1
        drivers.append(f"HY spreads elevated ({hy:.0f} bps)")
    else:
        drivers.append(f"HY spreads contained ({hy:.0f} bps)" if hy else "HY spreads contained")

    # NFCI
    nfci = latest.get("NFCI")
    if nfci is not None:
        if nfci > 0.5:
            score += 2
            drivers.append(f"Financial conditions tightening materially (NFCI={nfci:.2f})")
        elif nfci > 0.0:
            score += 1
            drivers.append(f"Financial conditions modestly tight (NFCI={nfci:.2f})")
        else:
            drivers.append(f"Financial conditions accommodative (NFCI={nfci:.2f})")

    if score <= 2:
        label = "Healthy"
    elif score <= 5:
        label = "Softening"
    elif score <= 8:
        label = "Fragile"
    else:
        label = "Recessionary"

    return {"label": label, "score": score, "drivers": drivers[:3]}


# ---------------------------------------------------------------------------
# Forward Recession Risk (6–12 months)
# ---------------------------------------------------------------------------

def compute_forward_recession_risk(
    raw: dict, z_scores: dict, latest: dict, ci_top: dict
) -> dict:
    """
    Leading-indicator forward recession risk, 6–12 month horizon.
    Weighted score 0–100 (capped):
      Yield curve:        level 10 + momentum 10 = max 20
      HY spreads:         level 10 + momentum 10 = max 20
      NFCI:               trend                  = max 10
      Lending standards:  level                  = max 15
      Credit impulse:                             = max 15
      Consumer stress:                            = max 15
      Labor trends:                               = max 15

    Thresholds: 0–15 Low, 16–40 Guarded, 41–65 Elevated, 66+ High.

    Guardrail: if ≥2 of the following conditions are active, floor is Guarded:
      • credit impulse negative
      • consumer delinquency z > 1
      • yield curve flattening over 3M
      • HY spreads widening over 3M
      • bank lending tightening (DRTSCLCC z > 0.25)

    Returns {"label", "score", "drivers"}.
    """
    score        = 0
    contributors: list[tuple[int, str]] = []
    guardrail: list[bool] = []

    # ── Yield curve (max 20: level + momentum) ────────────────────────
    # Level bands reflect how much structural risk the current spread
    # carries; momentum captures recent flattening pressure.
    t10y2y      = latest.get("T10Y2Y")
    yc_pts      = 0
    yc_msg      = ""
    yc_flat     = False
    level_score = 0
    mom_score   = 0
    curve_chg_1m: float | None = None
    curve_chg_3m: float | None = None

    if t10y2y is not None:
        # ── Level score ──────────────────────────────────────────────
        if t10y2y < 0:
            level_score = 15
        elif t10y2y < 0.5:
            level_score = 8
        elif t10y2y < 1.0:
            level_score = 4
        # else: level_score = 0

        # ── Momentum score (1M + 3M flattening) ──────────────────────
        t10y2y_s = raw.get("T10Y2Y")
        if t10y2y_s is not None and len(t10y2y_s) >= 5:
            curve_chg_1m = round(float(t10y2y_s.iloc[-1] - t10y2y_s.iloc[-min(5, len(t10y2y_s))]), 4)
        if t10y2y_s is not None and len(t10y2y_s) >= 14:
            curve_chg_3m = round(float(t10y2y_s.iloc[-1] - t10y2y_s.iloc[-min(14, len(t10y2y_s))]), 4)

        flat_1m = curve_chg_1m is not None and curve_chg_1m < -0.10
        flat_3m = curve_chg_3m is not None and curve_chg_3m < -0.20

        if flat_1m and flat_3m:
            mom_score = 10          # both firing = max momentum score
        else:
            if flat_1m:
                mom_score += 4
            if flat_3m:
                mom_score += 6

        yc_flat = flat_1m or flat_3m
        yc_pts  = min(20, level_score + mom_score)

        # ── Driver description ────────────────────────────────────────
        if t10y2y < 0:
            yc_msg = f"Yield curve inverted ({t10y2y:+.2f}%), strong recession warning"
        elif t10y2y < 0.5:
            yc_msg = (
                f"Yield curve flat and flattening ({t10y2y:.2f}%), historically late-cycle"
                if yc_flat else
                f"Yield curve historically flat ({t10y2y:.2f}%), late-cycle territory"
            )
        elif t10y2y < 1.0:
            yc_msg = (
                f"Yield curve positive but flattening ({t10y2y:.2f}%), early warning"
                if yc_flat else
                f"Yield curve modest ({t10y2y:.2f}%), watch for continued compression"
            )
        else:
            if yc_flat:
                yc_msg = f"Yield curve flattening from healthy level ({t10y2y:.2f}%)"

        # Append momentum numbers when they contributed
        mom_parts = []
        if flat_1m and curve_chg_1m is not None:
            mom_parts.append(f"1M: {curve_chg_1m:+.2f}%")
        if flat_3m and curve_chg_3m is not None:
            mom_parts.append(f"3M: {curve_chg_3m:+.2f}%")
        if mom_parts and yc_msg:
            yc_msg += f" ({', '.join(mom_parts)})"

    score += yc_pts
    if yc_pts > 0 and yc_msg:
        contributors.append((yc_pts, yc_msg))
    guardrail.append(yc_flat)

    # ── HY spreads (max 20: level 10 + momentum 10) ───────────────────
    hy          = latest.get("BAMLH0A0HYM2", 0.0)
    hy_pts      = 0
    hy_msg      = ""
    hy_widening = False
    # Level (max 10)
    if hy > 500:
        hy_pts += 10
        hy_msg = f"HY spreads in danger zone ({hy:.0f} bps)"
    elif hy > 400:
        hy_pts += 7
        hy_msg = f"HY spreads elevated ({hy:.0f} bps)"
    elif hy > 350:
        hy_pts += 4
        hy_msg = f"HY spreads modestly wide ({hy:.0f} bps)"
    # Momentum (max 10)
    hy_s = raw.get("BAMLH0A0HYM2")
    if hy_s is not None and len(hy_s) >= 13:
        chg3m = float(hy_s.iloc[-1] - hy_s.iloc[-13])
        if chg3m > 100:
            hy_pts     += 10
            hy_widening = True
            suffix = f"; widened {chg3m:+.0f} bps over 3M"
        elif chg3m > 50:
            hy_pts     += 6
            hy_widening = True
            suffix = f"; widened {chg3m:+.0f} bps over 3M"
        elif chg3m > 25:
            hy_pts     += 3
            hy_widening = True
            suffix = f"; widening (+{chg3m:.0f} bps over 3M)"
        else:
            suffix = ""
        if hy_widening and suffix:
            hy_msg = (hy_msg + suffix) if hy_msg else f"HY spreads widening ({chg3m:+.0f} bps over 3M)"
    hy_pts = min(20, hy_pts)
    score  += hy_pts
    if hy_pts > 0 and hy_msg:
        contributors.append((hy_pts, hy_msg))
    guardrail.append(hy_widening)

    # ── NFCI (max 10) ────────────────────────────────────────────────
    nfci     = latest.get("NFCI")
    nfci_pts = 0
    if nfci is not None:
        if nfci > 0.5:
            nfci_pts = 10
            contributors.append((10, f"Financial conditions materially tight (NFCI={nfci:.2f})"))
        elif nfci > 0.0:
            nfci_pts = 6
            contributors.append((6, f"Financial conditions modestly tight (NFCI={nfci:.2f})"))
        elif nfci > -0.25:
            nfci_pts = 3
    score += nfci_pts

    # ── Lending standards (max 15) ────────────────────────────────────
    drt_z      = z_scores.get("DRTSCLCC", 0.0)
    drt_pts    = 0
    drt_active = drt_z > 0.25
    if drt_z > 1.5:
        drt_pts = 15
        contributors.append((15, f"Lending standards severely tightened (z={drt_z:.1f})"))
    elif drt_z > 0.75:
        drt_pts = 10
        contributors.append((10, f"Lending standards tightened meaningfully (z={drt_z:.1f})"))
    elif drt_z > 0.25:
        drt_pts = 5
        contributors.append((5, f"Lending standards tightening (z={drt_z:.1f})"))
    score += drt_pts
    guardrail.append(drt_active)

    # ── Credit impulse (max 15) ───────────────────────────────────────
    ci_class    = ci_top.get("classification", "Unknown")
    ci_val      = ci_top.get("value")
    ci_pts      = 0
    ci_negative = ci_class == "Negative"
    if ci_negative:
        ci_pts = 15
        v_str  = f"${ci_val:.1f}B" if ci_val is not None else "negative"
        contributors.append((15, f"Credit impulse negative ({v_str})"))
    elif ci_class == "Neutral":
        ci_pts = 8
        v_str  = f"${ci_val:.1f}B" if ci_val is not None else "neutral"
        contributors.append((8, f"Credit impulse neutral ({v_str}), growth decelerating"))
    score += ci_pts
    guardrail.append(ci_negative)

    # ── Consumer stress (max 15) ──────────────────────────────────────
    cc_z          = z_scores.get("DRCCLACBS", 0.0)
    cl_z          = z_scores.get("DRCLACBS",  0.0)
    cs_pts        = 0
    cs_msgs: list[str] = []
    consumer_elevated = False
    if cc_z > 1.5:
        cs_pts           += 8
        consumer_elevated = True
        cs_msgs.append(f"Credit card delinquency sharply elevated (z={cc_z:.1f})")
    elif cc_z > 1.0:
        cs_pts           += 5
        consumer_elevated = True
        cs_msgs.append(f"Credit card delinquency elevated (z={cc_z:.1f})")
    if cl_z > 1.5:
        cs_pts           += 7
        consumer_elevated = True
        cs_msgs.append(f"Consumer loan delinquency sharply elevated (z={cl_z:.1f})")
    elif cl_z > 1.0:
        cs_pts           += 5
        consumer_elevated = True
        cs_msgs.append(f"Consumer loan delinquency elevated (z={cl_z:.1f})")
    cs_pts = min(15, cs_pts)
    if cs_pts > 0:
        contributors.append((cs_pts, "; ".join(cs_msgs)))
    score += cs_pts
    guardrail.append(consumer_elevated)

    # ── Labor deterioration trend (max 15) ────────────────────────────
    labor_pts  = 0
    labor_msgs: list[str] = []
    for sid, msg in [
        ("ICSA",    "Initial claims trending up over 3M"),
        ("CCSA",    "Continuing claims trending up over 3M"),
        ("AWHAETP", "Avg weekly hours declining over 3M"),
    ]:
        s = raw.get(sid)
        if s is not None and len(s) >= 8 and compute_trend(sid, s) == "deteriorating":
            labor_pts += 5
            labor_msgs.append(msg)
    if labor_pts > 0:
        contributors.append((labor_pts, "; ".join(labor_msgs)))
    score += labor_pts

    score = min(100, score)

    # ── Classify ──────────────────────────────────────────────────────
    if score <= 15:
        label = "Low"
    elif score <= 40:
        label = "Guarded"
    elif score <= 65:
        label = "Elevated"
    else:
        label = "High"

    # ── Guardrail: ≥2 risk flags → floor is Guarded ───────────────────
    if label == "Low" and sum(guardrail) >= 2:
        label = "Guarded"

    contributors.sort(key=lambda x: -x[0])
    drivers = [c[1] for c in contributors[:3]]
    if not drivers:
        drivers = ["Leading indicators broadly supportive of continued expansion"]

    return {
        "label":   label,
        "score":   score,
        "drivers": drivers,
        "yield_curve_signal": {
            "level_score":    level_score,
            "momentum_score": mom_score,
            "curve_change_1m": curve_chg_1m,
            "curve_change_3m": curve_chg_3m,
            "description":    yc_msg or "Yield curve data unavailable",
        },
    }


# ---------------------------------------------------------------------------
# Deterioration Speed
# ---------------------------------------------------------------------------

_SPEED_INDICATORS = {
    "ICSA":         {"label": "Initial claims",       "higher_is_bad": True},
    "CCSA":         {"label": "Continuing claims",    "higher_is_bad": True},
    "BAMLH0A0HYM2": {"label": "HY spreads",           "higher_is_bad": True},
    "NFCI":         {"label": "Financial conditions", "higher_is_bad": True},
    "AWHAETP":      {"label": "Avg weekly hours",     "higher_is_bad": False},
    "T10Y2Y":       {"label": "Yield curve",          "higher_is_bad": False},
    "DRTSCLCC":     {"label": "Lending standards",    "higher_is_bad": True},
}


def compute_deterioration_speed(raw: dict) -> dict:
    """
    Count indicators showing meaningful deterioration on BOTH 1M and 3M timeframes.
    Also requires above-median 1M momentum z-score in the bad direction.
    0–1 Low, 2–3 Moderate, 4+ High.
    Returns {"label", "count", "drivers"}.
    """
    count     = 0
    triggered: list[str] = []

    for sid, cfg in _SPEED_INDICATORS.items():
        series     = raw.get(sid)
        higher_bad = cfg["higher_is_bad"]
        if series is None or len(series) < 14:
            continue

        idx_1m = min(4,  len(series) - 1)
        idx_3m = min(13, len(series) - 1)
        chg_1m = float(series.iloc[-1] - series.iloc[-(idx_1m + 1)])
        chg_3m = float(series.iloc[-1] - series.iloc[-(idx_3m + 1)])

        all_1m = series.diff(4).dropna()
        mom_z  = compute_z_score(all_1m, chg_1m)

        det_1m  = (higher_bad and chg_1m > 0) or (not higher_bad and chg_1m < 0)
        det_3m  = (higher_bad and chg_3m > 0) or (not higher_bad and chg_3m < 0)
        sig_mom = abs(mom_z) > 0.5

        if det_1m and det_3m and sig_mom:
            count += 1
            triggered.append(cfg["label"])

    if count <= 1:
        label = "Low"
    elif count <= 3:
        label = "Moderate"
    else:
        label = "High"

    if triggered:
        drivers = [
            f"{count} of 7 indicators deteriorating on both 1M and 3M: {', '.join(triggered)}",
        ]
    else:
        drivers = ["No indicators showing meaningful momentum deterioration"]

    return {"label": label, "count": count, "drivers": drivers}


# ---------------------------------------------------------------------------
# Recession Confirmation Signal
# ---------------------------------------------------------------------------

def compute_recession_confirmation(raw: dict) -> tuple[str, int, dict]:
    """
    Labor market confirmation signal.
    Rules:
      1. ICSA latest > 13-week average by ≥ 8%    → +1
      2. CCSA latest > 13-week average by ≥ 5%    → +1
      3. AWHAETP latest ≥ 0.1 hrs below 13-wk avg → +1
    Score 0 = Low, 1 = Watch, 2–3 = High.
    """
    score = 0
    details: dict = {}

    icsa = raw.get("ICSA")
    if icsa is not None and len(icsa) >= 13:
        avg13 = float(icsa.iloc[-13:].mean())
        latest = float(icsa.iloc[-1])
        triggered = avg13 > 0 and latest >= avg13 * 1.08
        details["icsa_latest"]    = round(latest, 0)
        details["icsa_avg13"]     = round(avg13, 0)
        details["icsa_triggered"] = triggered
        if triggered:
            score += 1

    ccsa = raw.get("CCSA")
    if ccsa is not None and len(ccsa) >= 13:
        avg13 = float(ccsa.iloc[-13:].mean())
        latest = float(ccsa.iloc[-1])
        triggered = avg13 > 0 and latest >= avg13 * 1.05
        details["ccsa_latest"]    = round(latest, 0)
        details["ccsa_avg13"]     = round(avg13, 0)
        details["ccsa_triggered"] = triggered
        if triggered:
            score += 1

    awhaetp = raw.get("AWHAETP")
    if awhaetp is not None and len(awhaetp) >= 13:
        avg13 = float(awhaetp.iloc[-13:].mean())
        latest = float(awhaetp.iloc[-1])
        triggered = (avg13 - latest) >= 0.1
        details["awhaetp_latest"]    = round(latest, 2)
        details["awhaetp_avg13"]     = round(avg13, 2)
        details["awhaetp_triggered"] = triggered
        if triggered:
            score += 1

    label = "Low" if score == 0 else "Watch" if score == 1 else "High"
    return label, score, details


# ---------------------------------------------------------------------------
# Regime Shift Detection
# ---------------------------------------------------------------------------

def generate_shift_signals(shift_prob: str) -> list[dict]:
    """Trade positioning ideas keyed to regime shift probability."""
    if shift_prob == "HIGH":
        return [
            {"asset": "Long Treasuries (TLT)", "direction": "bullish",
             "rationale": "Multiple deteriorating signals point to an approaching rate-cutting cycle; duration outperforms."},
            {"asset": "Gold (GLD)",             "direction": "bullish",
             "rationale": "Regime shift risk drives safe-haven demand and real-rate compression."},
            {"asset": "US Equities (SPY)",      "direction": "bearish",
             "rationale": "Broad simultaneous deterioration across labor, credit, and financial conditions is historically bearish."},
            {"asset": "High Yield (HYG)",        "direction": "bearish",
             "rationale": "Widening credit spreads and tightening conditions compress high-yield returns."},
            {"asset": "Small Caps (IWM)",        "direction": "bearish",
             "rationale": "Small caps most exposed to credit-cycle turning points and earnings pressure."},
        ]
    if shift_prob == "MODERATE":
        return [
            {"asset": "Defensives (XLV / XLP)", "direction": "bullish",
             "rationale": "Rotate toward non-cyclical sectors as multiple macro indicators soften."},
            {"asset": "Inv. Grade Bonds (LQD)",  "direction": "bullish",
             "rationale": "Quality credit outperforms as growth decelerates on deteriorating conditions."},
            {"asset": "Cyclicals (XLY / XLI)",  "direction": "bearish",
             "rationale": "Reduce cyclical exposure with 2 indicators showing unusual momentum deterioration."},
        ]
    # LOW
    return [
        {"asset": "Maintain current allocation", "direction": "neutral",
         "rationale": "Macro momentum is broadly stable. No urgent repositioning required."},
    ]


def compute_regime_shift(
    raw: dict[str, pd.Series],
    z_scores: dict[str, float],
    recession_prob: float,
) -> tuple[str, int, list[dict], list[dict]]:
    """
    Regime Shift Engine.
    For each of 5 key indicators computes:
      • 1-month absolute change (4 weekly obs)
      • 3-month absolute change (13 weekly obs)
      • z-score of 1-month momentum vs all historical 1-month changes
    Counts how many are deteriorating; returns shift_prob, count, alerts, signals.
    """
    alerts: list[dict] = []
    deteriorating_count = 0

    for sid, cfg in SHIFT_INDICATORS.items():
        series     = raw.get(sid)
        higher_bad = cfg["higher_is_bad"]
        label      = cfg["label"]
        unit       = SERIES_CONFIG.get(sid, {}).get("unit", "")

        if series is None or len(series) < 5:
            alerts.append({
                "series_id": sid, "label": label, "unit": unit,
                "message": SHIFT_ALERT_MESSAGES[sid],
                "deteriorating": False, "severity": "stable",
                "momentum_z": 0.0, "change_1m": None, "change_3m": None,
            })
            continue

        # Absolute changes
        idx_1m    = min(4,  len(series) - 1)
        idx_3m    = min(13, len(series) - 1)
        change_1m = round(float(series.iloc[-1] - series.iloc[-(idx_1m + 1)]), 6)
        change_3m = round(float(series.iloc[-1] - series.iloc[-(idx_3m + 1)]), 6)

        # Z-score of current 1-month change vs distribution of all historical 1-month changes
        all_1m_changes = series.diff(4).dropna()
        momentum_z     = compute_z_score(all_1m_changes, change_1m)

        # Deteriorating: moving in the bad direction with unusual speed
        is_deteriorating = (
            (higher_bad is True  and momentum_z >  0.5) or
            (higher_bad is False and momentum_z < -0.5)
        )
        if is_deteriorating:
            deteriorating_count += 1

        abs_z    = abs(momentum_z)
        severity = (
            ("high"   if abs_z >= 1.5 else "medium" if abs_z >= 0.75 else "low")
            if is_deteriorating else "stable"
        )

        alerts.append({
            "series_id":     sid,
            "label":         label,
            "unit":          unit,
            "message":       SHIFT_ALERT_MESSAGES[sid],
            "deteriorating": is_deteriorating,
            "severity":      severity,
            "momentum_z":    round(momentum_z, 3),
            "change_1m":     change_1m,
            "change_3m":     change_3m,
        })

    # Shift probability
    if deteriorating_count >= 3:
        shift_prob = "HIGH"
    elif deteriorating_count >= 2:
        shift_prob = "MODERATE"
    else:
        shift_prob = "LOW"

    shift_signals = generate_shift_signals(shift_prob)
    return shift_prob, deteriorating_count, alerts, shift_signals


# ---------------------------------------------------------------------------
# Tradable signals
# ---------------------------------------------------------------------------

def generate_signals(
    regime: str,
    recession_prob: float,
    latest: dict,
    z_scores: dict,
) -> list[dict]:

    if regime == "Crisis":
        return [
            {"asset": "Short Treasuries (SHY)",          "direction": "bullish",
             "rationale": "Extreme flight-to-safety with liquidity crunch; short end bid hardest."},
            {"asset": "Gold (GLD)",                       "direction": "bullish",
             "rationale": "Safe-haven demand at maximum; gold historically outperforms in crisis."},
            {"asset": "US Dollar (UUP)",                  "direction": "bullish",
             "rationale": "Dollar liquidity crunch drives dollar strength in global crisis."},
            {"asset": "High Yield Credit (HYG)",          "direction": "bearish",
             "rationale": "Credit markets seize; HY spreads blow out to crisis levels."},
            {"asset": "Equities (SPY)",                   "direction": "bearish",
             "rationale": "Equity markets compress sharply in recession + liquidity crisis."},
            {"asset": "Commodities (DJP)",                "direction": "bearish",
             "rationale": "Demand destruction and dollar strength crush commodity prices."},
        ]

    if regime == "Expansion" and recession_prob < 30:
        return [
            {"asset": "US Equities (SPY)",           "direction": "bullish",
             "rationale": "Expansion phase is historically strongest for broad equities."},
            {"asset": "Small Caps (IWM)",             "direction": "bullish",
             "rationale": "Small caps outperform materially in early-to-mid expansion."},
            {"asset": "Cyclicals / Industrials (XLI)","direction": "bullish",
             "rationale": "Industrial output and capex expand with the cycle."},
            {"asset": "Commodities (DJP)",            "direction": "bullish",
             "rationale": "Demand-driven commodity tailwind in expansion."},
            {"asset": "Long Duration Bonds (TLT)",    "direction": "bearish",
             "rationale": "Growth and inflation pressure hurt long-duration bonds."},
            {"asset": "Defensive Equity (XLU)",       "direction": "bearish",
             "rationale": "Utilities and defensives lag in risk-on expansion."},
        ]

    if regime == "Slowdown":
        return [
            {"asset": "Inv. Grade Bonds (LQD)",       "direction": "bullish",
             "rationale": "Quality credit outperforms as growth decelerates."},
            {"asset": "Healthcare (XLV)",              "direction": "bullish",
             "rationale": "Defensives hold up as consumer spending softens."},
            {"asset": "Consumer Staples (XLP)",        "direction": "bullish",
             "rationale": "Non-discretionary demand is resilient in slowdowns."},
            {"asset": "Cyclical Equities (XLY)",       "direction": "bearish",
             "rationale": "Discretionary spending the first to decline in slowdowns."},
            {"asset": "Small Caps (IWM)",              "direction": "bearish",
             "rationale": "Higher credit sensitivity hurts small caps in tighter conditions."},
            {"asset": "Energy (XLE)",                  "direction": "bearish",
             "rationale": "Commodity demand weakens as economic activity cools."},
        ]

    if regime == "Late Cycle":
        return [
            {"asset": "Long Duration Bonds (TLT)",    "direction": "bullish",
             "rationale": "Curve flattening and looming recession support long duration."},
            {"asset": "Gold (GLD)",                   "direction": "bullish",
             "rationale": "Late-cycle macro stress and potential Fed pivot favor gold."},
            {"asset": "Utilities (XLU)",              "direction": "bullish",
             "rationale": "Defensive positioning outperforms as cycle matures."},
            {"asset": "High Yield Credit (HYG)",      "direction": "bearish",
             "rationale": "Default risk rises and HY spreads widen in late cycle."},
            {"asset": "Banks / Financials (XLF)",     "direction": "bearish",
             "rationale": "Credit cycle deterioration pressures bank earnings."},
            {"asset": "Small Caps (IWM)",             "direction": "bearish",
             "rationale": "Financing costs compress small-cap margins late cycle."},
        ]

    # Recession Risk
    return [
        {"asset": "Long Duration Bonds (TLT)",    "direction": "bullish",
         "rationale": "Fed easing cycle drives yields lower; bonds rally in recession."},
        {"asset": "Gold (GLD)",                   "direction": "bullish",
         "rationale": "Safe-haven demand surges; gold historically outperforms in recessions."},
        {"asset": "US Dollar (UUP)",              "direction": "bullish",
         "rationale": "Flight-to-safety bids drive dollar strength."},
        {"asset": "High Yield Credit (HYG)",      "direction": "bearish",
         "rationale": "Spreads blow out; HY defaults accelerate in recessions."},
        {"asset": "Small Caps (IWM)",             "direction": "bearish",
         "rationale": "Most exposed to credit crunch and earnings collapse."},
        {"asset": "Commodities (DJP)",            "direction": "bearish",
         "rationale": "Demand destruction crushes commodity prices in recessions."},
    ]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    api_key = get_api_key()
    fred    = Fred(api_key=api_key)
    start   = datetime.today() - timedelta(days=ZSCORE_DAYS)

    print(f"Fetching {len(SERIES_CONFIG)} FRED series from {start.date()}…")

    # ── Fetch all series ──────────────────────────────────────────────────
    raw: dict[str, pd.Series] = {}
    for sid, cfg in SERIES_CONFIG.items():
        print(f"  {sid:<16}", end="", flush=True)
        s = fetch_series(fred, sid, start)
        if s.empty:
            print("FAILED")
        else:
            weekly = resample_to_weekly(s)
            # Apply optional unit scaling (e.g. WTREGEN: $M → $B)
            scale = cfg.get("scale", 1.0)
            if scale != 1.0:
                weekly = weekly * scale
            raw[sid] = weekly
            print(f"OK  ({len(weekly)} obs,  latest={weekly.iloc[-1]:.4g}  {weekly.index[-1].date()})")

    # ── Compute per-series statistics ─────────────────────────────────────
    indicators: list[dict] = []
    z_scores:   dict[str, float] = {}
    latest_vals: dict[str, float] = {}

    for sid, cfg in SERIES_CONFIG.items():
        series = raw.get(sid)

        if series is None or series.empty:
            indicators.append({
                "series_id":       sid,
                "label":           cfg["label"],
                "group":           cfg["group"],
                "unit":            cfg["unit"],
                "higher_is_bad":   cfg["higher_is_bad"],
                "latest_value":    None,
                "prev_value":      None,
                "period_change":   None,
                "trend_3m":        "neutral",
                "z_score":         0.0,
                "history_dates":   [],
                "history_values":  [],
            })
            continue

        latest_val  = float(series.iloc[-1])
        prev_val    = float(series.iloc[-2]) if len(series) >= 2 else None
        period_chg  = round(latest_val - prev_val, 6) if prev_val is not None else None
        trend       = compute_trend(sid, series)
        z           = compute_z_score(series, latest_val)

        z_scores[sid]    = z
        latest_vals[sid] = latest_val

        chart = series.iloc[-CHART_WEEKS:]
        indicators.append({
            "series_id":      sid,
            "label":          cfg["label"],
            "group":          cfg["group"],
            "unit":           cfg["unit"],
            "higher_is_bad":  cfg["higher_is_bad"],
            "latest_value":   round(latest_val, 6),
            "prev_value":     round(prev_val, 6) if prev_val is not None else None,
            "period_change":  round(period_chg, 6) if period_chg is not None else None,
            "trend_3m":       trend,
            "z_score":        z,
            "history_dates":  [d.strftime("%Y-%m-%d") for d in chart.index],
            "history_values": [round(float(v), 6) for v in chart.values],
        })

    # ── Macro signals ─────────────────────────────────────────────────────
    regime, risk_score, regime_details = classify_regime(z_scores, latest_vals)
    recession_prob = compute_recession_probability(z_scores)
    credit_stress  = compute_credit_stress(z_scores, latest_vals)
    liquidity_index, liquidity_regime = compute_liquidity(z_scores)

    # Escalate to Crisis when Recession Risk coincides with Tight liquidity
    if regime == "Recession Risk" and liquidity_regime == "Tight":
        regime = "Crisis"
        regime_details.insert(0, "Tight liquidity amplifies recession risk → Crisis")

    signals = generate_signals(regime, recession_prob, latest_vals, z_scores)
    shift_prob, shift_count, macro_alerts, shift_signals = compute_regime_shift(
        raw, z_scores, recession_prob
    )
    rc_label, rc_score, rc_details = compute_recession_confirmation(raw)
    ci_top, ci_ind = compute_credit_impulse(raw)
    indicators.append(ci_ind)

    gl_top, gl_ind = compute_global_liquidity(raw)
    indicators.append(gl_ind)

    curr_cond = compute_current_conditions(raw, z_scores, latest_vals)
    fwd_risk  = compute_forward_recession_risk(raw, z_scores, latest_vals, ci_top)
    yc_signal = fwd_risk.pop("yield_curve_signal", {})
    det_speed = compute_deterioration_speed(raw)

    # ── Write output ──────────────────────────────────────────────────────
    output = {
        "updated":                         datetime.today().strftime("%Y-%m-%d"),
        "macro_regime":                    regime,
        "regime_score":                    risk_score,
        "regime_details":                  regime_details,
        "recession_probability":           recession_prob,
        "credit_stress_level":             credit_stress,
        "liquidity_index":                 liquidity_index,
        "liquidity_regime":                liquidity_regime,
        "regime_shift_probability":        shift_prob,
        "regime_shift_count":              shift_count,
        "macro_alerts":                    macro_alerts,
        "regime_shift_signals":            shift_signals,
        "recession_confirmation":          rc_label,
        "recession_confirmation_score":    rc_score,
        "recession_confirmation_details":  rc_details,
        "credit_impulse":                  ci_top,
        "global_liquidity":                gl_top,
        "current_conditions":              curr_cond,
        "forward_recession_risk":          fwd_risk,
        "yield_curve_signal":              yc_signal,
        "deterioration_speed":             det_speed,
        "tradable_signals":                signals,
        "indicators":                      indicators,
    }

    out_path = Path(__file__).resolve().parent.parent / "public" / "macro.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as fh:
        json.dump(output, fh, indent=2)

    print(f"\n{'─'*55}")
    print(f"  Output:           {out_path}")
    print(f"  Macro Regime:     {regime}  (score={risk_score})")
    print(f"  Recession Prob:   {recession_prob}%")
    print(f"  Credit Stress:    {credit_stress}")
    print(f"  Liquidity:        {liquidity_regime}  (index={liquidity_index})")
    print(f"  Regime Shift:     {shift_prob}  ({shift_count}/5 deteriorating)")
    print(f"  Recession Conf:   {rc_label}  (score={rc_score}/3)")
    print(f"  Credit Impulse:   {ci_top.get('classification')}  (value={ci_top.get('value')}B)")
    print(f"  Global Liquidity: {gl_top.get('classification')}  (value={gl_top.get('value')}B, 3M chg={gl_top.get('change_3m')}B)")
    print(f"  Curr Conditions:  {curr_cond['label']}  (score={curr_cond['score']}/10)")
    print(f"  Forward Risk:     {fwd_risk['label']}  (score={fwd_risk['score']}/100)")
    print(f"  Det. Speed:       {det_speed['label']}  (count={det_speed['count']}/7)")
    print(f"  Signals:          {len(signals)}")
    print(f"  Z-scores:         {z_scores}")
    print(f"  Regime triggers:")
    for d in regime_details:
        print(f"    · {d}")
    print(f"{'─'*55}")


if __name__ == "__main__":
    main()
