"""Offline backtest harness for Variant C strategy on historical crypto data.

Goal: compare strategy behavior across market regimes (bear / range / rebound)
using stable historical data fetched once from Binance.
"""
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import ccxt
import numpy as np

from config import MIN_NET_PROFIT, REGIME_PARAMS, TAKER_FEE
from indicators import compute, signal_at


DEFAULT_PARAMS = {
    "ma_fast": 20,
    "ma_slow": 50,
    "macd_fast": 12,
    "macd_slow": 26,
    "macd_signal": 9,
    "rsi_period": 14,
    "rsi_buy": 38,
    "rsi_sell": 70,
}


def fetch_history(symbol: str, start_ms: int, end_ms: int, tf: str = "4h") -> np.ndarray:
    ex = ccxt.binance({"enableRateLimit": True})
    bars: list = []
    since = start_ms
    while since < end_ms:
        chunk = ex.fetch_ohlcv(symbol, tf, since=since, limit=1000)
        if not chunk:
            break
        bars.extend(chunk)
        next_since = chunk[-1][0] + 1
        if next_since <= since:
            break
        since = next_since
        time.sleep(0.15)
    return np.array([b for b in bars if b[0] < end_ms], dtype=float)


@dataclass
class Trade:
    symbol: str
    entry_ts: int
    entry_px: float
    exit_ts: int
    exit_px: float
    pnl_pct: float
    reason: str
    entry_type: str
    bars: int


def classify_regime(adx_val: float) -> str:
    if adx_val < 25:
        return "RANGE"
    if adx_val > 40:
        return "TRENDING"
    return "TRANSITION"


def run_backtest(
    symbol: str,
    start_date: str,
    end_date: str,
    params: dict,
    initial_cash: float = 1000.0,
    taker: float = TAKER_FEE,
    min_net: float = MIN_NET_PROFIT,
    alloc_pct: float = 0.20,
) -> dict | None:
    start_ms = int(datetime.fromisoformat(start_date).replace(tzinfo=timezone.utc).timestamp() * 1000)
    end_ms = int(datetime.fromisoformat(end_date).replace(tzinfo=timezone.utc).timestamp() * 1000)

    bars = fetch_history(symbol, start_ms, end_ms)
    if len(bars) == 0:
        return None

    closes = bars[:, 4]
    highs = bars[:, 2]
    lows = bars[:, 3]
    ts = bars[:, 0].astype(np.int64)
    ind = compute(closes, params, high=highs, low=lows)

    cash = initial_cash
    position = None
    trades: list[Trade] = []
    peak = initial_cash
    max_dd = 0.0

    pullback_signals = 0
    trend_signals = 0

    warmup = max(params["ma_slow"], 60)
    for i in range(warmup, len(closes)):
        px = float(closes[i])
        adx_val = float(ind["adx"][i])
        regime = classify_regime(adx_val)
        cfg = REGIME_PARAMS[regime]

        equity = cash + (position["qty"] * px if position else 0.0)
        peak = max(peak, equity)
        dd = (equity - peak) / peak if peak else 0.0
        max_dd = min(max_dd, dd)

        # ── exits ────────────────────────────────────────────────────
        if position:
            pnl_pct = (px - position["entry_px"]) / position["entry_px"]
            exited = False

            if pnl_pct <= cfg["sl"]:
                cash += position["qty"] * px * (1 - taker)
                trades.append(Trade(symbol, position["ts"], position["entry_px"], int(ts[i]), px,
                                    pnl_pct, "SL", position["type"], i - position["i"]))
                position = None
                exited = True
            elif pnl_pct >= cfg["tp2"]:
                cash += position["qty"] * px * (1 - taker)
                trades.append(Trade(symbol, position["ts"], position["entry_px"], int(ts[i]), px,
                                    pnl_pct, "TP2", position["type"], i - position["i"]))
                position = None
                exited = True
            elif (not position["tp1_done"]) and pnl_pct >= cfg["tp1"]:
                half_qty = position["qty"] / 2
                cash += half_qty * px * (1 - taker)
                position["qty"] -= half_qty
                position["tp1_done"] = True
            elif signal_at(ind, i, params) == "SELL":
                cash += position["qty"] * px * (1 - taker)
                trades.append(Trade(symbol, position["ts"], position["entry_px"], int(ts[i]), px,
                                    pnl_pct, "SIGNAL", position["type"], i - position["i"]))
                position = None
                exited = True

            if exited:
                continue

        # ── entries ──────────────────────────────────────────────────
        if not position:
            rsi_val = float(ind["rsi"][i])
            ma_cross = float(ind["ma_cross"][i])
            macd_h = float(ind["macd_h"][i])

            is_pullback = (signal_at(ind, i, params) == "BUY" and rsi_val < params["rsi_buy"])
            is_trend = (cfg["allow_trend"]
                        and ma_cross > 0
                        and macd_h > 0
                        and 40 <= rsi_val <= 70
                        and adx_val > 25)

            if is_pullback:
                pullback_signals += 1
            if is_trend:
                trend_signals += 1

            if is_pullback or is_trend:
                total_fee = taker * (1 + cfg["tp1"] / 2 + cfg["tp2"] / 2)
                if cfg["tp2"] - total_fee < min_net:
                    continue

                alloc = min(equity * alloc_pct, cash)
                if alloc < 20:
                    continue
                qty = alloc / (px * (1 + taker))
                cash -= qty * px * (1 + taker)
                position = {
                    "entry_px": px,
                    "qty": qty,
                    "i": i,
                    "ts": int(ts[i]),
                    "tp1_done": False,
                    "type": "PB" if is_pullback else "TREND",
                }

    # close remaining
    if position:
        px = float(closes[-1])
        cash += position["qty"] * px * (1 - taker)
        pnl_pct = (px - position["entry_px"]) / position["entry_px"]
        trades.append(Trade(symbol, position["ts"], position["entry_px"], int(ts[-1]), px,
                            pnl_pct, "EOP", position["type"], len(closes) - 1 - position["i"]))

    final_equity = cash
    ret_pct = (final_equity / initial_cash - 1) * 100
    full_trades = [t for t in trades if t.reason != "TP1"]
    wins = sum(1 for t in full_trades if t.pnl_pct > 0)
    win_rate = wins / len(full_trades) * 100 if full_trades else 0.0

    return {
        "symbol": symbol,
        "start": start_date,
        "end": end_date,
        "bars": len(closes),
        "pullback_signals": pullback_signals,
        "trend_signals": trend_signals,
        "trades": len(full_trades),
        "return_pct": ret_pct,
        "max_dd_pct": max_dd * 100,
        "win_rate": win_rate,
        "final_equity": final_equity,
        "trade_log": full_trades,
    }


def main() -> int:
    segments = [
        ("BTC/USDT", "2022-01-01", "2022-06-30", "BTC 熊市 H1 (47k→19k)"),
        ("BTC/USDT", "2022-07-01", "2022-12-31", "BTC 震盪 H2 (19-25k)"),
        ("BTC/USDT", "2023-01-01", "2023-06-30", "BTC 反彈 H1 (16k→30k)"),
        ("BTC/USDT", "2023-07-01", "2023-12-31", "BTC 橫盤 H2 (26-45k)"),
        ("ETH/USDT", "2022-01-01", "2022-06-30", "ETH 熊市 H1"),
        ("ETH/USDT", "2022-07-01", "2022-12-31", "ETH 震盪 H2"),
        ("ETH/USDT", "2023-01-01", "2023-06-30", "ETH 反彈 H1"),
        ("ETH/USDT", "2023-07-01", "2023-12-31", "ETH 橫盤 H2"),
    ]

    print(f"{'='*80}\nBacktest: Variant C strategy on historical data\n{'='*80}")
    print(f"params: {DEFAULT_PARAMS}\n")
    print(f"{'Period':<32} {'Bars':>5} {'PB':>4} {'TR':>4} {'Trd':>4} {'Ret%':>8} {'DD%':>8} {'Win%':>6}")
    print("-" * 80)

    results = []
    for symbol, start, end, label in segments:
        try:
            r = run_backtest(symbol, start, end, DEFAULT_PARAMS)
        except Exception as exc:
            print(f"{label:<32} ERROR: {exc}")
            continue
        if r is None:
            print(f"{label:<32} NO DATA")
            continue
        results.append((label, r))
        print(f"{label:<32} {r['bars']:>5} "
              f"{r['pullback_signals']:>4} {r['trend_signals']:>4} "
              f"{r['trades']:>4} {r['return_pct']:>+7.2f}% "
              f"{r['max_dd_pct']:>+7.2f}% {r['win_rate']:>5.1f}%")

    print("-" * 80)
    print("Legend: PB=pullback signals, TR=trend signals (raw candidates, pre-filter)")
    print("        Trd=actual trades (closed), Ret%=cumulative return, DD%=max drawdown")

    # aggregate
    if results:
        total_ret = sum(r["return_pct"] for _, r in results)
        total_trd = sum(r["trades"] for _, r in results)
        print(f"\nAGGREGATE: {len(results)} segments, {total_trd} total trades, "
              f"avg return per segment: {total_ret/len(results):+.2f}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
