#!/usr/bin/env python3
"""Calibration fit: test 5 formulas against 5 dashboard data points.

Two fitting strategies:
  1. CUMULATIVE fit — direct LS on (cumul_CU, observed_%) at all 5 points
  2. DELTA fit — LS on (delta_CU, delta_%) across 4 consecutive windows

Weekly reset: Fri 2026-03-20 08:00 UTC
Data points collected on Mon 2026-03-24.
"""

import sys
sys.path.insert(0, "/Users/otonashi/thinking/building/Claude-Code-Usage-Monitor/src")

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Tuple

from claude_monitor.core.models import TokenSnapshot, UsageEntry
from claude_monitor.data.analysis import CalibrationStore, ComputeModel
from claude_monitor.data.reader import load_usage_entries

# ── Constants ────────────────────────────────────────────────────────────────

WEEKLY_RESET = datetime(2026, 3, 20, 8, 0, 0, tzinfo=timezone.utc)

# (UTC timestamp, weekly-all %, sonnet-only %)
CAL_POINTS = [
    (datetime(2026, 3, 24,  7, 53, 0, tzinfo=timezone.utc), 52, 5),
    (datetime(2026, 3, 24, 10, 28, 0, tzinfo=timezone.utc), 52, 6),
    (datetime(2026, 3, 24, 12, 22, 0, tzinfo=timezone.utc), 53, 6),
    (datetime(2026, 3, 24, 13, 27, 0, tzinfo=timezone.utc), 56, 6),
    (datetime(2026, 3, 24, 13, 45, 0, tzinfo=timezone.utc), 56, 6),
]

# Delta windows: (from_idx, to_idx, delta_weekly_%, delta_sonnet_%)
DELTA_WINDOWS = [
    (0, 1, 0, 1),   # 07:53→10:28
    (1, 2, 1, 0),   # 10:28→12:22
    (2, 3, 3, 0),   # 12:22→13:27
    (3, 4, 0, 0),   # 13:27→13:45
]


# ── Helpers ──────────────────────────────────────────────────────────────────

def classify(model: str) -> str:
    m = model.lower()
    if "opus" in m: return "opus"
    if "sonnet" in m: return "sonnet"
    if "haiku" in m: return "haiku"
    return "other"

def is_sonnet(model: str) -> bool:
    return "sonnet" in model.lower()

@dataclass
class TokenBucket:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation: int = 0
    cache_read: int = 0

def bucket_entries(entries: List[UsageEntry]) -> Dict[str, TokenBucket]:
    buckets: Dict[str, TokenBucket] = defaultdict(TokenBucket)
    for e in entries:
        fam = classify(e.model)
        b = buckets[fam]
        b.input_tokens += e.input_tokens
        b.output_tokens += e.output_tokens
        b.cache_creation += e.cache_creation_tokens
        b.cache_read += e.cache_read_tokens
    return dict(buckets)

def entries_in_window(entries: List[UsageEntry], t_start: datetime, t_end: datetime) -> List[UsageEntry]:
    return [e for e in entries if t_start <= e.timestamp < t_end]

def entries_up_to(entries: List[UsageEntry], t_end: datetime) -> List[UsageEntry]:
    return [e for e in entries if e.timestamp < t_end]


# ── Formulas ─────────────────────────────────────────────────────────────────

def formula_a(buckets: Dict[str, TokenBucket]) -> float:
    """API rate-limit: input+cc at 1x, output at 5x, cr=0, no model mult."""
    total = 0.0
    for b in buckets.values():
        total += (b.input_tokens + b.cache_creation) * 1.0 + b.output_tokens * 5.0
    return total

def formula_b(buckets: Dict[str, TokenBucket]) -> float:
    """Cost-weighted with API pricing multipliers."""
    weights = {
        "opus":   {"input": 5.0,  "output": 25.0, "cc": 6.25, "cr": 0.5},
        "sonnet": {"input": 1.0,  "output": 5.0,  "cc": 1.25, "cr": 0.1},
        "haiku":  {"input": 0.08, "output": 0.4,  "cc": 0.1,  "cr": 0.008},
        "other":  {"input": 1.0,  "output": 5.0,  "cc": 1.25, "cr": 0.1},
    }
    total = 0.0
    for fam, b in buckets.items():
        w = weights.get(fam, weights["other"])
        total += b.input_tokens * w["input"] + b.output_tokens * w["output"] + b.cache_creation * w["cc"] + b.cache_read * w["cr"]
    return total

def formula_c(buckets: Dict[str, TokenBucket]) -> float:
    """Output-only: only output tokens count."""
    return sum(b.output_tokens for b in buckets.values())

def formula_d(buckets: Dict[str, TokenBucket]) -> float:
    """Input+Output only, no cache at all."""
    return sum(b.input_tokens + b.output_tokens for b in buckets.values())

def formula_e(buckets: Dict[str, TokenBucket]) -> float:
    """Formula A + model multiplier: opus=5x, sonnet=1x."""
    model_mult = {"opus": 5.0, "sonnet": 1.0, "haiku": 0.2, "other": 1.0}
    total = 0.0
    for fam, b in buckets.items():
        mult = model_mult.get(fam, 1.0)
        total += mult * ((b.input_tokens + b.cache_creation) * 1.0 + b.output_tokens * 5.0)
    return total

FORMULAS = {
    "A (API rate-limit)":  formula_a,
    "B (Cost-weighted)":   formula_b,
    "C (Output-only)":     formula_c,
    "D (Input+Output)":    formula_d,
    "E (A + model mult)":  formula_e,
}


# ── Fitting functions ────────────────────────────────────────────────────────

def fit_cumulative(cus: List[float], pcts: List[int]) -> Tuple[float, float]:
    """LS fit: minimize sum((cu_i/L - pct_i/100)^2) => L = sum(cu_i * pct_i/100) / sum((pct_i/100)^2)."""
    if not cus:
        return 0.0, float("inf")
    num = sum(cu * (p / 100.0) for cu, p in zip(cus, pcts))
    den = sum((p / 100.0) ** 2 for p in pcts)
    if den == 0:
        return 0.0, float("inf")
    limit = num / den
    residual = sum(((cu / limit) * 100.0 - p) ** 2 for cu, p in zip(cus, pcts))
    return limit, residual

def fit_delta(delta_cus: List[float], delta_pcts: List[int]) -> Tuple[float, float]:
    """LS fit on deltas. Zero-delta windows: penalize if predicted > 0.5%."""
    non_zero = [(cu, pct) for cu, pct in zip(delta_cus, delta_pcts) if pct > 0]
    if not non_zero:
        return 0.0, float("inf")
    num = sum(cu * (p / 100.0) for cu, p in non_zero)
    den = sum((p / 100.0) ** 2 for _, p in non_zero)
    if den == 0:
        return 0.0, float("inf")
    limit = num / den
    residual = 0.0
    for cu, pct in zip(delta_cus, delta_pcts):
        pred = (cu / limit) * 100.0 if limit > 0 else 0
        if pct > 0:
            residual += (pred - pct) ** 2
        elif pred >= 0.5:
            residual += pred ** 2
    return limit, residual


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    print("=" * 90)
    print("CALIBRATION FIT — 5 Formulas x 2 Strategies vs 5 Dashboard Points")
    print("=" * 90)
    print(f"Weekly reset:  {WEEKLY_RESET.isoformat()}")
    print(f"Data points:   {len(CAL_POINTS)}")
    print()

    # Load
    print("Loading usage entries...")
    entries, _ = load_usage_entries(hours_back=120)
    weekly = [e for e in entries if e.timestamp >= WEEKLY_RESET]
    print(f"  Entries since reset: {len(weekly)}")
    print()

    # ── Delta token breakdown ────────────────────────────────────────────
    print("-" * 90)
    print("DELTA TOKEN BREAKDOWN")
    print("-" * 90)

    delta_entries_all = []
    for idx_a, idx_b, d_weekly, d_sonnet in DELTA_WINDOWS:
        t_start, t_end = CAL_POINTS[idx_a][0], CAL_POINTS[idx_b][0]
        w_entries = entries_in_window(weekly, t_start, t_end)
        delta_entries_all.append(w_entries)

        buckets = bucket_entries(w_entries)
        print(f"\n  Window {idx_a+1}->{idx_b+1}: {t_start.strftime('%H:%M')}->{t_end.strftime('%H:%M')} UTC"
              f"  weekly_delta={d_weekly}%  sonnet_delta={d_sonnet}%  entries={len(w_entries)}")
        for fam in ["opus", "sonnet", "haiku"]:
            b = buckets.get(fam)
            if b and (b.input_tokens + b.output_tokens + b.cache_creation + b.cache_read > 0):
                print(f"    {fam:8s}  inp={b.input_tokens:>10,}  out={b.output_tokens:>10,}"
                      f"  cc={b.cache_creation:>10,}  cr={b.cache_read:>10,}")

    # ── Cumulative at each point ─────────────────────────────────────────
    print()
    print("-" * 90)
    print("CUMULATIVE AT EACH CALIBRATION POINT")
    print("-" * 90)

    cumul_all = []
    cumul_sonnet = []
    for i, (ts, wpct, spct) in enumerate(CAL_POINTS):
        cum = entries_up_to(weekly, ts)
        cum_s = [e for e in cum if is_sonnet(e.model)]
        cumul_all.append(cum)
        cumul_sonnet.append(cum_s)

        bk = bucket_entries(cum)
        raw = sum(b.input_tokens + b.output_tokens + b.cache_creation + b.cache_read for b in bk.values())
        print(f"\n  P{i+1} {ts.strftime('%H:%M')} UTC  weekly={wpct}%  sonnet={spct}%"
              f"  entries={len(cum)}  raw_tokens={raw:,}")
        for fam in ["opus", "sonnet", "haiku"]:
            b = bk.get(fam)
            if b:
                print(f"    {fam:8s}  inp={b.input_tokens:>12,}  out={b.output_tokens:>12,}"
                      f"  cc={b.cache_creation:>12,}  cr={b.cache_read:>12,}")

    # ── Fit each formula ─────────────────────────────────────────────────
    print()
    print("=" * 90)
    print("FORMULA RESULTS")
    print("=" * 90)

    actual_weekly = [p[1] for p in CAL_POINTS]
    actual_sonnet = [p[2] for p in CAL_POINTS]
    delta_weekly_pcts = [dw[2] for dw in DELTA_WINDOWS]
    delta_sonnet_pcts = [dw[3] for dw in DELTA_WINDOWS]

    results = {}

    for name, func in FORMULAS.items():
        # Cumulative CUs
        cum_cus = [func(bucket_entries(c)) for c in cumul_all]
        son_cum_cus = [func(bucket_entries(c)) for c in cumul_sonnet]

        # Delta CUs
        dlt_cus = [func(bucket_entries(d)) for d in delta_entries_all]
        son_dlt_cus = [func(bucket_entries([e for e in d if is_sonnet(e.model)])) for d in delta_entries_all]

        # --- CUMULATIVE FIT (primary) ---
        cum_limit, cum_resid = fit_cumulative(cum_cus, actual_weekly)
        cum_pred = [(cu / cum_limit * 100) if cum_limit > 0 else 0 for cu in cum_cus]
        cum_errs = [abs(p - a) for p, a in zip(cum_pred, actual_weekly)]
        cum_mae = sum(cum_errs) / len(cum_errs)

        # Sonnet cumulative fit
        son_cum_limit, son_cum_resid = fit_cumulative(son_cum_cus, actual_sonnet)
        son_cum_pred = [(cu / son_cum_limit * 100) if son_cum_limit > 0 else 0 for cu in son_cum_cus]
        son_cum_errs = [abs(p - a) for p, a in zip(son_cum_pred, actual_sonnet)]
        son_cum_mae = sum(son_cum_errs) / len(son_cum_errs)

        # --- DELTA FIT ---
        dlt_limit, dlt_resid = fit_delta(dlt_cus, delta_weekly_pcts)
        # Cross-validate delta fit on cumulative
        dlt_pred = [(cu / dlt_limit * 100) if dlt_limit > 0 else 0 for cu in cum_cus]
        dlt_errs = [abs(p - a) for p, a in zip(dlt_pred, actual_weekly)]
        dlt_mae = sum(dlt_errs) / len(dlt_errs)

        # Sonnet delta fit
        son_dlt_limit, son_dlt_resid = fit_delta(son_dlt_cus, delta_sonnet_pcts)
        son_dlt_pred = [(cu / son_dlt_limit * 100) if son_dlt_limit > 0 else 0 for cu in son_cum_cus]
        son_dlt_errs = [abs(p - a) for p, a in zip(son_dlt_pred, actual_sonnet)]
        son_dlt_mae = sum(son_dlt_errs) / len(son_dlt_errs)

        results[name] = {
            # Cumulative fit
            "cum_limit": cum_limit, "cum_resid": cum_resid,
            "cum_pred": cum_pred, "cum_mae": cum_mae,
            "cum_cus": cum_cus,
            # Delta fit
            "dlt_limit": dlt_limit, "dlt_resid": dlt_resid,
            "dlt_pred": dlt_pred, "dlt_mae": dlt_mae,
            "dlt_cus": dlt_cus,
            # Sonnet cumulative
            "son_cum_limit": son_cum_limit, "son_cum_resid": son_cum_resid,
            "son_cum_pred": son_cum_pred, "son_cum_mae": son_cum_mae,
            "son_cum_cus": son_cum_cus,
            # Sonnet delta
            "son_dlt_limit": son_dlt_limit, "son_dlt_resid": son_dlt_resid,
            "son_dlt_pred": son_dlt_pred, "son_dlt_mae": son_dlt_mae,
        }

    # ── Per-formula detail ───────────────────────────────────────────────
    for name, r in results.items():
        print(f"\n{'─'*90}")
        print(f"  {name}")
        print(f"{'─'*90}")

        print(f"\n  CUMULATIVE FIT (primary):")
        print(f"    Fitted limit:   {r['cum_limit']:>18,.0f}")
        print(f"    Residual SSE:   {r['cum_resid']:>18.2f}")
        print(f"    MAE:            {r['cum_mae']:>18.2f}%")
        print(f"    {'Pt':>4} {'UTC':>6} {'Actual':>7} {'Pred':>7} {'Err':>7} {'CU':>18}")
        for i, (ts, a, _) in enumerate(CAL_POINTS):
            p = r["cum_pred"][i]
            print(f"    {i+1:>4} {ts.strftime('%H:%M'):>6} {a:>6}% {p:>6.1f}% {p-a:>+6.1f}% {r['cum_cus'][i]:>17,.0f}")

        print(f"\n  DELTA FIT (cross-validated on cumulative):")
        print(f"    Fitted limit:   {r['dlt_limit']:>18,.0f}")
        print(f"    Delta SSE:      {r['dlt_resid']:>18.2f}")
        print(f"    Cumul MAE:      {r['dlt_mae']:>18.2f}%")
        print(f"    {'Pt':>4} {'UTC':>6} {'Actual':>7} {'Pred':>7} {'Err':>7}")
        for i, (ts, a, _) in enumerate(CAL_POINTS):
            p = r["dlt_pred"][i]
            print(f"    {i+1:>4} {ts.strftime('%H:%M'):>6} {a:>6}% {p:>6.1f}% {p-a:>+6.1f}%")

        print(f"\n  SONNET (cumulative fit):")
        print(f"    Fitted limit:   {r['son_cum_limit']:>18,.0f}")
        print(f"    MAE:            {r['son_cum_mae']:>18.2f}%")
        print(f"    {'Pt':>4} {'Actual':>7} {'Pred':>7}")
        for i, (ts, _, sa) in enumerate(CAL_POINTS):
            sp = r["son_cum_pred"][i]
            print(f"    {i+1:>4} {sa:>6}% {sp:>6.1f}%")

    # ── Summary table ────────────────────────────────────────────────────
    print()
    print("=" * 90)
    print("COMPARISON TABLE — CUMULATIVE FIT")
    print("=" * 90)
    print()
    hdr = (f"{'Formula':<22} {'Limit':>15} {'SSE':>8} {'MAE':>6}"
           f" {'P1':>6} {'P2':>6} {'P3':>6} {'P4':>6} {'P5':>6}"
           f" {'SonLim':>15} {'SonMAE':>6}")
    print(hdr)
    print("─" * len(hdr))
    for name, r in results.items():
        pp = [f"{p:.1f}" for p in r["cum_pred"]]
        print(f"{name:<22} {r['cum_limit']:>15,.0f} {r['cum_resid']:>8.2f} {r['cum_mae']:>5.2f}%"
              f" {pp[0]:>6} {pp[1]:>6} {pp[2]:>6} {pp[3]:>6} {pp[4]:>6}"
              f" {r['son_cum_limit']:>15,.0f} {r['son_cum_mae']:>5.2f}%")
    print(f"{'Dashboard':>22}" + " " * 30
          + f" {'52.0':>6} {'52.0':>6} {'53.0':>6} {'56.0':>6} {'56.0':>6}"
          + " " * 16 + f"{'5/6/6/6/6':>6}")

    print()
    print("=" * 90)
    print("COMPARISON TABLE — DELTA FIT (cross-validated)")
    print("=" * 90)
    print()
    hdr2 = (f"{'Formula':<22} {'DltLimit':>15} {'DltSSE':>8} {'CumMAE':>7}"
            f" {'P1':>6} {'P2':>6} {'P3':>6} {'P4':>6} {'P5':>6}"
            f" {'SonDltLim':>15} {'SonMAE':>6}")
    print(hdr2)
    print("─" * len(hdr2))
    for name, r in results.items():
        pp = [f"{p:.1f}" for p in r["dlt_pred"]]
        son_dl = r["son_dlt_limit"]
        son_dl_s = f"{son_dl:>15,.0f}" if son_dl > 0 else f"{'N/A':>15}"
        print(f"{name:<22} {r['dlt_limit']:>15,.0f} {r['dlt_resid']:>8.2f} {r['dlt_mae']:>6.2f}%"
              f" {pp[0]:>6} {pp[1]:>6} {pp[2]:>6} {pp[3]:>6} {pp[4]:>6}"
              f" {son_dl_s} {r['son_dlt_mae']:>5.2f}%")
    print(f"{'Dashboard':>22}" + " " * 31
          + f"{'52.0':>6} {'52.0':>6} {'53.0':>6} {'56.0':>6} {'56.0':>6}")

    # ── Winner ───────────────────────────────────────────────────────────
    # Primary metric: cumulative MAE
    best_cum = min(results.items(), key=lambda x: x[1]["cum_mae"])
    # Secondary: delta fit that also cross-validates well
    best_dlt = min(results.items(), key=lambda x: x[1]["dlt_mae"])

    print()
    print("=" * 90)
    print("WINNER ANALYSIS")
    print("=" * 90)
    print()
    print(f"  Best by cumulative fit MAE:  {best_cum[0]}")
    print(f"    Weekly limit:  {best_cum[1]['cum_limit']:,.0f}")
    print(f"    MAE:           {best_cum[1]['cum_mae']:.2f}%")
    print(f"    Sonnet limit:  {best_cum[1]['son_cum_limit']:,.0f}")
    print(f"    Sonnet MAE:    {best_cum[1]['son_cum_mae']:.2f}%")
    print()
    print(f"  Best by delta cross-val MAE: {best_dlt[0]}")
    print(f"    Weekly limit (delta):  {best_dlt[1]['dlt_limit']:,.0f}")
    print(f"    Cumul MAE:             {best_dlt[1]['dlt_mae']:.2f}%")
    print()

    # Ranking
    print("  Ranking by CUMULATIVE FIT MAE:")
    for rank, (n, r) in enumerate(sorted(results.items(), key=lambda x: x[1]["cum_mae"]), 1):
        m = " <<<" if n == best_cum[0] else ""
        print(f"    {rank}. {n:<25} MAE={r['cum_mae']:.2f}%  SSE={r['cum_resid']:.2f}{m}")

    print()
    print("  Ranking by DELTA FIT cross-validation MAE:")
    for rank, (n, r) in enumerate(sorted(results.items(), key=lambda x: x[1]["dlt_mae"]), 1):
        m = " <<<" if n == best_dlt[0] else ""
        print(f"    {rank}. {n:<25} MAE={r['dlt_mae']:.2f}%  DltSSE={r['dlt_resid']:.2f}{m}")

    # Diagnosis
    winner_name = best_cum[0]
    w = best_cum[1]
    print()
    print("─" * 90)
    print("INTERPRETATION")
    print("─" * 90)
    print()
    print(f"  The cumulative fit is the reliable signal: 5 data points with absolute %")
    print(f"  readings give a direct LIMIT estimate. The delta fit suffers from only")
    print(f"  2 non-zero windows (1% and 3%), making it underdetermined.")
    print()

    # Check agreement between cumul and delta
    for n, r in results.items():
        ratio = r["dlt_limit"] / r["cum_limit"] if r["cum_limit"] > 0 else 0
        print(f"  {n:<22}  cum_limit={r['cum_limit']:>15,.0f}  dlt_limit={r['dlt_limit']:>15,.0f}  ratio={ratio:.2f}x")
    print()
    print(f"  If delta_limit >> cum_limit, the marginal rate in the observed windows")
    print(f"  is lower than the average rate since reset (heavy usage happened earlier).")
    print()

    # ── Save best calibration ────────────────────────────────────────────
    print("=" * 90)
    print("SAVING CALIBRATION (cumulative fit winner)")
    print("=" * 90)
    print()

    store = CalibrationStore()
    winner_func = FORMULAS[winner_name]

    for i, (ts, wpct, spct) in enumerate(CAL_POINTS):
        # Weekly-all
        cum = entries_up_to(weekly, ts)
        bk = bucket_entries(cum)
        cu = winner_func(bk)
        total_tok = sum(e.input_tokens + e.output_tokens + e.cache_creation_tokens + e.cache_read_tokens for e in cum)
        snapshot = {
            fam: TokenSnapshot(
                input_tokens=b.input_tokens, output_tokens=b.output_tokens,
                cache_creation_tokens=b.cache_creation, cache_read_tokens=b.cache_read,
            ) for fam, b in bk.items()
        }
        store.save_calibration(
            percent=float(wpct), pool="weekly-all", tokens_consumed=total_tok,
            reset_time=WEEKLY_RESET, timestamp=ts, token_snapshot=snapshot, compute_units=cu,
        )
        print(f"  weekly-all   P{i+1}: {wpct}% at {ts.strftime('%H:%M')} UTC  CU={cu:>15,.0f}")

        # Weekly-sonnet
        cum_s = [e for e in cum if is_sonnet(e.model)]
        bk_s = bucket_entries(cum_s)
        cu_s = winner_func(bk_s)
        total_tok_s = sum(e.input_tokens + e.output_tokens + e.cache_creation_tokens + e.cache_read_tokens for e in cum_s)
        snap_s = {
            fam: TokenSnapshot(
                input_tokens=b.input_tokens, output_tokens=b.output_tokens,
                cache_creation_tokens=b.cache_creation, cache_read_tokens=b.cache_read,
            ) for fam, b in bk_s.items()
        }
        store.save_calibration(
            percent=float(spct), pool="weekly-sonnet", tokens_consumed=total_tok_s,
            reset_time=WEEKLY_RESET, timestamp=ts, token_snapshot=snap_s, compute_units=cu_s,
        )
        print(f"  weekly-sonnet P{i+1}: {spct}% at {ts.strftime('%H:%M')} UTC  CU={cu_s:>15,.0f}")

    print()
    est_all = store.estimate_compute_limit("weekly-all")
    est_son = store.estimate_compute_limit("weekly-sonnet")
    print(f"  Store weekly-all CU limit estimate:    {est_all:,.0f}" if est_all else "  No weekly-all estimate")
    print(f"  Store weekly-sonnet CU limit estimate: {est_son:,.0f}" if est_son else "  No weekly-sonnet estimate")
    print()
    print("Done.")


if __name__ == "__main__":
    main()
