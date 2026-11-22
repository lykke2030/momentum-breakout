#!/usr/bin/env python3
"""
vtg_edge.py - Intraday Momentum -> Base -> Contraction -> Breakout backtester on Alpaca data.

Rebuilds the logic of the "VTG Setup" idea on intraday bars and measures whether the
breakout signal has an edge versus a baseline, out of sample, after costs.

  1. Prior momentum   : gap-up on heavy relative volume, a thrust bar, or orderly persistent buying
  2. Base/contraction : pullback that holds, shrinking ranges (ATR contraction), EMA compression,
                        tight closes, fast EMAs converging, holding above EMA21, optional inside bar
  3. Breakout         : range expansion out of the contraction, closing near the bar's high

Signals are confirmed on a bar's close and entered at the NEXT bar's open (no lookahead).
Relative volume is measured against the same time of day on previous days.

Setup
-----
  pip install alpaca-py pandas numpy
  export ALPACA_API_KEY=...   ALPACA_SECRET_KEY=...      (Windows: set / $env:)

Examples
--------
  python vtg_edge.py --symbols GRAL,CAI,PICS,RGC,KOD --days 365
  python vtg_edge.py --universe-file tickers.txt --days 365 --ablate
  python vtg_edge.py --csv-dir mydata/              # your own CSVs: timestamp,open,high,low,close,volume

Outputs go to ./results/: trades.csv, summary.md, ablation.csv (with --ablate).
"""
from __future__ import annotations

import argparse
import math
import os
import sys
from dataclasses import dataclass, asdict, replace
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

NY = "America/New_York"


# ----------------------------------------------------------------------------- parameters
@dataclass(frozen=True)
class Params:
    timeframe_min: int = 5
    atr_len: int = 14
    ema_fast: int = 4
    ema_mid: int = 9
    ema_slow: int = 21
    rvol_days: int = 20              # time-of-day relative volume lookback (days)
    tod_adjust: bool = True          # scale ATR up to the usual range for that time of day

    # Daily gate (point-in-time "1M momentum" universe, uses PRIOR day only)
    use_daily_gate: bool = True
    daily_ret_lookback: int = 21     # trading days
    daily_min_ret: float = 0.10      # prior 1-month return >= 10%
    daily_above_ema: bool = True     # prior close above daily 21 EMA

    # Stage 1: momentum
    require_momentum: bool = True
    gap_min_pct: float = 0.03
    gap_min_rvol: float = 2.0
    thrust_min_atr: float = 1.5
    thrust_min_rvol: float = 2.0
    thrust_close_pos: float = 0.75
    persist_bars: int = 6
    persist_min_up: int = 5
    persist_min_move_atr: float = 2.0
    momentum_expiry_bars: int = 30   # base must resolve within N bars of the momentum high

    # Stage 2: base + contraction
    pullback_min_atr: float = 0.5
    max_retrace: float = 0.618
    contr_bars: int = 3
    require_contraction: bool = True
    contr_range_max_atr: float = 0.7  # mean range of contraction bars vs ATR
    tight_close_max_atr: float = 0.6  # close-to-close spread of contraction bars vs ATR
    ema_compress_max_atr: float = 0.5 # |EMA4 - EMA9| vs ATR (fast EMAs converge)
    require_inside_bar: bool = False

    # Stage 3: breakout
    bo_range_mult: float = 1.5        # breakout range vs mean contraction range
    bo_close_pos: float = 0.75        # close in top 25% of bar
    bo_min_rvol: float = 1.0

    # Session / execution
    first_entry: str = "09:45"
    last_entry: str = "15:30"
    skip_midday: bool = False         # skip 11:30-14:00 ET
    target_r: float = 2.0
    slippage_bps: float = 5.0         # per side
    min_risk_pct: float = 0.001       # ignore setups with stop closer than 0.1%


# ----------------------------------------------------------------------------- data
def fetch_alpaca(symbols, start, end, tf_min, feed, cache_dir):
    """Download regular-session bars from Alpaca with a simple per-symbol CSV cache."""
    try:
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
        from alpaca.data.enums import DataFeed, Adjustment
    except ImportError:
        sys.exit("Install alpaca-py first:  pip install alpaca-py")

    key = os.environ.get("ALPACA_API_KEY") or os.environ.get("APCA_API_KEY_ID")
    sec = os.environ.get("ALPACA_SECRET_KEY") or os.environ.get("APCA_API_SECRET_KEY")
    if not key or not sec:
        sys.exit("Set ALPACA_API_KEY and ALPACA_SECRET_KEY environment variables.")
    client = StockHistoricalDataClient(key, sec)
    os.makedirs(cache_dir, exist_ok=True)

    out = {}
    tag = f"{tf_min}m_{feed}_{start:%Y%m%d}_{end:%Y%m%d}"
    todo = []
    for s in symbols:
        path = os.path.join(cache_dir, f"{s}_{tag}.csv.gz")
        if os.path.exists(path):
            out[s] = pd.read_csv(path, parse_dates=["timestamp"]).set_index("timestamp")
        else:
            todo.append(s)

    for i in range(0, len(todo), 20):
        batch = todo[i:i + 20]
        print(f"  downloading {', '.join(batch)} ...", flush=True)
        req = StockBarsRequest(
            symbol_or_symbols=batch,
            timeframe=TimeFrame(tf_min, TimeFrameUnit.Minute),
            start=start, end=end,
            feed=DataFeed.SIP if feed == "sip" else DataFeed.IEX,
            adjustment=Adjustment.SPLIT,
        )
        df = client.get_stock_bars(req).df
        if df.empty:
            continue
        df = df.reset_index()
        for s, g in df.groupby("symbol"):
            g = g[["timestamp", "open", "high", "low", "close", "volume"]]
            g.to_csv(os.path.join(cache_dir, f"{s}_{tag}.csv.gz"), index=False)
            out[s] = g.set_index("timestamp")
    return out


def load_csv_dir(path, tf_min=5):
    """Load CSVs named SYMBOL_*.csv(.gz); files for the same symbol (e.g. one per year) are joined.
    Bars finer than tf_min (e.g. 1-minute files) are resampled to tf_min."""
    parts = {}
    for f in sorted(os.listdir(path)):
        if f.endswith((".csv", ".csv.gz")):
            sym = f.split(".")[0].split("_")[0].upper()
            df = pd.read_csv(os.path.join(path, f), parse_dates=["timestamp"])
            parts.setdefault(sym, []).append(df)
    out = {}
    for sym, dfs in parts.items():
        df = pd.concat(dfs).drop_duplicates("timestamp")
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
        df = df.set_index("timestamp").sort_index()
        step = df.index.to_series().diff().median()
        if pd.notna(step) and step < pd.Timedelta(minutes=tf_min):
            df = df.resample(f"{tf_min}min", label="left", closed="left").agg(
                {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}).dropna()
        out[sym] = df
    return out


def to_session(df):
    """Convert to New York time and keep the regular session only."""
    df = df.copy()
    idx = pd.to_datetime(df.index, utc=True).tz_convert(NY)
    df.index = idx
    t = idx.strftime("%H:%M")
    df = df[(t >= "09:30") & (t < "16:00")]
    df = df[~df.index.duplicated()].sort_index()
    return df[["open", "high", "low", "close", "volume"]].astype(float)


# ----------------------------------------------------------------------------- features
def add_features(df, p: Params):
    df = df.copy()
    df["date"] = df.index.date
    df["tod"] = df.index.strftime("%H:%M")
    new_day = df["date"] != df["date"].shift(1)
    df["new_day"] = new_day

    prev_c = df["close"].shift(1).where(~new_day)          # no cross-day gap in TR
    rng = df["high"] - df["low"]
    tr = pd.concat([rng, (df["high"] - prev_c).abs(), (df["low"] - prev_c).abs()], axis=1).max(axis=1)
    df["range"] = rng
    df["atr"] = tr.ewm(alpha=1 / p.atr_len, adjust=False).mean()
    for n, name in [(p.ema_fast, "ema_f"), (p.ema_mid, "ema_m"), (p.ema_slow, "ema_s")]:
        df[name] = df["close"].ewm(span=n, adjust=False).mean()
    df["close_pos"] = np.where(rng > 0, (df["close"] - df["low"]) / rng, 0.5)

    # Typical range for this time of day (previous N days only); morning bars are naturally bigger
    gr = df.groupby("tod")["range"]
    df["slot_range"] = gr.shift(1).groupby(df["tod"]).rolling(p.rvol_days, min_periods=5).mean() \
                         .reset_index(level=0, drop=True).reindex(df.index)

    # Time-of-day relative volume: same slot, previous N days only
    g = df.groupby("tod")["volume"]
    slot_avg = g.shift(1).groupby(df["tod"]).rolling(p.rvol_days, min_periods=5).mean() \
                .reset_index(level=0, drop=True).reindex(df.index)
    df["rvol"] = df["volume"] / slot_avg

    # Daily context (prior day only)
    daily = df.groupby("date").agg(d_open=("open", "first"), d_close=("close", "last"))
    daily["prev_close"] = daily["d_close"].shift(1)
    daily["gap"] = daily["d_open"] / daily["prev_close"] - 1
    daily["ret_lb"] = (daily["d_close"] / daily["d_close"].shift(p.daily_ret_lookback) - 1).shift(1)
    daily["ema21"] = daily["d_close"].ewm(span=21, adjust=False).mean()
    daily["above_ema"] = (daily["d_close"] > daily["ema21"]).shift(1)
    gate = daily["ret_lb"] >= p.daily_min_ret
    if p.daily_above_ema:
        gate &= daily["above_ema"].fillna(False).astype(bool)
    daily["gate"] = gate.fillna(False) if p.use_daily_gate else daily["ret_lb"].notna()
    df = df.join(daily[["gap", "ret_lb", "gate"]], on="date")
    return df


# ----------------------------------------------------------------------------- detection
def detect(df, p: Params, symbol: str):
    """Walk bars day by day with a small state machine. Returns list of signal dicts."""
    o, h, l, c = (df[k].to_numpy() for k in ("open", "high", "low", "close"))
    rng, atr, rvol = df["range"].to_numpy(), df["atr"].to_numpy(), df["rvol"].to_numpy()
    ema_f, ema_m, ema_s = df["ema_f"].to_numpy(), df["ema_m"].to_numpy(), df["ema_s"].to_numpy()
    cpos, tod = df["close_pos"].to_numpy(), df["tod"].to_numpy()
    new_day, gap, gate = df["new_day"].to_numpy(), df["gap"].to_numpy(), df["gate"].to_numpy()
    ret_lb = df["ret_lb"].to_numpy()
    srng = df["slot_range"].to_numpy()
    dates = df["date"].to_numpy()
    n, K = len(df), p.contr_bars
    sigs = []

    mom = None  # dict(type, leg_low, high, high_idx)
    day_start = 0
    for i in range(1, n):
        if new_day[i]:
            mom, day_start = None, i
        a = atr[i - 1]
        if not np.isfinite(a) or a <= 0:
            continue
        if p.tod_adjust and np.isfinite(srng[i]):
            a = max(a, srng[i])          # volatility unit = larger of ATR and usual range at this time of day
        rv = rvol[i] if np.isfinite(rvol[i]) else np.nan

        # ---------------- stage 3 check first (uses bars before i for the base)
        if i - day_start >= K + 1:
            w = slice(i - K, i)
            in_base = True
            base_ok = not p.require_momentum
            retrace = np.nan
            if mom is not None:
                in_base = mom["high_idx"] < i - K  # contraction bars all after momentum high
                base_low = l[mom["high_idx"] + 1:i].min() if mom["high_idx"] + 1 < i else np.inf
                leg = mom["high"] - mom["leg_low"]
                pulled = mom["high"] - base_low >= p.pullback_min_atr * a
                retrace = (mom["high"] - base_low) / leg if leg > 0 else np.nan
                fresh = i - mom["high_idx"] <= p.momentum_expiry_bars
                base_ok = in_base and pulled and fresh and (retrace <= p.max_retrace)

            mean_r = rng[w].mean()
            contr_ok = (mean_r <= p.contr_range_max_atr * a
                        and c[w].max() - c[w].min() <= p.tight_close_max_atr * a
                        and abs(ema_f[i - 1] - ema_m[i - 1]) <= p.ema_compress_max_atr * a
                        and c[i - 1] >= ema_s[i - 1])
            inside = any(h[j] <= h[j - 1] and l[j] >= l[j - 1] for j in range(i - K, i) if j > day_start)
            if p.require_inside_bar:
                contr_ok = contr_ok and inside
            if not p.require_contraction:
                contr_ok = True

            t = tod[i]
            time_ok = p.first_entry <= t <= p.last_entry and not (p.skip_midday and "11:30" <= t < "14:00")
            bo = (c[i] > h[w].max() and rng[i] >= p.bo_range_mult * max(mean_r, 1e-9)
                  and cpos[i] >= p.bo_close_pos and c[i] > o[i]
                  and (not np.isfinite(rv) or rv >= p.bo_min_rvol))

            if bo and base_ok and contr_ok and time_ok and gate[i] and i + 1 < n and not new_day[i + 1]:
                sigs.append(dict(
                    symbol=symbol, sig_idx=i, time=df.index[i], date=dates[i], tod=t,
                    mom_type=(mom or {}).get("type", "none"),
                    stop=l[w].min(), atr=a, rvol=rv, inside=inside,
                    retrace=retrace, contr_ratio=mean_r / a, gap=gap[i], daily_ret=ret_lb[i],
                    bo_range_atr=rng[i] / a,
                ))
                # breakout continues momentum: new leg starts from the contraction low
                mom = dict(type=(mom or {}).get("type", "none"), leg_low=l[w].min(), high=h[i], high_idx=i)
                continue

        # ---------------- stage 1: momentum detection / extension
        trig = None
        if new_day[i - 1] and i - 1 == day_start and np.isfinite(gap[i - 1]) and gap[i - 1] >= p.gap_min_pct \
                and np.isfinite(rvol[i - 1]) and rvol[i - 1] >= p.gap_min_rvol:
            trig = "gap"  # evaluated once the opening bar has closed
        if trig is None and rng[i] >= p.thrust_min_atr * a and cpos[i] >= p.thrust_close_pos and c[i] > o[i] \
                and np.isfinite(rv) and rv >= p.thrust_min_rvol:
            trig = "thrust"
        P = p.persist_bars
        if trig is None and i - day_start >= P:
            seg = c[i - P:i + 1]
            ups = int((np.diff(seg) > 0).sum())
            if ups >= p.persist_min_up and c[i] - c[i - P] >= p.persist_min_move_atr * a \
                    and c[i] > ema_m[i] > ema_s[i] and rng[i - P + 1:i + 1].max() <= 1.5 * a:
                trig = "persist"

        if trig is not None and mom is None:
            lo = l[max(day_start, i - P):i + 1].min()
            if trig == "gap":
                lo = min(lo, o[day_start] / (1 + gap[day_start]))   # leg starts at prior close
            hi_idx = day_start + int(np.argmax(h[day_start:i + 1]))
            mom = dict(type=trig, leg_low=lo, high=h[hi_idx], high_idx=hi_idx)
        if mom is not None and h[i] > mom["high"]:
            mom["high"], mom["high_idx"] = h[i], i   # new high: momentum still running, base restarts
        if mom is not None and mom["high_idx"] < i:
            base_low = l[mom["high_idx"] + 1:i + 1].min()   # pullback = bars AFTER the high
            leg = mom["high"] - mom["leg_low"]
            if leg > 0 and (mom["high"] - base_low) / leg > p.max_retrace:
                mom = None                            # pullback too deep: setup failed
            elif i - mom["high_idx"] > p.momentum_expiry_bars:
                mom = None
    return sigs


# ----------------------------------------------------------------------------- simulation
def simulate(df, sigs, p: Params):
    o, h, l, c = (df[k].to_numpy() for k in ("open", "high", "low", "close"))
    ema_m, atr = df["ema_m"].to_numpy(), df["atr"].to_numpy()
    new_day = df["new_day"].to_numpy()
    n, slip = len(df), p.slippage_bps / 1e4
    trades, busy_until = [], -1
    for s in sigs:
        i = s["sig_idx"]
        if i < busy_until:
            continue
        e_idx = i + 1
        entry = o[e_idx] * (1 + slip)
        stop = s["stop"]
        risk = entry - stop
        if risk <= entry * p.min_risk_pct:
            continue
        target = entry + p.target_r * risk
        end = e_idx
        while end + 1 < n and not new_day[end + 1]:
            end += 1

        def run(trail):
            for j in range(e_idx, end + 1):
                if l[j] <= stop:
                    px = min(o[j], stop) if j > e_idx else stop
                    return px * (1 - slip), j, "stop"
                if not trail and h[j] >= target:
                    return max(o[j], target) * (1 - slip) if j > e_idx else target * (1 - slip), j, "target"
                if trail and j > e_idx and c[j] < ema_m[j]:
                    return c[j] * (1 - slip), j, "trail"
            return c[end] * (1 - slip), end, "eod"

        x1, j1, why1 = run(False)
        x2, j2, why2 = run(True)
        seg_h, seg_l = h[e_idx:end + 1].max(), l[e_idx:end + 1].min()
        a = s["atr"]
        fwd = {f"fwd{k}_atr": (c[min(e_idx + k, end)] - entry) / a for k in (3, 6, 12)}
        trades.append({**s, "entry_time": df.index[e_idx], "entry": entry, "risk_pct": risk / entry,
                       "r_fixed": (x1 - entry) / risk, "exit_fixed": why1,
                       "r_trail": (x2 - entry) / risk, "exit_trail": why2,
                       "mfe_r": (seg_h - entry) / risk, "mae_r": (seg_l - entry) / risk,
                       "eod_atr": (c[end] - entry) / a, **fwd})
        busy_until = max(j1, j2) + 1
    return trades


def baseline(df, p: Params):
    """Forward returns (in ATR) from every eligible bar: same hours, same daily gate, long at next open."""
    n = len(df)
    c, o = df["close"].to_numpy(), df["open"].to_numpy()
    atr_prev = df["atr"].shift(1).to_numpy()
    nd = df["new_day"].to_numpy()
    tod, gate = df["tod"].to_numpy(), df["gate"].fillna(False).astype(bool).to_numpy()
    day_id = np.cumsum(nd)
    last = pd.Series(np.arange(n)).groupby(day_id).transform("max").to_numpy()
    idx = np.arange(n - 1)
    ok = gate[:-1] & (tod[:-1] >= p.first_entry) & (tod[:-1] <= p.last_entry) & ~nd[1:] \
        & np.isfinite(atr_prev[:-1]) & (atr_prev[:-1] > 0)
    idx = idx[ok]
    slip = p.slippage_bps / 1e4
    e = o[idx + 1] * (1 + slip)
    a = atr_prev[idx]
    return {"fwd6_atr": (c[np.minimum(idx + 7, last[idx])] - e) / a,
            "eod_atr": (c[last[idx]] * (1 - slip) - e) / a}


# ----------------------------------------------------------------------------- stats
def rstats(r):
    r = np.asarray(r, float)
    r = r[np.isfinite(r)]
    if len(r) == 0:
        return dict(n=0, win=np.nan, avg_r=np.nan, pf=np.nan, t=np.nan, total_r=0.0)
    wins, losses = r[r > 0].sum(), -r[r < 0].sum()
    sd = r.std(ddof=1) if len(r) > 1 else np.nan
    return dict(n=len(r), win=(r > 0).mean(), avg_r=r.mean(),
                pf=wins / losses if losses > 0 else np.inf,
                t=r.mean() / (sd / math.sqrt(len(r))) if sd and sd > 0 else np.nan,
                total_r=r.sum())


def fmt_table(rows, cols):
    head = "| " + " | ".join(cols) + " |\n|" + "---|" * len(cols) + "\n"
    def f(v):
        if isinstance(v, float):
            return "nan" if not np.isfinite(v) else f"{v:.2f}"
        return str(v)
    return head + "".join("| " + " | ".join(f(r.get(k, "")) for k in cols) + " |\n" for r in rows)


_FEAT_CACHE = {}
FEATURE_KEYS = ("atr_len", "ema_fast", "ema_mid", "ema_slow", "rvol_days", "use_daily_gate",
                "daily_ret_lookback", "daily_min_ret", "daily_above_ema")


def run_all(data, p: Params, verbose=True):
    all_trades, base = [], {"fwd6_atr": [], "eod_atr": []}
    fkey = tuple(getattr(p, k) for k in FEATURE_KEYS)
    for sym, raw in data.items():
        ck = (sym, fkey)
        if ck not in _FEAT_CACHE:
            df = to_session(raw)
            _FEAT_CACHE[ck] = add_features(df, p) if len(df) >= 200 else None
        df = _FEAT_CACHE[ck]
        if df is None:
            continue
        sigs = detect(df, p, sym)
        all_trades += simulate(df, sigs, p)
        b = baseline(df, p)
        for k in base:
            base[k].append(b[k])
        if verbose:
            print(f"  {sym:6s} bars={len(df):6d} signals={len(sigs):4d}")
    base = {k: np.concatenate(v) if v else np.array([]) for k, v in base.items()}
    return pd.DataFrame(all_trades), base


def tod_bucket(t):
    if t < "10:30": return "1 open 9:45-10:30"
    if t < "11:30": return "2 morning 10:30-11:30"
    if t < "14:00": return "3 midday 11:30-14:00"
    return "4 afternoon 14:00-15:30"


def summarize(tr, base, p: Params, oos_frac):
    lines = ["# VTG intraday breakout — edge report", ""]
    if tr.empty:
        return "\n".join(lines + ["No trades. Loosen parameters or widen the universe / date range."])
    tr = tr.sort_values("entry_time").reset_index(drop=True)
    dates = sorted(tr["date"].unique())
    cut = dates[int(len(dates) * (1 - oos_frac))] if len(dates) > 4 else dates[-1]
    tr["sample"] = np.where(tr["date"] < cut, "in-sample", "out-of-sample")
    tr["tod_bucket"] = tr["tod"].map(tod_bucket)

    lines += [f"Trades: **{len(tr)}** across {tr['symbol'].nunique()} symbols, "
              f"{dates[0]} → {dates[-1]}. Out-of-sample starts {cut}. "
              f"Costs: {p.slippage_bps} bps per side.", ""]

    lines += ["## 1. Does the signal beat a random entry? (forward move in ATRs)", "",
              "Baseline = going long at every eligible bar (same hours, same daily gate).", ""]
    rows = []
    for name, sig_v, base_v in [("next 6 bars", tr["fwd6_atr"], base["fwd6_atr"]),
                                ("to close", tr["eod_atr"], base["eod_atr"])]:
        rows.append({"horizon": name, "signal_avg": float(np.nanmean(sig_v)),
                     "baseline_avg": float(np.nanmean(base_v)) if len(base_v) else np.nan,
                     "signal_n": int(sig_v.notna().sum()), "baseline_n": len(base_v)})
    lines += [fmt_table(rows, ["horizon", "signal_avg", "baseline_avg", "signal_n", "baseline_n"]), ""]

    lines += ["## 2. Trade results (R = multiples of initial risk)", "",
              f"Fixed: stop at contraction low, target {p.target_r}R, else exit at close. "
              "Trail: stop, then exit on a close below EMA9.", ""]
    rows = []
    for samp in ["in-sample", "out-of-sample", "all"]:
        sub = tr if samp == "all" else tr[tr["sample"] == samp]
        for col in ["r_fixed", "r_trail"]:
            st = rstats(sub[col]); st.update(sample=samp, exit=col[2:])
            rows.append(st)
    lines += [fmt_table(rows, ["sample", "exit", "n", "win", "avg_r", "pf", "t", "total_r"]), "",
              "Read: avg_r > 0 with t > 2 in BOTH samples is the bar for a real edge. "
              "In-sample only = probably curve fit.", ""]

    lines += ["## 3. Where is the edge? (r_fixed, all trades)", ""]
    tr["rvol_bucket"] = pd.cut(tr["rvol"], [0, 1.5, 3, 1e9], labels=["<1.5x", "1.5-3x", ">3x"]).astype(str)
    tr["gap_day"] = np.where(tr["gap"] >= p.gap_min_pct, "gap day", "no gap")
    tr["retrace_bucket"] = pd.cut(tr["retrace"], [-1, 0.25, 0.4, 1], labels=["<25%", "25-40%", ">40%"]).astype(str)
    for col, title in [("tod_bucket", "Time of day"), ("mom_type", "Momentum type"),
                       ("inside", "Inside bar in contraction"), ("rvol_bucket", "Breakout relative volume"),
                       ("gap_day", "Gap day"), ("retrace_bucket", "Pullback depth")]:
        rows = []
        for k, g in tr.groupby(col):
            st = rstats(g["r_fixed"]); st[col] = k
            st["oos_avg_r"] = rstats(g.loc[g["sample"] == "out-of-sample", "r_fixed"])["avg_r"]
            rows.append(st)
        lines += [f"### {title}", "", fmt_table(rows, [col, "n", "win", "avg_r", "pf", "t", "oos_avg_r"]), ""]

    lines += ["## 4. Excursions", "",
              f"Median MFE {tr['mfe_r'].median():.2f}R, median MAE {tr['mae_r'].median():.2f}R. "
              f"Share reaching 1R: {(tr['mfe_r'] >= 1).mean():.0%}, 2R: {(tr['mfe_r'] >= 2).mean():.0%}, "
              f"3R: {(tr['mfe_r'] >= 3).mean():.0%}.", "",
              "If many trades reach 1R but few reach 2R, a smaller target or partial exit may fit better.", ""]

    lines += ["## Caveats", "",
              "- A ticker list chosen today (e.g. from a current momentum screen) has hindsight bias. "
              "The daily gate makes each day's eligibility point-in-time, but the list itself still matters: "
              "prefer a broad, liquid universe.",
              "- Groups with fewer than ~30 trades are noise.",
              "- Trying many parameter sets and keeping the best one inflates results. Judge by out-of-sample.", ""]
    return "\n".join(lines)


ABLATIONS = {
    "full setup": {},
    "no momentum requirement": {"require_momentum": False},
    "no contraction requirement": {"require_contraction": False},
    "breakout only (no momentum, no contraction)": {"require_momentum": False, "require_contraction": False},
    "require inside bar": {"require_inside_bar": True},
    "no daily gate": {"use_daily_gate": False},
    "skip midday": {"skip_midday": True},
    "no time-of-day range adjustment": {"tod_adjust": False},
    "stronger breakout volume (rvol>=2)": {"bo_min_rvol": 2.0},
    "target 1.5R": {"target_r": 1.5},
    "target 3R": {"target_r": 3.0},
}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--symbols", help="comma-separated tickers")
    ap.add_argument("--universe-file", help="text file, one ticker per line")
    ap.add_argument("--csv-dir", help="use local CSVs instead of Alpaca")
    ap.add_argument("--days", type=int, default=365, help="test period in calendar days")
    ap.add_argument("--timeframe", type=int, default=5, choices=[1, 2, 3, 5, 10, 15, 30])
    ap.add_argument("--feed", default="sip", choices=["sip", "iex"])
    ap.add_argument("--oos", type=float, default=0.3, help="fraction of days held out (latest)")
    ap.add_argument("--slippage-bps", type=float, default=5.0)
    ap.add_argument("--ablate", action="store_true", help="test which components carry the edge")
    ap.add_argument("--out", default="results")
    ap.add_argument("--cache", default="data_cache")
    args = ap.parse_args()

    p = Params(timeframe_min=args.timeframe, slippage_bps=args.slippage_bps)
    if args.csv_dir:
        data = load_csv_dir(args.csv_dir, args.timeframe)
    else:
        syms = []
        if args.symbols:
            syms += [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
        if args.universe_file:
            syms += [s.strip().upper() for s in open(args.universe_file) if s.strip() and not s.startswith("#")]
        if not syms:
            sys.exit("Give --symbols, --universe-file or --csv-dir")
        end = datetime.now(timezone.utc) - timedelta(minutes=20)   # free plan: SIP needs >15 min delay
        start = end - timedelta(days=args.days + 75)                # + warm-up for daily gate / RVOL
        start = start.replace(hour=0, minute=0, second=0, microsecond=0)
        end = end.replace(second=0, microsecond=0)
        print(f"Fetching {len(syms)} symbols, {args.timeframe}-min bars, {args.feed.upper()} feed")
        data = fetch_alpaca(sorted(set(syms)), start, end, args.timeframe, args.feed, args.cache)

    os.makedirs(args.out, exist_ok=True)
    print("Running full setup")
    tr, base = run_all(data, p)
    report = summarize(tr, base, p, args.oos)
    if not tr.empty:
        tr.to_csv(os.path.join(args.out, "trades.csv"), index=False)

    if args.ablate:
        rows = []
        for name, change in ABLATIONS.items():
            q = replace(p, **change)
            t, _ = (tr, base) if not change else run_all(data, q, verbose=False)
            if t.empty:
                rows.append({"variant": name, "n": 0}); continue
            t = t.sort_values("entry_time")
            ds = sorted(t["date"].unique())
            cut = ds[int(len(ds) * (1 - args.oos))] if len(ds) > 4 else ds[-1]
            a, b = rstats(t["r_fixed"]), rstats(t.loc[t["date"] >= cut, "r_fixed"])
            rows.append({"variant": name, "n": a["n"], "win": a["win"], "avg_r": a["avg_r"], "pf": a["pf"],
                         "t": a["t"], "oos_n": b["n"], "oos_avg_r": b["avg_r"]})
            print(f"  {name:45s} n={a['n']:5d} avgR={a['avg_r']:+.3f}")
        pd.DataFrame(rows).to_csv(os.path.join(args.out, "ablation.csv"), index=False)
        report += "\n## 5. Ablation: which part carries the edge? (r_fixed)\n\n" + \
                  fmt_table(rows, ["variant", "n", "win", "avg_r", "pf", "t", "oos_n", "oos_avg_r"]) + \
                  "\nIf 'breakout only' does as well as 'full setup', the momentum/base filters add nothing.\n"

    with open(os.path.join(args.out, "summary.md"), "w") as f:
        f.write(report + "\n\nParameters:\n\n```\n" + "\n".join(f"{k} = {v}" for k, v in asdict(p).items()) + "\n```\n")
    print("\n" + report)
    print(f"\nSaved to {args.out}/")


if __name__ == "__main__":
    main()
