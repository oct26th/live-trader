"""Momentum rotation backtest with BTC MA200 filter.

Strategy:
- Universe: 20 coins (SYMBOL_MAP).
- Every Sunday UTC 00:00: score each coin by past 20-day log return.
- Hold top 3 equally weighted.
- Individual stop-loss: -15% from entry (checked every bar, not just rebalance).
- Market filter: BTC 1D close > MA200 (1D) required for any new position.
  If filter fails mid-week, liquidate all holdings and stay flat.
"""
import os
import pickle
import sys
import time
from datetime import datetime, timezone

import ccxt
import numpy as np

from config import SYMBOL_MAP

# ── Variant A parameters (tuned after initial run) ───────────────────────────
LOOKBACK_DAYS = 30          # was 20 — filter short-term speculation noise
TOP_K = 5                   # was 3 — dilute single-name concentration
INDIVIDUAL_SL = -0.10       # was -0.15 — faster exit on alt dumps
VOL_LOOKBACK_DAYS = 30      # window for daily-return std
VOL_CAP = 0.08              # exclude coins with 30d daily σ > 8% (trash alts)
TAKER_FEE = 0.006
INITIAL_CASH = 1000.0
CACHE_PATH = "/tmp/backtest_1d_cache.pkl"
MA200_PERIOD = 200


def fetch_1d(symbol: str, start_ms: int, end_ms: int) -> np.ndarray:
    ex = ccxt.binance({"enableRateLimit": True})
    bars: list = []
    since = start_ms
    while since < end_ms:
        chunk = ex.fetch_ohlcv(symbol, "1d", since=since, limit=1000)
        if not chunk:
            break
        bars.extend(chunk)
        next_since = chunk[-1][0] + 1
        if next_since <= since:
            break
        since = next_since
        time.sleep(0.15)
    return np.array([b for b in bars if b[0] < end_ms], dtype=float)


def load_all_data(symbols: list[str], start_date: str, end_date: str) -> dict[str, np.ndarray]:
    """Load 1D OHLCV for all symbols, with disk cache for re-runs."""
    key = f"{start_date}_{end_date}"
    if os.path.exists(CACHE_PATH):
        with open(CACHE_PATH, "rb") as f:
            blob = pickle.load(f)
        if blob.get("key") == key:
            print(f"Loaded {len(blob['data'])} symbols from cache.")
            return blob["data"]

    # BTC needs extra lookback for MA200
    btc_start_date = "2021-01-01"  # gives 1y buffer before 2022-01-01 for MA200
    start_ms = int(datetime.fromisoformat(btc_start_date).replace(tzinfo=timezone.utc).timestamp() * 1000)
    end_ms = int(datetime.fromisoformat(end_date).replace(tzinfo=timezone.utc).timestamp() * 1000)

    data = {}
    for i, sym in enumerate(symbols, 1):
        print(f"[{i}/{len(symbols)}] fetching {sym}...", end=" ", flush=True)
        try:
            bars = fetch_1d(sym, start_ms, end_ms)
            data[sym] = bars
            print(f"{len(bars)} bars")
        except Exception as exc:
            print(f"ERROR: {exc}")
            data[sym] = np.array([])

    with open(CACHE_PATH, "wb") as f:
        pickle.dump({"key": key, "data": data}, f)
    return data


def align_to_ts(data: dict[str, np.ndarray], start_ts: int, end_ts: int) -> tuple[np.ndarray, dict[str, np.ndarray], dict[str, np.ndarray]]:
    """Return a shared daily timestamp grid and per-symbol close/ts arrays,
    aligned so index i == the same calendar day across all symbols."""
    btc = data["BTC/USDT"]
    mask = (btc[:, 0] >= start_ts) & (btc[:, 0] < end_ts)
    ts_grid = btc[mask, 0].astype(np.int64)

    closes = {}
    valid_from = {}  # index into ts_grid where this symbol first has data
    for sym, bars in data.items():
        if len(bars) == 0:
            closes[sym] = np.full(len(ts_grid), np.nan)
            continue
        sym_ts = bars[:, 0].astype(np.int64)
        sym_close = bars[:, 4]
        aligned = np.full(len(ts_grid), np.nan)
        # map by timestamp
        ts_to_close = dict(zip(sym_ts, sym_close))
        for j, t in enumerate(ts_grid):
            if t in ts_to_close:
                aligned[j] = ts_to_close[t]
        closes[sym] = aligned
    return ts_grid, closes, valid_from


def sma(arr: np.ndarray, period: int) -> np.ndarray:
    """Simple moving average. Returns array of same length, NaN before window fills."""
    out = np.full(len(arr), np.nan)
    cumsum = np.cumsum(np.nan_to_num(arr, nan=0.0))
    valid = ~np.isnan(arr)
    valid_cumsum = np.cumsum(valid.astype(int))
    for i in range(period - 1, len(arr)):
        if valid_cumsum[i] - (valid_cumsum[i - period] if i >= period else 0) == period:
            start = i - period
            s = cumsum[i] - (cumsum[start] if start >= 0 else 0)
            out[i] = s / period
    return out


def log_return(closes: np.ndarray, i: int, lookback: int) -> float:
    """Past-lookback log return at index i. Returns -inf if data missing."""
    if i < lookback:
        return -np.inf
    c_now = closes[i]
    c_then = closes[i - lookback]
    if np.isnan(c_now) or np.isnan(c_then) or c_then <= 0:
        return -np.inf
    return float(np.log(c_now / c_then))


def daily_vol(closes: np.ndarray, i: int, lookback: int) -> float:
    """Std dev of daily log returns over the past `lookback` days. np.inf if missing."""
    if i < lookback + 1:
        return np.inf
    window = closes[i - lookback : i + 1]
    if np.any(np.isnan(window)) or np.any(window <= 0):
        return np.inf
    daily_logret = np.diff(np.log(window))
    return float(np.std(daily_logret))


def run_momentum(start_date: str = "2022-01-01", end_date: str = "2025-01-01") -> dict:
    symbols = list(SYMBOL_MAP.keys())
    data = load_all_data(symbols, start_date, end_date)

    start_ts = int(datetime.fromisoformat(start_date).replace(tzinfo=timezone.utc).timestamp() * 1000)
    end_ts = int(datetime.fromisoformat(end_date).replace(tzinfo=timezone.utc).timestamp() * 1000)

    ts_grid, closes, _ = align_to_ts(data, start_ts, end_ts)

    # For MA200, use BTC's full history including pre-start buffer
    btc_full = data["BTC/USDT"]
    btc_full_ts = btc_full[:, 0].astype(np.int64)
    btc_full_close = btc_full[:, 4]
    btc_ma200_full = sma(btc_full_close, MA200_PERIOD)
    # Map BTC MA200 onto our ts_grid
    btc_ma200_map = dict(zip(btc_full_ts, btc_ma200_full))
    btc_close_map = dict(zip(btc_full_ts, btc_full_close))
    ma200_on_grid = np.array([btc_ma200_map.get(t, np.nan) for t in ts_grid])
    btc_on_grid = np.array([btc_close_map.get(t, np.nan) for t in ts_grid])

    cash = INITIAL_CASH
    # holdings: {sym: {"qty": float, "entry_px": float, "entry_i": int}}
    holdings: dict = {}
    peak = INITIAL_CASH
    max_dd = 0.0
    equity_curve = []
    trades: list[dict] = []
    rebalance_count = 0
    filter_blocks = 0  # weeks where MA200 filter blocked new entries

    def mark_to_market(i: int) -> float:
        value = cash
        for sym, h in holdings.items():
            px = closes[sym][i]
            if not np.isnan(px):
                value += h["qty"] * px
        return value

    def liquidate_all(i: int, reason: str):
        nonlocal cash
        for sym in list(holdings.keys()):
            h = holdings[sym]
            px = closes[sym][i]
            if np.isnan(px):
                continue
            pnl_pct = (px - h["entry_px"]) / h["entry_px"]
            proceeds = h["qty"] * px * (1 - TAKER_FEE)
            cash += proceeds
            trades.append({
                "sym": sym,
                "entry_ts": int(ts_grid[h["entry_i"]]),
                "entry_px": h["entry_px"],
                "exit_ts": int(ts_grid[i]),
                "exit_px": float(px),
                "pnl_pct": pnl_pct,
                "reason": reason,
                "bars_held": i - h["entry_i"],
            })
            del holdings[sym]

    for i in range(len(ts_grid)):
        dt = datetime.fromtimestamp(ts_grid[i] / 1000, tz=timezone.utc)

        # ── intra-week: check individual stop-loss ───────────────────
        for sym in list(holdings.keys()):
            px = closes[sym][i]
            if np.isnan(px):
                continue
            h = holdings[sym]
            pnl_pct = (px - h["entry_px"]) / h["entry_px"]
            if pnl_pct <= INDIVIDUAL_SL:
                proceeds = h["qty"] * px * (1 - TAKER_FEE)
                cash += proceeds
                trades.append({
                    "sym": sym,
                    "entry_ts": int(ts_grid[h["entry_i"]]),
                    "entry_px": h["entry_px"],
                    "exit_ts": int(ts_grid[i]),
                    "exit_px": float(px),
                    "pnl_pct": pnl_pct,
                    "reason": "SL",
                    "bars_held": i - h["entry_i"],
                })
                del holdings[sym]

        # ── Sunday: rebalance ────────────────────────────────────────
        if dt.weekday() == 6:  # Sunday
            rebalance_count += 1

            # Market filter
            btc_px = btc_on_grid[i]
            btc_ma = ma200_on_grid[i]
            filter_ok = (not np.isnan(btc_px)) and (not np.isnan(btc_ma)) and btc_px > btc_ma

            if not filter_ok:
                # kill switch: liquidate everything, stay flat
                liquidate_all(i, "FILTER_OFF")
                filter_blocks += 1
            else:
                # Score everyone — but exclude high-volatility trash
                scores = []
                for sym in symbols:
                    v = daily_vol(closes[sym], i, VOL_LOOKBACK_DAYS)
                    if v > VOL_CAP:
                        continue  # volatility filter
                    r = log_return(closes[sym], i, LOOKBACK_DAYS)
                    if r == -np.inf:
                        continue
                    scores.append((sym, r))
                scores.sort(key=lambda x: x[1], reverse=True)
                top = [s for s, _ in scores[:TOP_K]]

                # Sell anything not in top
                for sym in list(holdings.keys()):
                    if sym not in top:
                        h = holdings[sym]
                        px = closes[sym][i]
                        if np.isnan(px):
                            continue
                        pnl_pct = (px - h["entry_px"]) / h["entry_px"]
                        proceeds = h["qty"] * px * (1 - TAKER_FEE)
                        cash += proceeds
                        trades.append({
                            "sym": sym,
                            "entry_ts": int(ts_grid[h["entry_i"]]),
                            "entry_px": h["entry_px"],
                            "exit_ts": int(ts_grid[i]),
                            "exit_px": float(px),
                            "pnl_pct": pnl_pct,
                            "reason": "ROT",
                            "bars_held": i - h["entry_i"],
                        })
                        del holdings[sym]

                # Buy new names to reach top_k
                new_names = [s for s in top if s not in holdings]
                if new_names:
                    slots_open = len(new_names)
                    # equal weight across the FINAL top_k, so each target is 1/top_k of equity
                    equity_now = mark_to_market(i)
                    target_per_name = equity_now / TOP_K
                    for sym in new_names:
                        px = closes[sym][i]
                        if np.isnan(px) or px <= 0:
                            continue
                        # buy up to target_per_name, capped at available cash
                        alloc = min(target_per_name, cash)
                        if alloc < 20:
                            continue
                        qty = alloc / (px * (1 + TAKER_FEE))
                        cash -= qty * px * (1 + TAKER_FEE)
                        holdings[sym] = {"qty": qty, "entry_px": float(px), "entry_i": i}

        # record equity
        eq = mark_to_market(i)
        peak = max(peak, eq)
        dd = (eq - peak) / peak if peak else 0.0
        max_dd = min(max_dd, dd)
        equity_curve.append((int(ts_grid[i]), eq))

    # final liquidation
    final_i = len(ts_grid) - 1
    liquidate_all(final_i, "EOP")

    final_equity = cash
    ret_pct = (final_equity / INITIAL_CASH - 1) * 100
    closed = trades
    wins = sum(1 for t in closed if t["pnl_pct"] > 0)
    win_rate = wins / len(closed) * 100 if closed else 0.0
    avg_win = np.mean([t["pnl_pct"] for t in closed if t["pnl_pct"] > 0]) * 100 if wins else 0.0
    losses = [t["pnl_pct"] for t in closed if t["pnl_pct"] <= 0]
    avg_loss = np.mean(losses) * 100 if losses else 0.0

    return {
        "start": start_date,
        "end": end_date,
        "bars": len(ts_grid),
        "rebalance_count": rebalance_count,
        "filter_blocks": filter_blocks,
        "trades": len(closed),
        "wins": wins,
        "win_rate": win_rate,
        "avg_win_pct": avg_win,
        "avg_loss_pct": avg_loss,
        "return_pct": ret_pct,
        "max_dd_pct": max_dd * 100,
        "final_equity": final_equity,
        "equity_curve": equity_curve,
        "trade_log": closed,
    }


def print_by_year(result: dict):
    """Slice results by calendar year for context."""
    curve = result["equity_curve"]
    if not curve:
        return
    buckets: dict[int, list] = {}
    for ts_ms, eq in curve:
        y = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).year
        buckets.setdefault(y, []).append(eq)

    print(f"\n{'Year':<6} {'Start':>10} {'End':>10} {'Return':>9} {'Max DD':>9}")
    print("-" * 50)
    for y, eqs in sorted(buckets.items()):
        if len(eqs) < 2:
            continue
        ret = (eqs[-1] / eqs[0] - 1) * 100
        peak_y, dd_y = eqs[0], 0.0
        for e in eqs:
            peak_y = max(peak_y, e)
            d = (e - peak_y) / peak_y
            dd_y = min(dd_y, d)
        print(f"{y:<6} ${eqs[0]:>8.2f} ${eqs[-1]:>8.2f} {ret:>+7.2f}% {dd_y*100:>+7.2f}%")


def main() -> int:
    print("=" * 80)
    print(f"Momentum Rotation Backtest — Variant A")
    print(f"  top-{TOP_K}, {LOOKBACK_DAYS}d lookback, SL {INDIVIDUAL_SL*100:.0f}%, "
          f"vol cap {VOL_CAP*100:.0f}%, BTC MA200 filter")
    print("=" * 80)

    result = run_momentum("2022-01-01", "2025-01-01")

    print(f"\nPeriod:          {result['start']} → {result['end']}")
    print(f"Days:            {result['bars']}")
    print(f"Rebalances:      {result['rebalance_count']} weeks")
    print(f"Filter blocks:   {result['filter_blocks']} weeks (kill switch on)")
    print(f"Trades closed:   {result['trades']}")
    print(f"Win rate:        {result['win_rate']:.1f}%")
    print(f"Avg win:         +{result['avg_win_pct']:.2f}%")
    print(f"Avg loss:        {result['avg_loss_pct']:+.2f}%")
    print(f"Final equity:    ${result['final_equity']:.2f}")
    print(f"Total return:    {result['return_pct']:+.2f}%")
    print(f"Max drawdown:    {result['max_dd_pct']:+.2f}%")

    print_by_year(result)

    # exit reason breakdown
    reasons: dict = {}
    for t in result["trade_log"]:
        reasons[t["reason"]] = reasons.get(t["reason"], 0) + 1
    print(f"\nExit reasons: {reasons}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
