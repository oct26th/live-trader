"""Live Trader v4 — Variant A Momentum Rotation Strategy.

Paper-mode first. No live orders unless DRY_RUN=false AND explicit go-live.
See CLAUDE.md §5 for strategy rationale and shipping criteria.
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone

import numpy as np

from config import (
    DRY_RUN,
    MOMENTUM_INITIAL_PAPER_CASH,
    MOMENTUM_LOOKBACK_DAYS,
    MOMENTUM_MA200_PERIOD,
    MOMENTUM_REBALANCE_WEEKDAY,
    MOMENTUM_SL,
    MOMENTUM_TOP_K,
    MOMENTUM_VOL_CAP,
    MOMENTUM_VOL_LOOKBACK_DAYS,
    PAPER_STATE_PATH,
    SYMBOL_MAP,
    TAKER_FEE,
)
from exchange import fetch_1d_closes, to_bin, to_cb
from notifier import DiscordNotifier
from portfolio import Portfolio

BTC_BIN = "BTC/USDT"
MIN_HISTORY = MOMENTUM_MA200_PERIOD + MOMENTUM_LOOKBACK_DAYS  # bars needed for signals


class MomentumTrader:
    """Weekly-rebalanced momentum rotation with BTC MA200 kill switch."""

    def __init__(self) -> None:
        initial = MOMENTUM_INITIAL_PAPER_CASH if DRY_RUN else 0.0
        self.portfolio = Portfolio(initial)
        self.notifier = DiscordNotifier(paper_mode=DRY_RUN)
        self.log = logging.getLogger("momentum")

        self._entry_px: dict[str, float] = {}
        self._peak_equity = initial
        self._prices: dict[str, float] = {}
        self._active_set: set[str] = set()  # bin_syms of current holdings
        self._last_rebalance_date: str | None = None  # YYYY-MM-DD
        self._filter_on = True  # BTC > MA200 → True

        self.running = True

    # ── lifecycle ────────────────────────────────────────────────────
    def authenticate(self) -> None:
        """Initialize — paper: load state; live: Coinbase auth (not yet enabled)."""
        if DRY_RUN:
            self.log.info("🧪 PAPER MODE — no real orders will be placed.")
            _load_paper_state(self)
        else:
            self.log.error("Live mode requested but not enabled. Set DRY_RUN=true.")
            raise RuntimeError("Live mode disabled until paper validation complete.")

    def stop(self, *_args) -> None:
        self.log.info("⏹  Stopping — flushing state.")
        self.save_state()
        self.running = False

    # ── main loop ────────────────────────────────────────────────────
    def tick(self) -> None:
        """Pull 1D closes, mark-to-market, check stops, rebalance on Sunday."""
        try:
            closes_map, btc_ma200, btc_close = self._fetch_universe()
        except Exception as exc:
            self.log.error(f"tick() fetch failed: {exc}")
            return

        if not closes_map:
            self.log.warning("No market data — skipping tick.")
            return

        # Update mark-to-market prices (cb_sym → last close)
        for bin_sym, closes in closes_map.items():
            if len(closes) > 0 and not np.isnan(closes[-1]):
                self._prices[to_cb(bin_sym)] = float(closes[-1])

        # Update peak / drawdown
        eq = self.portfolio.equity(self._prices)
        self._peak_equity = max(self._peak_equity, eq)

        # Per-coin stop-loss (check every tick, not just rebalance day)
        self._check_stop_losses()

        # Weekly rebalance
        now = datetime.now(timezone.utc)
        today_str = now.strftime("%Y-%m-%d")
        is_sunday = now.weekday() == MOMENTUM_REBALANCE_WEEKDAY
        already_today = self._last_rebalance_date == today_str
        if is_sunday and not already_today:
            self._rebalance(closes_map, btc_close, btc_ma200)
            self._last_rebalance_date = today_str

        self.save_state()

        btc_regime = {
            "regime": "FILTER_ON" if self._filter_on else "FILTER_OFF",
            "adx": 0,
            "trend": "bullish" if self._filter_on else "bearish",
        }
        self.notifier.send(self, btc_regime)

    # ── data ─────────────────────────────────────────────────────────
    def _fetch_universe(self) -> tuple[dict[str, np.ndarray], float | None, float | None]:
        """Return {bin_sym: 1D closes}, BTC MA200, BTC last close."""
        closes_map: dict[str, np.ndarray] = {}
        for bin_sym in SYMBOL_MAP.keys():
            closes = fetch_1d_closes(bin_sym, limit=MIN_HISTORY + 20)
            if closes is None or len(closes) < MIN_HISTORY:
                continue  # new listing or fetch failed
            closes_map[bin_sym] = closes
            time.sleep(0.1)  # gentle rate-limit

        btc = closes_map.get(BTC_BIN)
        if btc is None or len(btc) < MOMENTUM_MA200_PERIOD:
            return closes_map, None, None
        btc_ma200 = float(np.mean(btc[-MOMENTUM_MA200_PERIOD:]))
        btc_close = float(btc[-1])
        return closes_map, btc_ma200, btc_close

    # ── risk ─────────────────────────────────────────────────────────
    def _check_stop_losses(self) -> None:
        """Sell any position that breached -10% from entry."""
        for cb_sym in list(self.portfolio.positions.keys()):
            px = self._prices.get(cb_sym)
            entry = self._entry_px.get(cb_sym)
            if px is None or entry is None or entry <= 0:
                continue
            pnl_pct = (px - entry) / entry
            if pnl_pct <= MOMENTUM_SL:
                self._sell(cb_sym, reason="SL")

    # ── rebalance ────────────────────────────────────────────────────
    def _rebalance(
        self,
        closes_map: dict[str, np.ndarray],
        btc_close: float | None,
        btc_ma200: float | None,
    ) -> None:
        # Market filter
        if btc_close is None or btc_ma200 is None or btc_close <= btc_ma200:
            self._filter_on = False
            self.log.warning(
                f"🛑 Filter OFF — BTC ${btc_close} ≤ MA200 ${btc_ma200}. Liquidating."
            )
            for cb_sym in list(self.portfolio.positions.keys()):
                self._sell(cb_sym, reason="FILTER_OFF")
            self._active_set = set()
            return

        self._filter_on = True

        # Score universe
        scored: list[tuple[str, float]] = []
        for bin_sym, closes in closes_map.items():
            if len(closes) < MIN_HISTORY:
                continue
            # Volatility filter (daily σ over 30d)
            recent = closes[-MOMENTUM_VOL_LOOKBACK_DAYS - 1 :]
            if np.any(recent <= 0):
                continue
            daily_ret = np.diff(np.log(recent))
            vol = float(np.std(daily_ret))
            if vol > MOMENTUM_VOL_CAP:
                continue
            # 30d log return
            r = float(np.log(closes[-1] / closes[-MOMENTUM_LOOKBACK_DAYS - 1]))
            if not np.isfinite(r):
                continue
            scored.append((bin_sym, r))

        scored.sort(key=lambda x: x[1], reverse=True)
        top = [s for s, _ in scored[:MOMENTUM_TOP_K]]
        self.log.info(f"📊 Rebalance: top-{MOMENTUM_TOP_K} = {top}")

        # Sell anything not in top
        for cb_sym in list(self.portfolio.positions.keys()):
            if to_bin(cb_sym) not in top:
                self._sell(cb_sym, reason="ROTATE")

        # Buy new names to reach top-K
        self._active_set = set(top)
        new_syms = [b for b in top if to_cb(b) not in self.portfolio.positions]
        if not new_syms:
            return
        equity_now = self.portfolio.equity(self._prices)
        target_per_name = equity_now / MOMENTUM_TOP_K
        for bin_sym in new_syms:
            cb_sym = to_cb(bin_sym)
            px = float(closes_map[bin_sym][-1])
            if px <= 0:
                continue
            alloc = min(target_per_name, self.portfolio.cash)
            if alloc < 20:
                continue
            qty = alloc / (px * (1 + TAKER_FEE))
            self._buy(cb_sym, qty, px)

    # ── orders ───────────────────────────────────────────────────────
    def _buy(self, cb_sym: str, qty: float, px: float) -> None:
        cost = qty * px * (1 + TAKER_FEE)
        if cost > self.portfolio.cash:
            return
        if DRY_RUN:
            self.portfolio.cash -= cost
            self.portfolio.positions[cb_sym] = self.portfolio.positions.get(cb_sym, 0.0) + qty
            self._entry_px[cb_sym] = px
            self.log.info(f"🧪 [PAPER] BUY {cb_sym} qty={qty:.6f} @ ${px:.4f} cost=${cost:.2f}")
        else:
            raise NotImplementedError("Live orders disabled until paper validation.")

    def _sell(self, cb_sym: str, reason: str = "") -> None:
        qty = self.portfolio.positions.get(cb_sym, 0.0)
        if qty <= 0:
            return
        px = self._prices.get(cb_sym, 0.0)
        if px <= 0:
            return
        entry = self._entry_px.get(cb_sym, px)
        pnl_pct = (px - entry) / entry if entry > 0 else 0.0
        proceeds = qty * px * (1 - TAKER_FEE)
        if DRY_RUN:
            self.portfolio.cash += proceeds
            self.portfolio.trades.append(
                {
                    "sym": cb_sym,
                    "qty": qty,
                    "entry_px": entry,
                    "exit_px": px,
                    "pnl_pct": pnl_pct,
                    "reason": reason,
                    "ts": datetime.now(timezone.utc).isoformat(),
                }
            )
            del self.portfolio.positions[cb_sym]
            self._entry_px.pop(cb_sym, None)
            self.log.info(
                f"🧪 [PAPER] SELL {cb_sym} qty={qty:.6f} @ ${px:.4f} "
                f"pnl={pnl_pct*100:+.2f}% reason={reason}"
            )
        else:
            raise NotImplementedError("Live orders disabled until paper validation.")

    # ── persistence ──────────────────────────────────────────────────
    @property
    def prices(self) -> dict[str, float]:
        """Notifier expects trader.prices."""
        return self._prices

    def save_state(self) -> None:
        _save_paper_state(self)


# ── state file helpers (kept inline to avoid cross-module coupling) ──
def _save_paper_state(trader: MomentumTrader) -> None:
    state = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "dry_run": DRY_RUN,
        "cash": trader.portfolio.cash,
        "positions": trader.portfolio.positions,
        "entry_px": trader._entry_px,
        "peak_equity": trader._peak_equity,
        "active_set": sorted(trader._active_set),
        "filter_on": trader._filter_on,
        "last_rebalance_date": trader._last_rebalance_date,
        "trade_count": len(trader.portfolio.trades),
        "trades_tail": trader.portfolio.trades[-50:],
    }
    os.makedirs(os.path.dirname(PAPER_STATE_PATH), exist_ok=True)
    with open(PAPER_STATE_PATH, "w") as f:
        json.dump(state, f, indent=2, default=str)


def _load_paper_state(trader: MomentumTrader) -> None:
    if not os.path.exists(PAPER_STATE_PATH):
        trader.log.info(f"No paper state at {PAPER_STATE_PATH} — starting fresh.")
        return
    try:
        with open(PAPER_STATE_PATH) as f:
            state = json.load(f)
    except (json.JSONDecodeError, IOError) as exc:
        trader.log.warning(f"Paper state corrupted: {exc} — starting fresh.")
        return

    trader.portfolio.cash = state.get("cash", MOMENTUM_INITIAL_PAPER_CASH)
    trader.portfolio.positions = state.get("positions", {})
    trader._entry_px = state.get("entry_px", {})
    trader._peak_equity = state.get("peak_equity", MOMENTUM_INITIAL_PAPER_CASH)
    trader._active_set = set(state.get("active_set", []))
    trader._filter_on = state.get("filter_on", True)
    trader._last_rebalance_date = state.get("last_rebalance_date")
    trader.portfolio.trades = state.get("trades_tail", [])
    trader.log.info(
        f"🔁 Paper state restored — cash=${trader.portfolio.cash:.2f} "
        f"positions={len(trader.portfolio.positions)} "
        f"last_rebalance={trader._last_rebalance_date}"
    )
