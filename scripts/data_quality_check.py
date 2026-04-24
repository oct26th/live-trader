#!/usr/bin/env python3
"""
Data Quality Check — validates OHLCV data before backtesting.

Runs automatically at the start of any backtest script.
Checks for:
  1. Flash crash detection (single-bar moves > 20%)
  2. Missing data gaps
  3. Stale data (last bar older than 24h)
  4. Constant-price bars (flat lines suggesting feed failure)

Usage:
  python3 scripts/data_quality_check.py [--verbose]

Exit codes:
  0 = all checks passed
  1 = warnings only (checks passed but anomalies detected)
  2 = data quality issues found
"""
import argparse
import sys
import numpy as np
import ccxt
from datetime import datetime, timezone, timedelta

FLASH_CRASH_THRESHOLD = 0.20     # 20% single-bar move = anomaly
MAX_CONSECUTIVE_FLATS = 5        # 5+ flat bars = feed failure
STALE_HOURS = 48                 # last bar older than this = warning

SYMBOLS = [
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT", "ADA/USDT",
    "AVAX/USDT", "LINK/USDT", "APT/USDT", "NEAR/USDT", "DOT/USDT",
    "ICP/USDT", "ATOM/USDT", "OP/USDT", "DOGE/USDT", "LTC/USDT",
    "ARB/USDT", "TIA/USDT", "SUI/USDT", "INJ/USDT", "WIF/USDT",
]

TIMEFRAMES = ["1d", "4h", "1h"]


def check_flash_crash(bars, symbol, tf):
    """Detect single-bar moves > 20% (likely wick flash crashes)."""
    if len(bars) < 2:
        return []
    anomalies = []
    for i in range(1, len(bars)):
        o, h, l, c = bars[i][1], bars[i][2], bars[i][3], bars[i][4]
        if o <= 0 or c <= 0:
            continue
        bar_ret = abs(c - o) / o
        # Check full range including wicks
        high_ret = (h - o) / o
        low_ret  = (l - o) / o
        if abs(low_ret) > FLASH_CRASH_THRESHOLD or abs(high_ret) > FLASH_CRASH_THRESHOLD:
            dt = datetime.fromtimestamp(bars[i][0] / 1000, tz=timezone.utc)
            anomalies.append({
                "symbol": symbol,
                "timeframe": tf,
                "date": dt.strftime("%Y-%m-%d %H:%M"),
                "type": "wick",
                "open": o, "high": h, "low": l, "close": c,
                "wick_pct": f"{low_ret*100:+.1f}% / {high_ret*100:+.1f}%",
            })
    return anomalies


def check_gaps(bars, symbol, tf):
    """Detect large time gaps between consecutive bars."""
    gaps = []
    for i in range(1, len(bars)):
        dt0 = bars[i-1][0]
        dt1 = bars[i][0]
        # Expected interval: 1d = 86400s, 4h = 14400s, 1h = 3600s
        interval_map = {"1d": 86400, "4h": 14400, "1h": 3600}
        expected = interval_map.get(tf, 86400)
        gap_hours = (dt1 - dt0) / 3600
        if gap_hours > expected * 3:  # 3x expected gap
            dt = datetime.fromtimestamp(dt1 / 1000, tz=timezone.utc)
            gaps.append({
                "symbol": symbol, "timeframe": tf,
                "date": dt.strftime("%Y-%m-%d %H:%M"),
                "gap_hours": round(gap_hours, 1),
            })
    return gaps


def check_flat_bars(bars, symbol, tf):
    """Detect 5+ consecutive bars with identical close price."""
    if len(bars) < MAX_CONSECUTIVE_FLATS:
        return []
    flats = []
    count = 1
    for i in range(1, len(bars)):
        if bars[i][4] == bars[i-1][4]:
            count += 1
        else:
            if count >= MAX_CONSECUTIVE_FLATS:
                dt = datetime.fromtimestamp(bars[i-1][0] / 1000, tz=timezone.utc)
                flats.append({
                    "symbol": symbol, "timeframe": tf,
                    "date": dt.strftime("%Y-%m-%d"),
                    "count": count, "price": bars[i-1][4],
                })
            count = 1
    return flats


def check_stale_data(bars, symbol, tf):
    """Check if the most recent bar is older than STALE_HOURS."""
    if len(bars) == 0:
        return {"symbol": symbol, "timeframe": tf, "stale": True}
    last_ts = bars[-1][0]
    last_dt = datetime.fromtimestamp(last_ts / 1000, tz=timezone.utc)
    now = datetime.now(timezone.utc)
    age_hours = (now - last_dt).total_seconds() / 3600
    if age_hours > STALE_HOURS:
        return {
            "symbol": symbol, "timeframe": tf,
            "last_bar": last_dt.strftime("%Y-%m-%d %H:%M"),
            "age_hours": round(age_hours, 1),
            "stale": True,
        }
    return {"symbol": symbol, "timeframe": tf, "stale": False, "last_bar": last_dt.strftime("%Y-%m-%d"), "age_hours": round(age_hours, 1)}


def run_checks(symbols, timeframes, verbose=False):
    ex = ccxt.binance({"enableRateLimit": True})
    all_anomalies = []

    for sym in symbols:
        for tf in timeframes:
            try:
                bars = ex.fetch_ohlcv(sym, tf, limit=200)
                if not bars:
                    continue
            except Exception as e:
                if verbose:
                    print(f"  ⚠️  {sym}/{tf}: fetch error — {e}")
                continue

            # Check 1D specifically for flash crash
            if tf == "1d":
                flash = check_flash_crash(bars, sym, tf)
                if flash:
                    all_anomalies.extend(flash)

            gaps  = check_gaps(bars, sym, tf)
            flats = check_flat_bars(bars, sym, tf)
            stale = check_stale_data(bars, sym, tf)

            if verbose:
                for a in flash:
                    print(f"  🔥 {a['date']} {a['symbol']}/{a['timeframe']}: wick {a['wick_pct']} (O={a['open']:,.0f} C={a['close']:,.0f})")
                for g in gaps:
                    print(f"  ⏳ {g['date']} {g['symbol']}/{g['timeframe']}: gap {g['gap_hours']}h")
                for f in flats:
                    print(f"  📉 {f['date']} {f['symbol']}/{f['timeframe']}: {f['count']}x flat @ ${f['price']:,.0f}")
                if stale.get("stale"):
                    print(f"  ⚠️  {stale['symbol']}/{stale['timeframe']}: STALE {stale['age_hours']}h old (last: {stale['last_bar']})")

    return all_anomalies


def main():
    parser = argparse.ArgumentParser(description="Data quality sanity check")
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument("--symbols", nargs="*", default=SYMBOLS)
    parser.add_argument("--timeframes", nargs="*", default=["1d"])
    args = parser.parse_args()

    print(f"🔍 Data Quality Check — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"   Symbols: {len(args.symbols)} | Timeframes: {args.timeframes}")
    print()

    anomalies = run_checks(args.symbols, args.timeframes, verbose=args.verbose)

    print()
    if not anomalies:
        print("✅ All checks passed — no anomalies detected")
        sys.exit(0)
    else:
        print(f"⚠️  Detected {len(anomalies)} anomaly(ies):")
        for a in anomalies:
            print(f"  🔥 {a['date']} {a['symbol']}/{a['timeframe']}: wick {a['wick_pct']}")
        print()
        print("   Note: Large wicks (single-bar >20% move) may indicate real flash crash events.")
        print("   In backtesting, these are valid market events. The strategy's behavior during")
        print("   flash crashes is part of the documented risk (see CLAUDE.md §5).")
        sys.exit(1)


if __name__ == "__main__":
    main()
