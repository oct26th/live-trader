"""Live Trader v4 — Core Trading Logic"""
import time
import json
import logging
from datetime import datetime
from threading import Event

from config import (
    SYMBOL_MAP,
    CB_TO_BIN,
    REGIME_PARAMS,
    ADX_THRESHOLD,
    MAX_DRAWDOWN,
    PAUSE_DURATION,
    RESERVE_CASH_PCT,
    ROTATION_INTERVAL,
    SCORE_INTERVAL,
    BLACKLIST_DURATION,
    TAKER_FEE,
    MIN_NET_PROFIT,
)
from portfolio import Portfolio
from indicators import compute, signal_at
from exchange import get_coinbase_exchange, fetch_ohlcv, fetch_close, to_cb, to_bin
from state import save_state, load_state
from notifier import DiscordNotifier
import numpy as np


class LiveTrader:
    """Main trading engine."""

    def __init__(self, params_path, capital_each=50.0):
        with open(params_path) as f:
            self.params = json.load(f)
        self.capital_each = capital_each
        self.ex = None
        self.portfolio = None
        self.running = Event()
        self.prices = {}
        self.check_secs = 300

        # Internal state
        self._entry_px = {}
        self._peak_equity = None
        self._pause_until = None
        self._partial_sells = {}
        self._pool_scores = {}
        self._active_set = set()
        self._last_rotation = None
        self._last_score_at = None
        self._blacklist = {}
        self._rotation_count = 0
        self._regime_cfg = REGIME_PARAMS["UNKNOWN"]
        self._last_notify_at = None

        # Logging
        self.log = logging.getLogger("live_trader")

        # Notifier
        self.notifier = DiscordNotifier()

    def authenticate(self):
        """Initialize exchange and portfolio."""
        self.ex = get_coinbase_exchange()
        bal = self.ex.fetch_balance()
        usd = float(bal['free'].get('USD', 0))
        self.portfolio = Portfolio(usd)
        self._peak_equity = usd
        self.log.info(f"🔴 LIVE MODE v4 | USD balance: ${usd:.2f} | Pool: 20 coins")
        for bin_sym, cb_sym in SYMBOL_MAP.items():
            coin = cb_sym.split('-')[0]
            qty = float(bal['free'].get(coin, 0))
            if qty > 0.001:
                self.portfolio.positions[cb_sym] = qty
                self.log.info(f"   Loaded position: {cb_sym} qty={qty:.6f}")
        self.log.info(f"   Pool: {list(SYMBOL_MAP.keys())}")
        load_state(self)
        return True

    def _btc_regime(self):
        """Detect BTC market regime via ADX."""
        try:
            closes, highs, lows = fetch_ohlcv('BTC/USDT', '4h', limit=100)
            if closes is None or len(closes) < 60:
                return {"regime": "UNKNOWN", "adx": 0, "trend": "N/A"}
            p = {
                "ma_fast": 20,
                "ma_slow": 50,
                "macd_fast": 12,
                "macd_slow": 26,
                "macd_signal": 9,
                "rsi_period": 14,
            }
            ind = compute(closes, p, highs, lows)
            adx = float(ind["adx"][-1])
            ma_f = ind["ma_f"][-1]
            ma_s = ind["ma_s"][-1]
            mac_h = ind["macd_h"][-1]
            if adx < 25:
                regime = "RANGE"
            elif adx > 40:
                regime = "TRENDING"
            else:
                regime = "TRANSITION"
            trend = (
                "bullish"
                if ma_f > ma_s and mac_h > 0
                else "bearish"
                if ma_f < ma_s and mac_h < 0
                else "neutral"
            )
            return {"regime": regime, "adx": round(adx, 1), "trend": trend}
        except Exception as e:
            self.log.warning(f"   BTC regime detection failed: {e}")
            return {"regime": "UNKNOWN", "adx": 0, "trend": "N/A"}

    def _apply_regime(self, btc_regime):
        """Get regime parameters from config."""
        return REGIME_PARAMS.get(btc_regime["regime"], REGIME_PARAMS["UNKNOWN"])

    def _update_peak_and_check_drawdown(self):
        eq = self.portfolio.equity(self.prices)
        if self._peak_equity is None:
            self._peak_equity = eq
            return
        if eq > self._peak_equity:
            self._peak_equity = eq
        drawdown = (eq / self._peak_equity - 1) * 100
        if drawdown <= MAX_DRAWDOWN and self._pause_until is None:
            self._pause_until = time.time() + PAUSE_DURATION
            self.log.warning(
                f"⚠️  MAX DRAWDOWN {drawdown:.2f}% — Paused until "
                f"{datetime.fromtimestamp(self._pause_until).strftime('%Y-%m-%d %H:%M UTC')}"
            )

    def _is_paused(self):
        if self._pause_until is None:
            return False
        if time.time() >= self._pause_until:
            self._pause_until = None
            self.log.info("✅ Trading pause ended.")
            return False
        return True

    def _check_stop_loss(self, cb_sym, price):
        entry = self._entry_px.get(cb_sym)
        if entry is None:
            return False
        sl = self._regime_cfg["sl"] * 100
        pnl_pct = (price - entry) / entry * 100
        if pnl_pct <= sl:
            self.log.info(f"🛑 STOP-LOSS {cb_sym} PnL={pnl_pct:.2f}% (SL={sl:.1f}%)")
            return True
        return False

    def _check_take_profit(self, cb_sym, price):
        entry = self._entry_px.get(cb_sym)
        if entry is None:
            return None
        tp1 = self._regime_cfg["tp1"] * 100
        tp2 = self._regime_cfg["tp2"] * 100
        pnl_pct = (price - entry) / entry * 100
        ps = self._partial_sells.setdefault(
            cb_sym, {"tp1_done": False, "tp2_done": False}
        )
        if not ps["tp1_done"] and pnl_pct >= tp1:
            ps["tp1_done"] = True
            return "TP1"
        if ps["tp1_done"] and not ps["tp2_done"] and pnl_pct >= tp2:
            ps["tp2_done"] = True
            return "TP2"
        return None

    def _score_pool(self):
        scores = {}
        for bin_sym, cb_sym in SYMBOL_MAP.items():
            raw = self.params.get(bin_sym, {})
            p = raw["params"] if "params" in raw else raw
            if not p:
                continue
            closes = fetch_close(bin_sym)
            if closes is None:
                continue
            price = closes[-1]
            self.prices[bin_sym] = price
            self.prices[cb_sym] = price
            ind = compute(closes, p)
            rsi = ind["rsi"][-1]
            ma_cross = ind["ma_cross"][-1]
            macd_h = ind["macd_h"][-1]
            rsi_score = max(0, 50 - rsi) * 0.30
            trend_score = (1.0 if ma_cross == 1 else 0.0) * 30
            macd_score = (1.0 if macd_h > 0 else 0.0) * 20
            rets = np.diff(closes[-20:]) / closes[-20:-1]
            vol = np.std(rets) * 100 if len(rets) > 1 else 0
            vol_score = min(vol * 5, 20) * 0.20
            scores[bin_sym] = round(rsi_score + trend_score + macd_score + vol_score, 2)
        return scores

    def _prune_blacklist(self):
        now = time.time()
        expired = [k for k, v in self._blacklist.items() if now >= v]
        for k in expired:
            del self._blacklist[k]
        if expired:
            self.log.info(f"   Blacklist cleared: {expired}")

    def _should_rotate(self):
        if self._last_rotation is None:
            return True
        return (time.time() - self._last_rotation) >= ROTATION_INTERVAL

    def _do_rotation(self):
        self._rotation_count += 1
        self._last_rotation = time.time()
        self._prune_blacklist()
        now = time.time()

        scores = self._score_pool()
        self._pool_scores = scores

        held_bins = {CB_TO_BIN[c] for c in self.portfolio.positions.keys()}
        eligible = {
            k: v
            for k, v in scores.items()
            if k not in self._blacklist and k not in held_bins
        }

        ranked = sorted(eligible.items(), key=lambda x: x[1], reverse=True)
        top_bins = [k for k, _ in ranked[: self._regime_cfg["max_active"]]]
        self._active_set = set(top_bins)

        self.log.info(f"\n{'='*56}")
        self.log.info(f"  🔄 ROTATION #{self._rotation_count}")
        self.log.info(f"{'='*56}")
        self.log.info("  Scores (top 20):")
        all_ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        for i, (k, v) in enumerate(all_ranked):
            tag = (
                "★ ACTIVE"
                if k in self._active_set
                else ("✗ HOLD" if k in held_bins else "")
            )
            in_bl = " [BLACKLIST]" if k in self._blacklist else ""
            self.log.info(f"    {i+1:2d}. {k:12s} {v:5.2f} {tag}{in_bl}")
        self.log.info(f"  Active: {list(self._active_set)}")
        self.log.info(
            f"  Next rotation in: {ROTATION_INTERVAL // (24 * 3600)} days\n"
        )

        for cb_sym in list(self.portfolio.positions.keys()):
            bin_sym = CB_TO_BIN.get(cb_sym, cb_sym)
            if bin_sym not in self._active_set:
                price = self.prices.get(bin_sym, 0) or self.prices.get(cb_sym, 0)
                if price > 0:
                    self.log.info(f"🔴 ROTATION SELL {cb_sym} — no longer in active set")
                    self._sell(bin_sym, price, reason="ROTATION")

    def _buy(self, bin_sym, price, qty, weight=1.0, rsi=50):
        cb = to_cb(bin_sym)
        alloc_usd = qty * price
        self.log.info(
            f"🟢 BUY  {cb:10s}  qty={qty:.6f}  @ ${price:.2f}  "
            f"alloc=${alloc_usd:.2f}  RSI={rsi:.1f}  weight={weight:.0%}"
        )
        try:
            r = self.ex.create_market_buy_order(cb, None, {"cost": alloc_usd})
            fills = r.get("trades", [])
            actual_qty = (
                sum(f.get("amount", 0) for f in fills)
                if fills
                else alloc_usd / price * 0.999
            )
            self.portfolio.cash -= alloc_usd
            self.portfolio.positions[cb] = self.portfolio.positions.get(cb, 0) + actual_qty
            self._entry_px[cb] = price
            self._partial_sells[cb] = {"tp1_done": False, "tp2_done": False}
            self.portfolio.trades.append({
                "time": datetime.now().isoformat(),
                "pair": cb,
                "side": "BUY",
                "qty": actual_qty,
                "price": price,
                "rsi": round(rsi, 1),
                "weight": round(weight, 2),
                "alloc_usd": round(alloc_usd, 2),
            })
            self.log.info(f"   ✅ Filled qty={actual_qty:.6f}")
        except Exception as e:
            self.log.error(f"   BUY FAILED: {e}")

    def _sell(self, bin_sym, price, reason="SIGNAL", qty=None):
        cb = to_cb(bin_sym)
        pos_qty = self.portfolio.positions.get(cb, 0)
        if pos_qty <= 0:
            return
        actual_qty = qty if qty is not None else pos_qty
        entry = self._entry_px.get(cb, price)
        pnl = (price - entry) / entry * 100
        proceeds = actual_qty * price * 0.994
        fee = actual_qty * price * 0.006
        self.log.info(
            f"🔴 SELL [{reason}] {cb:10s}  qty={actual_qty:.6f}  @ ${price:.2f}  "
            f"PnL={pnl:+.2f}%  fee=${fee:.2f}"
        )
        try:
            r = self.ex.create_market_sell_order(cb, actual_qty)
            self.portfolio.cash += proceeds
            new_qty = pos_qty - actual_qty
            if new_qty <= 0.001:
                self.portfolio.positions.pop(cb, None)
                self._entry_px.pop(cb, None)
                self._partial_sells.pop(cb, None)
            else:
                self.portfolio.positions[cb] = new_qty
            self.portfolio.trades.append({
                "time": datetime.now().isoformat(),
                "pair": cb,
                "side": "SELL",
                "qty": actual_qty,
                "price": price,
                "entry": entry,
                "pnl": round(pnl, 2),
                "fee": round(fee, 2),
                "reason": reason,
            })
            self.log.info(f"   Order OK: {r.get('id', '?')}")
        except Exception as e:
            self.log.error(f"   SELL FAILED: {e}")

    def tick(self):
        """Main trading loop iteration."""
        self._update_peak_and_check_drawdown()

        if self._is_paused():
            eq = self.portfolio.equity(self.prices)
            self.log.info(
                f"[PAUSED] Equity=${eq:.2f}  Until {datetime.fromtimestamp(self._pause_until).strftime('%H:%M UTC')}"
            )
            save_state(self)
            return

        now = time.time()

        if (
            self._last_score_at is None
            or (now - self._last_score_at) >= SCORE_INTERVAL
        ):
            self._pool_scores = self._score_pool()
            self._last_score_at = now
            self.log.info(
                f"[POOL SCORE] Updated at {datetime.now().strftime('%H:%M UTC')}"
            )

        if self._should_rotate():
            self._do_rotation()

        self.log.info(f"\n{'─'*50} {datetime.now().strftime('%H:%M UTC')}")

        btc_regime = self._btc_regime()
        self._regime_cfg = self._apply_regime(btc_regime)
        regime_cfg = self._regime_cfg
        regime_tag = f"[{btc_regime['regime']} BTC ADX={btc_regime['adx']} {btc_regime['trend']}]"
        self.log.info(
            f"  {regime_tag}  Trend={'✓ ON' if regime_cfg['allow_trend'] else '✗ OFF'}  "
            f"MaxActive={regime_cfg['max_active']}  SL={regime_cfg['sl']*100:.0f}%  "
            f"TP1={regime_cfg['tp1']*100:.0f}%  TP2={regime_cfg['tp2']*100:.0f}%"
        )

        eq = self.portfolio.equity(self.prices)

        for bin_sym, cb_sym in SYMBOL_MAP.items():
            raw = self.params.get(bin_sym, {})
            p = raw["params"] if "params" in raw else raw
            if not p:
                continue
            closes, highs, lows = fetch_ohlcv(bin_sym)
            if closes is None:
                continue

            price = closes[-1]
            self.prices[bin_sym] = price
            self.prices[cb_sym] = price
            ind = compute(closes, p, highs, lows)
            sig = signal_at(ind, len(closes) - 1, p)
            rsi = ind["rsi"][-1]
            adx = ind["adx"][-1]

            in_pos = cb_sym in self.portfolio.positions
            is_active = bin_sym in self._active_set

            if in_pos:
                if self._check_stop_loss(cb_sym, price):
                    self._blacklist[bin_sym] = now + BLACKLIST_DURATION
                    self._sell(bin_sym, price, reason="STOP_LOSS")
                    self._active_set.discard(bin_sym)
                    self.log.warning(f"   🚫 {bin_sym} blacklisted for 2 weeks")
                    continue

                tp = self._check_take_profit(cb_sym, price)
                if tp == "TP1":
                    pos_qty = self.portfolio.positions[cb_sym]
                    sell_qty = round(pos_qty * 0.50, 6)
                    self._sell(bin_sym, price, reason="TP1", qty=sell_qty)
                    self.log.info(
                        f"   🎯 TP1 hit — sold 50% ({sell_qty:.4f}), keeping rest"
                    )
                    continue
                if tp == "TP2":
                    self._sell(bin_sym, price, reason="TP2")
                    self._active_set.discard(bin_sym)
                    self.log.info(f"   🎯 TP2 hit — sold remaining, slot freed")
                    continue

                if sig == "SELL":
                    self._sell(bin_sym, price, reason="SIGNAL")
                    self._active_set.discard(bin_sym)

            else:
                if not is_active:
                    continue

                rsi_buy = p.get("rsi_buy", 38)
                pullback_4h = sig == "BUY" and rsi < rsi_buy

                mac = ind["ma_cross"][-1]
                mac_h = ind["macd_h"][-1]
                trend_4h = (
                    regime_cfg["allow_trend"]
                    and mac == 1
                    and mac_h > 0
                    and 40 <= rsi <= 70
                    and adx > ADX_THRESHOLD
                )

                entry_ok = pullback_4h or trend_4h
                entry_reason = ""
                if pullback_4h:
                    entry_reason = "PULLBACK_4H"
                elif trend_4h:
                    entry_reason = "TREND_4H"

                if not entry_ok:
                    continue

                tp2_raw = regime_cfg["tp2"]
                tp1_raw = regime_cfg["tp1"]
                total_fee_pct = TAKER_FEE * (1 + tp1_raw / 2 + tp2_raw / 2)
                net_tp2 = tp2_raw - total_fee_pct
                if net_tp2 < MIN_NET_PROFIT:
                    self.log.info(
                        f"   ⛔ {bin_sym} skipped — TP2 net={net_tp2*100:.1f}% < min({MIN_NET_PROFIT*100:.0f}%)"
                    )
                    continue

                rsi_floor = 20
                weight = max(
                    0.0,
                    min(1.0, (rsi_buy - rsi) / (rsi_buy - rsi_floor)),
                )
                if weight <= 0.1:
                    continue

                reserve = eq * RESERVE_CASH_PCT
                available = eq - reserve
                existing_pos_value = sum(
                    self.portfolio.positions[s] * self.prices.get(s, 0)
                    for s in self.portfolio.positions
                )
                investable = available - existing_pos_value
                max_alloc = investable / regime_cfg["max_active"]
                min_alloc = 20.0
                alloc = max(
                    min_alloc, min(weight * available * 0.20, max_alloc)
                )

                if self.portfolio.cash >= alloc:
                    qty = alloc / price
                    self.log.info(
                        f"   → ENTRY {entry_reason} {bin_sym} (RSI={rsi:.0f}, ADX={adx:.0f})"
                    )
                    self._buy(bin_sym, price, qty, weight, rsi)

        eq = self.portfolio.equity(self.prices)
        ret = (eq / self.portfolio.initial - 1) * 100
        dd = (eq / self._peak_equity - 1) * 100 if self._peak_equity else 0
        active = list(self.portfolio.positions.keys())
        self.log.info(
            f"[PORT] Equity=${eq:.2f}  Ret={ret:+.2f}%  DD={dd:+.2f}%  "
            f"Cash=${self.portfolio.cash:.2f}  Active={active}"
        )
        save_state(self)
        self.notifier.send(self, btc_regime)

    def run(self):
        """Start the trading loop."""
        self.authenticate()
        self.log.info(f"\n{'='*60}")
        self.log.info(f"  LIVE TRADER v4 — Observation Pool + Rotation")
        self.log.info(
            f"  Pool: 20 coins | Active: max {self._regime_cfg['max_active']} | "
            f"Reserve: {RESERVE_CASH_PCT:.0%} cash"
        )
        self.log.info(
            f"  Rotation: every {ROTATION_INTERVAL // (24*3600)} days | "
            f"Score update: hourly"
        )
        self.log.info(
            f"  Stop-loss: {self._regime_cfg['sl']*100:.0f}% | "
            f"Max DD: {MAX_DRAWDOWN}%"
        )
        self.log.info(
            f"  TP1: +{self._regime_cfg['tp1']*100:.0f}% (sell 50%) | "
            f"TP2: +{self._regime_cfg['tp2']*100:.0f}% (sell all)"
        )
        self.log.info(f"{'='*60}\n")
        self.running.set()
        self.tick()
        import schedule
        schedule.every(self.check_secs).seconds.do(self.tick)
        schedule.every(1).hours.do(save_state, self)
        import time as t
        while self.running.is_set():
            schedule.run_pending()
            t.sleep(10)

    def stop(self):
        """Graceful shutdown."""
        self.log.info("Stopping...")
        self.running.clear()
        save_state(self)
