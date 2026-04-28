"""Paper-mode strategy classes for the bake-off arena.

Each Strategy:
- Owns its own Portfolio (paper cash) and entry_px / state.
- Receives a shared MarketData snapshot per tick (no per-strategy refetch).
- Is fully forward-simulated — same code path that would run live.

Arena pattern guarantees:
- All strategies see identical market data each tick (apples-to-apples).
- Each strategy persists to its own paper_state_{name}.json.
- No strategy can see another's portfolio (no leak).
"""
from __future__ import annotations

import json
import logging
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

import numpy as np

from config import (
    ARENA_INITIAL_CASH,
    ARENA_MA200_PERIOD,
    ARENA_REBALANCE_WEEKDAY,
    ARENA_STATE_DIR,
    SYMBOL_MAP,
    TAKER_FEE,
)
from exchange import to_bin, to_cb
from portfolio import Portfolio


# ── Shared market data snapshot ──────────────────────────────────────────────
@dataclass
class MarketData:
    """One tick's view of the market — passed to every strategy."""
    ts: datetime
    closes_map: dict[str, np.ndarray]    # bin_sym -> 1D closes (most recent last)
    highs_map: dict[str, np.ndarray]     # bin_sym -> 1D highs
    lows_map: dict[str, np.ndarray]      # bin_sym -> 1D lows
    btc_close: float | None              # convenience: latest BTC 1D close
    btc_ma200: float | None              # convenience: BTC 1D MA200

    def latest_price(self, bin_sym: str) -> float | None:
        c = self.closes_map.get(bin_sym)
        if c is None or len(c) == 0:
            return None
        v = float(c[-1])
        return v if not np.isnan(v) else None


# ── Strategy base class ──────────────────────────────────────────────────────
class Strategy(ABC):
    """Abstract base. Subclasses must implement on_tick()."""

    def __init__(self, name: str, label: str, initial_cash: float = ARENA_INITIAL_CASH) -> None:
        self.name = name
        self.label = label
        self.portfolio = Portfolio(initial_cash)
        self.log = logging.getLogger(f"strategy.{name}")
        self._entry_px: dict[str, float] = {}
        self._peak_equity = initial_cash
        self._max_dd = 0.0
        self._prices: dict[str, float] = {}    # cb_sym -> latest price (for portfolio.equity)
        self._last_rebalance_date: str | None = None
        self._extra: dict = {}                 # subclass-specific scratch state

    # ── public API ───────────────────────────────────────────────────────────
    def tick(self, md: MarketData) -> None:
        """Update prices, run the subclass logic, refresh DD, save state."""
        # mark-to-market: cb_sym -> price (subset of universe each strategy uses)
        for bin_sym, px_arr in md.closes_map.items():
            if len(px_arr) == 0:
                continue
            v = float(px_arr[-1])
            if not np.isnan(v):
                self._prices[to_cb(bin_sym)] = v

        try:
            self.on_tick(md)
        except Exception as exc:
            self.log.error(f"on_tick failed: {exc}", exc_info=True)

        eq = self.portfolio.equity(self._prices)
        self._peak_equity = max(self._peak_equity, eq)
        if self._peak_equity > 0:
            dd = (eq - self._peak_equity) / self._peak_equity
            self._max_dd = min(self._max_dd, dd)

        self.save()

    @abstractmethod
    def on_tick(self, md: MarketData) -> None:
        """Subclass-specific logic. Update self.portfolio + self._entry_px."""
        ...

    # ── orderbook (paper) ────────────────────────────────────────────────────
    def buy(self, cb_sym: str, qty: float, px: float, reason: str = "") -> None:
        cost = qty * px * (1 + TAKER_FEE)
        if cost <= 0 or cost > self.portfolio.cash:
            return
        self.portfolio.cash -= cost
        self.portfolio.positions[cb_sym] = self.portfolio.positions.get(cb_sym, 0.0) + qty
        # If averaging in, update entry to weighted avg; else set new entry.
        old_qty = self.portfolio.positions[cb_sym] - qty
        if old_qty > 0 and cb_sym in self._entry_px:
            old_entry = self._entry_px[cb_sym]
            self._entry_px[cb_sym] = (old_entry * old_qty + px * qty) / (old_qty + qty)
        else:
            self._entry_px[cb_sym] = px
        self.log.info(f"[{self.name}] BUY {cb_sym} qty={qty:.6f} @ ${px:.4f} cost=${cost:.2f} ({reason})")

    def sell(self, cb_sym: str, reason: str = "", qty: float | None = None) -> None:
        held = self.portfolio.positions.get(cb_sym, 0.0)
        if held <= 0:
            return
        sell_qty = held if qty is None else min(qty, held)
        px = self._prices.get(cb_sym, 0.0)
        if px <= 0:
            return
        entry = self._entry_px.get(cb_sym, px)
        pnl_pct = (px - entry) / entry if entry > 0 else 0.0
        proceeds = sell_qty * px * (1 - TAKER_FEE)
        self.portfolio.cash += proceeds
        self.portfolio.trades.append({
            "sym": cb_sym, "qty": sell_qty, "entry_px": entry, "exit_px": px,
            "pnl_pct": pnl_pct, "reason": reason,
            "ts": datetime.now(timezone.utc).isoformat(),
        })
        self.portfolio.positions[cb_sym] = held - sell_qty
        if self.portfolio.positions[cb_sym] <= 1e-9:
            del self.portfolio.positions[cb_sym]
            self._entry_px.pop(cb_sym, None)
        self.log.info(f"[{self.name}] SELL {cb_sym} qty={sell_qty:.6f} @ ${px:.4f} pnl={pnl_pct*100:+.2f}% ({reason})")

    def equity(self) -> float:
        return self.portfolio.equity(self._prices)

    # ── persistence ──────────────────────────────────────────────────────────
    def state_path(self) -> str:
        return f"{ARENA_STATE_DIR}/paper_state_{self.name}.json"

    def save(self) -> None:
        os.makedirs(ARENA_STATE_DIR, exist_ok=True)
        snapshot = {
            "name": self.name,
            "label": self.label,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "cash": self.portfolio.cash,
            "positions": self.portfolio.positions,
            "entry_px": self._entry_px,
            "equity": self.equity(),
            "peak_equity": self._peak_equity,
            "max_dd_pct": self._max_dd * 100,
            "last_rebalance_date": self._last_rebalance_date,
            "trade_count": len(self.portfolio.trades),
            "trades_tail": self.portfolio.trades[-50:],
            "extra": self._extra,
        }
        with open(self.state_path(), "w") as f:
            json.dump(snapshot, f, indent=2, default=str)

    def load(self) -> None:
        path = self.state_path()
        if not os.path.exists(path):
            return
        try:
            with open(path) as f:
                s = json.load(f)
        except (json.JSONDecodeError, IOError):
            return
        self.portfolio.cash = s.get("cash", ARENA_INITIAL_CASH)
        self.portfolio.positions = s.get("positions", {})
        self._entry_px = s.get("entry_px", {})
        self._peak_equity = s.get("peak_equity", ARENA_INITIAL_CASH)
        self._max_dd = s.get("max_dd_pct", 0.0) / 100
        self._last_rebalance_date = s.get("last_rebalance_date")
        self.portfolio.trades = s.get("trades_tail", [])
        self._extra = s.get("extra", {})


# ── Helpers ──────────────────────────────────────────────────────────────────
def _ema(arr: np.ndarray, span: int) -> np.ndarray:
    a = 2 / (span + 1)
    out = np.empty(len(arr), dtype=float)
    out[0] = arr[0]
    for i in range(1, len(arr)):
        out[i] = a * arr[i] + (1 - a) * out[i - 1]
    return out


def _atr(high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = 14) -> np.ndarray:
    n = len(close)
    if n < period + 1:
        return np.zeros(n)
    tr = np.zeros(n)
    tr[0] = high[0] - low[0]
    for i in range(1, n):
        hl = high[i] - low[i]
        hc = abs(high[i] - close[i - 1])
        lc = abs(low[i] - close[i - 1])
        tr[i] = max(hl, hc, lc)
    out = np.zeros(n)
    out[period - 1] = tr[:period].mean()
    for i in range(period, n):
        out[i] = (out[i - 1] * (period - 1) + tr[i]) / period
    return out


def _is_new_rebalance_day(today: datetime, last_str: str | None, weekday: int) -> bool:
    """True if today is the target weekday and we haven't rebalanced today yet."""
    if today.weekday() != weekday:
        return False
    return last_str != today.strftime("%Y-%m-%d")


# ── Momentum rotation strategy (variants A, A', A'') ─────────────────────────
class MomentumStrategy(Strategy):
    """Top-K by N-day log return, weekly rebalance, BTC MA200 kill switch."""

    def __init__(
        self, name: str, label: str,
        lookback: int, top_k: int, vol_cap: float, sl: float,
        vol_lookback: int = 30,
    ) -> None:
        super().__init__(name, label)
        self.lookback = lookback
        self.top_k = top_k
        self.vol_cap = vol_cap
        self.sl = sl
        self.vol_lookback = vol_lookback
        self._extra.setdefault("filter_on", True)

    def on_tick(self, md: MarketData) -> None:
        # Per-tick stop-loss check
        for cb_sym in list(self.portfolio.positions.keys()):
            entry = self._entry_px.get(cb_sym)
            px = self._prices.get(cb_sym)
            if entry is None or px is None or entry <= 0:
                continue
            if (px - entry) / entry <= self.sl:
                self.sell(cb_sym, reason="SL")

        # Rebalance only on Sundays
        if not _is_new_rebalance_day(md.ts, self._last_rebalance_date, ARENA_REBALANCE_WEEKDAY):
            return

        self._last_rebalance_date = md.ts.strftime("%Y-%m-%d")
        self._rebalance(md)

    def _rebalance(self, md: MarketData) -> None:
        # Market filter: BTC > MA200
        btc_close = md.btc_close
        btc_ma = md.btc_ma200
        if btc_close is None or btc_ma is None or btc_close <= btc_ma:
            self._extra["filter_on"] = False
            self.log.info(f"[{self.name}] FILTER OFF — BTC ${btc_close} ≤ MA200 ${btc_ma}. Liquidating.")
            for cb_sym in list(self.portfolio.positions.keys()):
                self.sell(cb_sym, reason="FILTER_OFF")
            return

        self._extra["filter_on"] = True

        # Score
        scored: list[tuple[str, float]] = []
        min_history = ARENA_MA200_PERIOD + max(self.lookback, self.vol_lookback)
        for bin_sym, closes in md.closes_map.items():
            if len(closes) < min_history:
                continue
            recent = closes[-self.vol_lookback - 1:]
            if np.any(recent <= 0):
                continue
            daily_ret = np.diff(np.log(recent))
            vol = float(np.std(daily_ret))
            if vol > self.vol_cap:
                continue
            r = float(np.log(closes[-1] / closes[-self.lookback - 1]))
            if not np.isfinite(r):
                continue
            scored.append((bin_sym, r))

        scored.sort(key=lambda x: x[1], reverse=True)
        top = [s for s, _ in scored[: self.top_k]]
        self.log.info(f"[{self.name}] Rebalance — top-{self.top_k}: {top}")

        # Sell everything not in top
        for cb_sym in list(self.portfolio.positions.keys()):
            if to_bin(cb_sym) not in top:
                self.sell(cb_sym, reason="ROTATE")

        # Buy missing
        new_syms = [b for b in top if to_cb(b) not in self.portfolio.positions]
        if not new_syms:
            return
        equity_now = self.equity()
        target_per = equity_now / self.top_k
        for bin_sym in new_syms:
            cb_sym = to_cb(bin_sym)
            px = md.latest_price(bin_sym)
            if px is None or px <= 0:
                continue
            alloc = min(target_per, self.portfolio.cash)
            if alloc < 20:
                continue
            qty = alloc / (px * (1 + TAKER_FEE))
            self.buy(cb_sym, qty, px, reason="ROTATE_IN")


# ── Trend following strategy (variant B) ─────────────────────────────────────
class TrendFollowingStrategy(Strategy):
    """Single-symbol (BTC) trend follow on 1D: MA20/50 cross + ATR trailing stop."""

    def __init__(
        self, name: str, label: str,
        ma_fast: int = 20, ma_slow: int = 50,
        atr_period: int = 14, atr_mult: float = 2.0,
        symbol: str = "BTC/USDT",
    ) -> None:
        super().__init__(name, label)
        self.ma_fast = ma_fast
        self.ma_slow = ma_slow
        self.atr_period = atr_period
        self.atr_mult = atr_mult
        self.symbol = symbol
        self._extra.setdefault("trailing_high", None)

    def on_tick(self, md: MarketData) -> None:
        bin_sym = self.symbol
        cb_sym = to_cb(bin_sym)
        closes = md.closes_map.get(bin_sym)
        highs = md.highs_map.get(bin_sym)
        lows = md.lows_map.get(bin_sym)
        if closes is None or highs is None or lows is None:
            return
        if len(closes) < max(self.ma_slow, self.atr_period * 2):
            return

        ma_fast = _ema(closes, self.ma_fast)
        ma_slow = _ema(closes, self.ma_slow)
        atr = _atr(highs, lows, closes, self.atr_period)
        px = float(closes[-1])
        bullish = ma_fast[-1] > ma_slow[-1]
        held = self.portfolio.positions.get(cb_sym, 0.0)

        if held <= 0:
            # entry: MA cross up + just confirmed bull
            if bullish and ma_fast[-2] <= ma_slow[-2]:
                alloc = self.portfolio.cash * 0.95  # leave some cash for fees
                if alloc < 20:
                    return
                qty = alloc / (px * (1 + TAKER_FEE))
                self.buy(cb_sym, qty, px, reason="MA_CROSS_UP")
                self._extra["trailing_high"] = px
            return

        # held: update trailing high, check exit
        trailing_high = self._extra.get("trailing_high") or px
        trailing_high = max(trailing_high, px)
        self._extra["trailing_high"] = trailing_high

        trailing_stop = trailing_high - self.atr_mult * float(atr[-1])
        cross_down = ma_fast[-1] < ma_slow[-1] and ma_fast[-2] >= ma_slow[-2]

        if px <= trailing_stop:
            self.sell(cb_sym, reason="ATR_TRAIL")
            self._extra["trailing_high"] = None
        elif cross_down:
            self.sell(cb_sym, reason="MA_CROSS_DOWN")
            self._extra["trailing_high"] = None


# ── Passive benchmark (variants D, E) ────────────────────────────────────────
class PassiveStrategy(Strategy):
    """Fixed-weight portfolio: D = 100% BTC buy & hold; E = 60/40 monthly rebalance."""

    def __init__(
        self, name: str, label: str,
        weights: dict[str, float],
        rebalance: str | None = None,  # None | "monthly"
    ) -> None:
        super().__init__(name, label)
        self.weights = weights
        self.rebalance_freq = rebalance
        self._extra.setdefault("initialized", False)
        self._extra.setdefault("last_rebalance_month", None)

    def on_tick(self, md: MarketData) -> None:
        # First-touch: deploy cash according to weights
        if not self._extra.get("initialized"):
            self._deploy(md, "INITIAL")
            self._extra["initialized"] = True
            self._extra["last_rebalance_month"] = md.ts.strftime("%Y-%m")
            return

        # Monthly rebalance for E
        if self.rebalance_freq == "monthly":
            current_month = md.ts.strftime("%Y-%m")
            if current_month != self._extra.get("last_rebalance_month"):
                self._rebalance_to_target(md)
                self._extra["last_rebalance_month"] = current_month

    def _deploy(self, md: MarketData, reason: str) -> None:
        equity = self.portfolio.cash  # all cash on first call
        for bin_sym, w in self.weights.items():
            cb_sym = to_cb(bin_sym)
            px = md.latest_price(bin_sym)
            if px is None or px <= 0:
                continue
            target_value = equity * w
            qty = target_value / (px * (1 + TAKER_FEE))
            self.buy(cb_sym, qty, px, reason=reason)

    def _rebalance_to_target(self, md: MarketData) -> None:
        equity = self.equity()
        for bin_sym, w in self.weights.items():
            cb_sym = to_cb(bin_sym)
            px = md.latest_price(bin_sym)
            if px is None or px <= 0:
                continue
            current_qty = self.portfolio.positions.get(cb_sym, 0.0)
            current_value = current_qty * px
            target_value = equity * w
            delta_value = target_value - current_value
            if abs(delta_value) < 5:  # ignore tiny adjustments
                continue
            if delta_value > 0:
                qty = delta_value / (px * (1 + TAKER_FEE))
                self.buy(cb_sym, qty, px, reason="REBAL_BUY")
            else:
                qty_to_sell = (-delta_value) / px
                self.sell(cb_sym, reason="REBAL_SELL", qty=qty_to_sell)


# ── Factory ──────────────────────────────────────────────────────────────────
def build_strategy(spec: dict) -> Strategy:
    """Construct a Strategy instance from an ARENA_STRATEGIES dict entry."""
    t = spec["type"]
    name = spec["name"]
    label = spec.get("label", name)
    if t == "momentum":
        return MomentumStrategy(
            name=name, label=label,
            lookback=spec["lookback"], top_k=spec["top_k"],
            vol_cap=spec["vol_cap"], sl=spec["sl"],
        )
    if t == "trend":
        return TrendFollowingStrategy(
            name=name, label=label,
            ma_fast=spec.get("ma_fast", 20),
            ma_slow=spec.get("ma_slow", 50),
            atr_period=spec.get("atr_period", 14),
            atr_mult=spec.get("atr_mult", 2.0),
            symbol=spec.get("symbol", "BTC/USDT"),
        )
    if t == "passive":
        return PassiveStrategy(
            name=name, label=label,
            weights=spec["weights"],
            rebalance=spec.get("rebalance"),
        )
    raise ValueError(f"Unknown strategy type: {t}")
