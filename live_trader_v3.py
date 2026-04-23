"""
Live Trader v3 — Observation Pool + Rotation System
20 coins, top 3 active positions, bi-weekly rotation, all v2 risk protections.
"""
import os
os.environ["TQDM_DISABLE"] = "1"
import sys, json, time, logging, signal, schedule
from datetime import datetime, timedelta
from threading import Event
from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
load_dotenv("/opt/data/trading_bot/.env")

LOG_DIR = "/tmp/trading_logs"
os.makedirs(LOG_DIR, exist_ok=True)
log = logging.getLogger("live_trader")
log.setLevel(logging.INFO)
fh = logging.FileHandler(f"{LOG_DIR}/live_{datetime.now().strftime('%Y%m%d')}.log")
fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
log.addHandler(fh)
log.addHandler(logging.StreamHandler())


# ── 20-Coin Observation Pool ─────────────────────────────────────────────────
SYMBOL_MAP = {
    "BTC/USDT":  "BTC-USD",
    "ETH/USDT":  "ETH-USD",
    "SOL/USDT":  "SOL-USD",
    "XRP/USDT":  "XRP-USD",
    "ADA/USDT":  "ADA-USD",
    "AVAX/USDT": "AVAX-USD",
    "LINK/USDT": "LINK-USD",
    "APT/USDT":  "APT-USD",
    "NEAR/USDT": "NEAR-USD",
    "DOT/USDT":  "DOT-USD",
    "ICP/USDT":  "ICP-USD",
    "ATOM/USDT": "ATOM-USD",
    "OP/USDT":   "OP-USD",
    "DOGE/USDT": "DOGE-USD",
    "LTC/USDT":  "LTC-USD",
    "ARB/USDT":  "ARB-USD",
    "TIA/USDT":  "TIA-USD",
    "SUI/USDT":  "SUI-USD",
    "INJ/USDT":  "INJ-USD",
    "WIF/USDT":  "WIF-USD",
}

# Reverse map: cb_sym -> bin_sym
CB_TO_BIN = {v: k for k, v in SYMBOL_MAP.items()}


# ── Exchange ─────────────────────────────────────────────────────────────────
def get_exchange():
    import ccxt
    ex = ccxt.coinbaseadvanced({
        'apiKey':    os.getenv('COINBASE_API_KEY'),
        'secret':    os.getenv('COINBASE_API_SECRET'),
        'options':   {'apiType': 'advanced',
                      'createMarketBuyOrderRequiresPrice': False},
        'enableRateLimit': True,
    })
    return ex


# ── Indicators ────────────────────────────────────────────────────────────
import numpy as np

def _ema(arr, span):
    a = 2/(span+1)
    out = np.empty(len(arr), dtype=float)
    out[0] = arr[0]
    for i in range(1, len(arr)): out[i] = a*arr[i]+(1-a)*out[i-1]
    return out

def _rsi(arr, period=14):
    d = np.diff(arr, prepend=arr[0])
    g, l = np.maximum(d,0.0), np.maximum(-d,0.0)
    ag, al = np.empty(len(arr),dtype=float), np.empty(len(arr),dtype=float)
    ag[0], al[0] = g[0], l[0]
    for i in range(1,len(arr)):
        ag[i]=(ag[i-1]*(period-1)+g[i])/period
        al[i]=(al[i-1]*(period-1)+l[i])/period
    rs=np.divide(ag,al,out=np.zeros_like(ag),where=al!=0)
    return 100-(100/(1+rs))

def compute(close, p):
    ma_f = _ema(close, p["ma_fast"])
    ma_s = _ema(close, p["ma_slow"])
    mline= _ema(close,p["macd_fast"])-_ema(close,p["macd_slow"])
    msig = _ema(mline,p["macd_signal"])
    macd_h = mline-msig
    rsi_v = _rsi(close,p["rsi_period"])
    mac = np.where(ma_f>ma_s,1.0,np.where(ma_f<ma_s,-1.0,0.0))
    mcx = np.zeros(len(close),dtype=float)
    for i in range(1,len(close)):
        if macd_h[i]>0>macd_h[i-1]: mcx[i]=1.0
        elif macd_h[i]<0<macd_h[i-1]: mcx[i]=-1.0
    return {"ma_f":ma_f,"ma_s":ma_s,"macd_h":macd_h,"mcross":mcx,"rsi":rsi_v,"ma_cross":mac}

def signal_at(ind, i, p):
    mac,mac_h,mx,rsi = ind["ma_cross"][i],ind["macd_h"][i],ind["mcross"][i],ind["rsi"][i]
    c1 = (mac==1) and (mac_h>0) and (rsi<p["rsi_buy"])
    c2 = (mx==1) and (mac==1) and (30<rsi<55)
    if c1 or c2: return "BUY"
    if rsi>p["rsi_sell"] or mac<0: return "SELL"
    return "HOLD"


# ── Portfolio ───────────────────────────────────────────────────────────────
class Portfolio:
    def __init__(self, initial_usd):
        self.initial = initial_usd
        self.cash    = initial_usd
        self.positions = {}   # {cb_sym: qty}
        self.trades  = []

    def equity(self, prices):
        return self.cash + sum(qty*prices.get(s,0) for s,qty in self.positions.items())


# ── Trader ──────────────────────────────────────────────────────────────────
class LiveTrader:
    STOP_LOSS_PCT    = -8.0
    MAX_DRAWDOWN      = -15.0
    PAUSE_DURATION    = 7 * 24 * 3600
    TP1_PCT           = 5.0
    TP2_PCT           = 10.0
    MAX_ACTIVE        = 3
    RESERVE_CASH_PCT  = 0.20
    ROTATION_INTERVAL  = 14 * 24 * 3600
    SCORE_INTERVAL     = 3600
    BLACKLIST_DURATION = 14 * 24 * 3600

    def __init__(self, params_path, capital_each=50.0):
        with open(params_path) as f:
            self.params = json.load(f)
        self.capital_each   = capital_each
        self.ex             = None
        self.portfolio      = None
        self.running         = Event()
        self.prices         = {}
        self.check_secs      = 300
        self._entry_px      = {}    # {cb_sym: entry_price}
        self._peak_equity   = None
        self._pause_until   = None
        self._partial_sells = {}    # {cb_sym: {'tp1_done', 'tp2_done'}}
        self._pool_scores   = {}    # {bin_sym: score}
        self._active_set    = set() # {bin_sym}
        self._last_rotation = None
        self._last_score_at = None
        self._blacklist     = {}    # {bin_sym: unlock_ts}
        self._rotation_count = 0

    def authenticate(self):
        self.ex = get_exchange()
        bal = self.ex.fetch_balance()
        usd = float(bal['free'].get('USD', 0))
        self.portfolio = Portfolio(usd)
        self._peak_equity = usd
        log.info(f"🔴 LIVE MODE v3 | USD balance: ${usd:.2f} | Pool: 20 coins")
        for bin_sym, cb_sym in SYMBOL_MAP.items():
            coin = cb_sym.split('-')[0]
            qty = float(bal['free'].get(coin, 0))
            if qty > 0.001:
                self.portfolio.positions[cb_sym] = qty
                log.info(f"   Loaded position: {cb_sym} qty={qty:.6f}")
        log.info(f"   Pool: {list(SYMBOL_MAP.keys())}")
        return True

    def fetch_close(self, bin_sym):
        import ccxt
        ex = ccxt.binance({'enableRateLimit': True})
        data = ex.fetch_ohlcv(bin_sym, '4h', limit=300)
        if not data: return None
        return np.array([c[4] for c in data], dtype=float)

    # ── Risk helpers ──────────────────────────────────────────────────────

    def _update_peak_and_check_drawdown(self):
        eq = self.portfolio.equity(self.prices)
        if self._peak_equity is None:
            self._peak_equity = eq
            return
        if eq > self._peak_equity:
            self._peak_equity = eq
        drawdown = (eq / self._peak_equity - 1) * 100
        if drawdown <= self.MAX_DRAWDOWN and self._pause_until is None:
            self._pause_until = time.time() + self.PAUSE_DURATION
            log.warning(
                f"⚠️  MAX DRAWDOWN {drawdown:.2f}% — Paused until "
                f"{datetime.fromtimestamp(self._pause_until).strftime('%Y-%m-%d %H:%M UTC')}"
            )

    def _is_paused(self):
        if self._pause_until is None: return False
        if time.time() >= self._pause_until:
            self._pause_until = None
            log.info("✅ Trading pause ended.")
            return False
        return True

    def _check_stop_loss(self, cb_sym, price):
        entry = self._entry_px.get(cb_sym)
        if entry is None: return False
        pnl_pct = (price - entry) / entry * 100
        if pnl_pct <= self.STOP_LOSS_PCT:
            log.info(f"🛑 STOP-LOSS {cb_sym} PnL={pnl_pct:.2f}%")
            return True
        return False

    def _check_take_profit(self, cb_sym, price):
        entry = self._entry_px.get(cb_sym)
        if entry is None: return None
        pnl_pct = (price - entry) / entry * 100
        ps = self._partial_sells.setdefault(cb_sym, {'tp1_done': False, 'tp2_done': False})
        if not ps['tp1_done'] and pnl_pct >= self.TP1_PCT:
            ps['tp1_done'] = True
            return 'TP1'
        if ps['tp1_done'] and not ps['tp2_done'] and pnl_pct >= self.TP2_PCT:
            ps['tp2_done'] = True
            return 'TP2'
        return None

    # ── Pool scoring ───────────────────────────────────────────────────────

    def _score_pool(self):
        scores = {}
        for bin_sym, cb_sym in SYMBOL_MAP.items():
            raw = self.params.get(bin_sym, {})
            p = raw["params"] if "params" in raw else raw
            if not p: continue
            closes = self.fetch_close(bin_sym)
            if closes is None: continue
            price = closes[-1]
            self.prices[bin_sym] = price
            self.prices[cb_sym] = price
            ind = compute(closes, p)
            rsi = ind["rsi"][-1]
            ma_cross = ind["ma_cross"][-1]
            macd_h = ind["macd_h"][-1]
            rsi_score  = max(0, 50 - rsi) * 0.30
            trend_score = (1.0 if ma_cross == 1 else 0.0) * 30
            macd_score  = (1.0 if macd_h > 0 else 0.0) * 20
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
            log.info(f"   Blacklist cleared: {expired}")

    def _should_rotate(self):
        if self._last_rotation is None: return True
        return (time.time() - self._last_rotation) >= self.ROTATION_INTERVAL

    def _do_rotation(self):
        self._rotation_count += 1
        self._last_rotation = time.time()
        self._prune_blacklist()
        now = time.time()

        scores = self._score_pool()
        self._pool_scores = scores

        # portfolio.positions keys are cb_sym; blacklist/active are bin_sym
        held_bins = {CB_TO_BIN[c] for c in self.portfolio.positions.keys()}
        eligible = {
            k: v for k, v in scores.items()
            if k not in self._blacklist and k not in held_bins
        }

        ranked = sorted(eligible.items(), key=lambda x: x[1], reverse=True)
        top_bins = [k for k, _ in ranked[:self.MAX_ACTIVE]]
        self._active_set = set(top_bins)

        log.info(f"\n{'='*56}")
        log.info(f"  🔄 ROTATION #{self._rotation_count}")
        log.info(f"{'='*56}")
        log.info("  Scores (top 20):")
        all_ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        for i, (k, v) in enumerate(all_ranked):
            tag = "★ ACTIVE" if k in self._active_set else ("✗ HOLD" if k in held_bins else "")
            in_bl = " [BLACKLIST]" if k in self._blacklist else ""
            log.info(f"    {i+1:2d}. {k:12s} {v:5.2f} {tag}{in_bl}")
        log.info(f"  Active: {list(self._active_set)}")
        log.info(f"  Next rotation in: {self.ROTATION_INTERVAL // (24*3600)} days\n")

        # Close positions no longer in active set
        for cb_sym in list(self.portfolio.positions.keys()):
            bin_sym = CB_TO_BIN.get(cb_sym, cb_sym)
            if bin_sym not in self._active_set:
                price = self.prices.get(bin_sym, 0) or self.prices.get(cb_sym, 0)
                if price > 0:
                    log.info(f"🔴 ROTATION SELL {cb_sym} — no longer in active set")
                    self._sell(bin_sym, price, reason="ROTATION")

    # ── Order execution ───────────────────────────────────────────────────

    def _buy(self, bin_sym, price, qty, weight=1.0, rsi=50):
        cb = SYMBOL_MAP[bin_sym]
        alloc_usd = qty * price
        log.info(f"🟢 BUY  {cb:10s}  qty={qty:.6f}  @ ${price:.2f}  "
                 f"alloc=${alloc_usd:.2f}  RSI={rsi:.1f}  weight={weight:.0%}")
        try:
            r = self.ex.create_market_buy_order(cb, None, {"cost": alloc_usd})
            fills = r.get("trades", [])
            actual_qty = sum(f.get("amount", 0) for f in fills) if fills else alloc_usd / price * 0.999
            self.portfolio.cash -= alloc_usd
            self.portfolio.positions[cb] = self.portfolio.positions.get(cb, 0) + actual_qty
            self._entry_px[cb] = price
            self._partial_sells[cb] = {'tp1_done': False, 'tp2_done': False}
            self.portfolio.trades.append({
                "time": datetime.now().isoformat(), "pair": cb,
                "side": "BUY", "qty": actual_qty, "price": price,
                "rsi": round(rsi, 1), "weight": round(weight, 2),
                "alloc_usd": round(alloc_usd, 2)})
            log.info(f"   ✅ Filled qty={actual_qty:.6f}")
        except Exception as e:
            log.error(f"   BUY FAILED: {e}")

    def _sell(self, bin_sym, price, reason="SIGNAL", qty=None):
        cb = SYMBOL_MAP[bin_sym]
        pos_qty = self.portfolio.positions.get(cb, 0)
        if pos_qty <= 0: return
        actual_qty = qty if qty is not None else pos_qty
        entry = self._entry_px.get(cb, price)
        pnl = (price - entry) / entry * 100
        proceeds = actual_qty * price * 0.994
        fee = actual_qty * price * 0.006
        log.info(f"🔴 SELL [{reason}] {cb:10s}  qty={actual_qty:.6f}  @ ${price:.2f}  "
                 f"PnL={pnl:+.2f}%  fee=${fee:.2f}")
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
                "time": datetime.now().isoformat(), "pair": cb,
                "side": "SELL", "qty": actual_qty, "price": price,
                "entry": entry, "pnl": round(pnl, 2), "fee": round(fee, 2),
                "reason": reason})
            log.info(f"   Order OK: {r.get('id', '?')}")
        except Exception as e:
            log.error(f"   SELL FAILED: {e}")

    # ── Core tick ────────────────────────────────────────────────────────

    def tick(self):
        self._update_peak_and_check_drawdown()

        if self._is_paused():
            eq = self.portfolio.equity(self.prices)
            log.info(f"[PAUSED] Equity=${eq:.2f}  Until {datetime.fromtimestamp(self._pause_until).strftime('%H:%M UTC')}")
            self._save()
            return

        now = time.time()

        if self._last_score_at is None or (now - self._last_score_at) >= self.SCORE_INTERVAL:
            self._pool_scores = self._score_pool()
            self._last_score_at = now
            log.info(f"[POOL SCORE] Updated at {datetime.now().strftime('%H:%M UTC')}")

        if self._should_rotate():
            self._do_rotation()

        log.info(f"\n{'─'*50} {datetime.now().strftime('%H:%M UTC')}")

        eq = self.portfolio.equity(self.prices)

        for bin_sym, cb_sym in SYMBOL_MAP.items():
            raw = self.params.get(bin_sym, {})
            p = raw["params"] if "params" in raw else raw
            if not p: continue
            closes = self.fetch_close(bin_sym)
            if closes is None: continue

            price = closes[-1]
            self.prices[bin_sym] = price
            self.prices[cb_sym] = price
            ind = compute(closes, p)
            sig = signal_at(ind, len(closes)-1, p)
            rsi = ind["rsi"][-1]

            in_pos = cb_sym in self.portfolio.positions
            is_active = bin_sym in self._active_set

            if in_pos:
                if self._check_stop_loss(cb_sym, price):
                    self._blacklist[bin_sym] = now + self.BLACKLIST_DURATION
                    self._sell(bin_sym, price, reason="STOP_LOSS")
                    self._active_set.discard(bin_sym)
                    log.warning(f"   🚫 {bin_sym} blacklisted for 2 weeks")
                    continue

                tp = self._check_take_profit(cb_sym, price)
                if tp == "TP1":
                    pos_qty = self.portfolio.positions[cb_sym]
                    sell_qty = round(pos_qty * 0.50, 6)
                    self._sell(bin_sym, price, reason="TP1", qty=sell_qty)
                    log.info(f"   🎯 TP1 hit — sold 50% ({sell_qty:.4f}), keeping rest")
                    continue
                if tp == "TP2":
                    self._sell(bin_sym, price, reason="TP2")
                    self._active_set.discard(bin_sym)
                    log.info(f"   🎯 TP2 hit — sold remaining, slot freed")
                    continue

                if sig == "SELL":
                    self._sell(bin_sym, price, reason="SIGNAL")
                    self._active_set.discard(bin_sym)

            else:
                if not is_active:
                    continue

                if sig != "BUY":
                    continue

                rsi_buy = p.get("rsi_buy", 40)
                rsi_floor = 20
                if rsi >= rsi_buy:
                    continue
                weight = max(0.0, min(1.0, (rsi_buy - rsi) / (rsi_buy - rsi_floor)))
                if weight <= 0.1:
                    continue

                reserve = eq * self.RESERVE_CASH_PCT
                available = eq - reserve
                existing_pos_value = sum(
                    self.portfolio.positions[s] * self.prices.get(s, 0)
                    for s in self.portfolio.positions
                )
                investable = available - existing_pos_value
                max_alloc = investable / self.MAX_ACTIVE
                min_alloc = 20.0
                alloc = max(min_alloc, min(weight * available * 0.20, max_alloc))

                if self.portfolio.cash >= alloc:
                    qty = alloc / price
                    self._buy(bin_sym, price, qty, weight, rsi)

        eq = self.portfolio.equity(self.prices)
        ret = (eq / self.portfolio.initial - 1) * 100
        dd = (eq / self._peak_equity - 1) * 100 if self._peak_equity else 0
        active = list(self.portfolio.positions.keys())
        log.info(f"[PORT] Equity=${eq:.2f}  Ret={ret:+.2f}%  DD={dd:+.2f}%  "
                 f"Cash=${self.portfolio.cash:.2f}  Active={active}")
        self._save()

    def _save(self):
        state = {
            "timestamp": datetime.now().isoformat(),
            "portfolio": {
                "cash":    self.portfolio.cash,
                "equity":  self.portfolio.equity(self.prices),
                "return":  (self.portfolio.equity(self.prices)/self.portfolio.initial-1)*100,
                "positions": self.portfolio.positions,
                "trades":  self.portfolio.trades,
            },
            "_peak_equity":    self._peak_equity,
            "_pause_until":    self._pause_until,
            "_partial_sells": self._partial_sells,
            "_active_set":     list(self._active_set),
            "_last_rotation":  self._last_rotation,
            "_last_score_at":  self._last_score_at,
            "_blacklist":      {k: v for k, v in self._blacklist.items()},
            "_rotation_count": self._rotation_count,
        }
        with open("/tmp/trading_output/live_state.json", "w") as f:
            json.dump(state, f, indent=2)

    def run(self):
        self.authenticate()
        log.info(f"\n{'='*60}")
        log.info(f"  LIVE TRADER v3 — Observation Pool + Rotation")
        log.info(f"  Pool: 20 coins | Active: max {self.MAX_ACTIVE} | Reserve: {self.RESERVE_CASH_PCT:.0%} cash")
        log.info(f"  Rotation: every {self.ROTATION_INTERVAL // (24*3600)} days | Score update: hourly")
        log.info(f"  Stop-loss: {self.STOP_LOSS_PCT}% | Max DD: {self.MAX_DRAWDOWN}%")
        log.info(f"  TP1: +{self.TP1_PCT}% (sell 50%) | TP2: +{self.TP2_PCT}% (sell all)")
        log.info(f"{'='*60}\n")
        self.running.set()
        self.tick()
        schedule.every(self.check_secs).seconds.do(self.tick)
        schedule.every(1).hours.do(self._save)
        import time as t
        while self.running.is_set():
            schedule.run_pending()
            t.sleep(10)

    def stop(self):
        log.info("Stopping..."); self.running.clear(); self._save()


def main():
    p = "/tmp/trading_output/best_params_4h.json"
    if not os.path.exists(p):
        log.error(f"Run optimizer first: {p}"); return
    t = LiveTrader(p, capital_each=50.0)
    signal.signal(signal.SIGINT,  lambda *a: (t.stop(), sys.exit(0)))
    signal.signal(signal.SIGTERM, lambda *a: (t.stop(), sys.exit(0)))
    t.run()

if __name__ == "__main__":
    main()
