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

def _adx(high, low, close, period=14):
    n = len(close)
    if n < period * 3:
        return np.zeros(n)
    tr = np.zeros(n); dp = np.zeros(n); dm = np.zeros(n)
    for i in range(1, n):
        hl = high[i] - low[i]
        hc = abs(high[i] - close[i-1])
        lc = abs(low[i] - close[i-1])
        tr[i] = max(hl, hc, lc)
        up = high[i] - high[i-1]; dn = low[i-1] - low[i]
        dp[i] = up if up > dn and up > 0 else 0
        dm[i] = dn if dn > up and dn > 0 else 0
    tr_s = np.zeros(n); dp_s = np.zeros(n); dm_s = np.zeros(n)
    tr_s[period] = tr[1:period+1].sum()
    dp_s[period] = dp[1:period+1].sum()
    dm_s[period] = dm[1:period+1].sum()
    for i in range(period+1, n):
        tr_s[i] = tr_s[i-1] - tr_s[i-1]/period + tr[i]
        dp_s[i] = dp_s[i-1] - dp_s[i-1]/period + dp[i]
        dm_s[i] = dm_s[i-1] - dm_s[i-1]/period + dm[i]
    di_p = np.divide(dp_s, tr_s, out=np.zeros_like(tr_s), where=tr_s!=0) * 100
    di_m = np.divide(dm_s, tr_s, out=np.zeros_like(tr_s), where=tr_s!=0) * 100
    dx = np.divide(np.abs(di_p - di_m), di_p + di_m, out=np.zeros_like(di_p), where=(di_p + di_m)!=0) * 100
    adx = np.zeros(n); adx[period*2] = dx[period:period*2].mean()
    for i in range(period*2+1, n):
        adx[i] = (adx[i-1]*(period-1) + dx[i]) / period
    return adx

def compute(close, p, high=None, low=None):
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
    # ADX (optional — zeros if no high/low)
    adx_v = _adx(high, low, close, p["rsi_period"]) if high is not None and low is not None else np.zeros(len(close))
    return {"ma_f":ma_f,"ma_s":ma_s,"macd_h":macd_h,"mcross":mcx,"rsi":rsi_v,"ma_cross":mac,"adx":adx_v}

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
    STOP_LOSS_PCT    = -5.0
    MAX_DRAWDOWN      = -15.0
    PAUSE_DURATION    = 7 * 24 * 3600
    TP1_PCT           = 8.0
    TP2_PCT           = 15.0
    ADX_THRESHOLD     = 25
    MAX_ACTIVE        = 3
    RESERVE_CASH_PCT  = 0.20
    ROTATION_INTERVAL  = 14 * 24 * 3600
    SCORE_INTERVAL     = 3600
    BLACKLIST_DURATION = 14 * 24 * 3600
    TAKER_FEE         = 0.006   # 0.6% per side (Binance taker)
    MIN_NET_PROFIT    = 0.03    # 至少 3% 淨利（扣除來回手續費）

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
        self._regime_cfg    = {"allow_trend": True, "max_active": 3, "tp1": 0.08, "tp2": 0.15, "sl": -0.05}  # default fallback

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

    def fetch_close(self, bin_sym, timeframe='4h', limit=300):
        import ccxt
        ex = ccxt.binance({'enableRateLimit': True})
        data = ex.fetch_ohlcv(bin_sym, timeframe, limit=limit)
        if not data: return None
        return np.array([c[4] for c in data], dtype=float)

    def fetch_ohlcv(self, bin_sym, timeframe='4h', limit=300):
        """Return (closes, highs, lows) tuples."""
        import ccxt
        ex = ccxt.binance({'enableRateLimit': True})
        data = ex.fetch_ohlcv(bin_sym, timeframe, limit=limit)
        if not data: return None, None, None
        closes = np.array([c[4] for c in data], dtype=float)
        highs  = np.array([c[2] for c in data], dtype=float)
        lows   = np.array([c[3] for c in data], dtype=float)
        return closes, highs, lows

    def _btc_regime(self):
        """Detect BTC market regime via ADX on 4H candles. Returns dict with regime info."""
        try:
            closes, highs, lows = self.fetch_ohlcv('BTC/USDT', '4h', limit=100)
            if closes is None or len(closes) < 60:
                return {"regime": "UNKNOWN", "adx": 0, "trend": "N/A"}
            p = {"ma_fast": 20, "ma_slow": 50, "macd_fast": 12,
                 "macd_slow": 26, "macd_signal": 9, "rsi_period": 14}
            ind = compute(closes, p, highs, lows)
            adx = float(ind["adx"][-1])
            ma_f = ind["ma_f"][-1]; ma_s = ind["ma_s"][-1]
            mac_h = ind["macd_h"][-1]
            if adx < 25:
                regime = "RANGE"
            elif adx > 40:
                regime = "TRENDING"
            else:
                regime = "TRANSITION"
            trend = "bullish" if ma_f > ma_s and mac_h > 0 else "bearish" if ma_f < ma_s and mac_h < 0 else "neutral"
            return {"regime": regime, "adx": round(adx, 1), "trend": trend}
        except Exception as e:
            log.warning(f"   BTC regime detection failed: {e}")
            return {"regime": "UNKNOWN", "adx": 0, "trend": "N/A"}

    def _apply_regime(self, btc_regime):
        """Adjust strategy params based on BTC market regime."""
        r = btc_regime["regime"]
        if r == "TRENDING":
            return {
                "allow_trend": True,
                "max_active": 3,
                "tp1": 0.08,
                "tp2": 0.20,    # 放寬，讓利潤奔跑
                "sl": -0.05,
            }
        elif r == "RANGE":
            return {
                "allow_trend": False,  # 關閉 TREND 軌道
                "max_active": 2,
                "tp1": 0.05,
                "tp2": 0.10,    # 縮短，來回刷
                "sl": -0.03,    # 緊縮止損
            }
        else:  # TRANSITION
            return {
                "allow_trend": True,
                "max_active": 1,     # 只留 1 倉
                "tp1": 0.06,
                "tp2": 0.12,
                "sl": -0.04,
            }

    def _1h_signal(self, bin_sym):
        """Compute BUY/SELL/HOLD on 1H for auxiliary entry signal."""
        closes = self.fetch_close(bin_sym, '1h', limit=60)
        if closes is None:
            return "HOLD"
        p = self.params.get(bin_sym, {})
        pp = p["params"] if "params" in p else p
        ind = compute(closes, pp)
        return signal_at(ind, len(closes) - 1, pp)

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
        sl = self._regime_cfg["sl"] * 100  # regime-adjusted
        pnl_pct = (price - entry) / entry * 100
        if pnl_pct <= sl:
            log.info(f"🛑 STOP-LOSS {cb_sym} PnL={pnl_pct:.2f}% (SL={sl:.1f}%)")
            return True
        return False

    def _check_take_profit(self, cb_sym, price):
        entry = self._entry_px.get(cb_sym)
        if entry is None: return None
        tp1 = self._regime_cfg["tp1"] * 100  # regime-adjusted
        tp2 = self._regime_cfg["tp2"] * 100  # regime-adjusted
        pnl_pct = (price - entry) / entry * 100
        ps = self._partial_sells.setdefault(cb_sym, {'tp1_done': False, 'tp2_done': False})
        if not ps['tp1_done'] and pnl_pct >= tp1:
            ps['tp1_done'] = True
            return 'TP1'
        if ps['tp1_done'] and not ps['tp2_done'] and pnl_pct >= tp2:
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

        # ── BTC Market Regime Detection ────────────────────────────────────────
        btc_regime = self._btc_regime()
        self._regime_cfg = self._apply_regime(btc_regime)
        regime_cfg = self._regime_cfg
        regime_tag = f"[{btc_regime['regime']} BTC ADX={btc_regime['adx']} {btc_regime['trend']}]"
        log.info(f"  {regime_tag}  Trend={'✓ ON' if regime_cfg['allow_trend'] else '✗ OFF'}  MaxActive={regime_cfg['max_active']}  SL={regime_cfg['sl']*100:.0f}%  TP1={regime_cfg['tp1']*100:.0f}%  TP2={regime_cfg['tp2']*100:.0f}%")

        eq = self.portfolio.equity(self.prices)

        for bin_sym, cb_sym in SYMBOL_MAP.items():
            raw = self.params.get(bin_sym, {})
            p = raw["params"] if "params" in raw else raw
            if not p: continue
            closes, highs, lows = self.fetch_ohlcv(bin_sym)
            if closes is None: continue

            price = closes[-1]
            self.prices[bin_sym] = price
            self.prices[cb_sym] = price
            ind = compute(closes, p, highs, lows)
            sig = signal_at(ind, len(closes)-1, p)
            rsi = ind["rsi"][-1]
            adx = ind["adx"][-1]

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

                # ── Dual-track entry (regime-adjusted) ──────────────────────────────
                rsi_buy = p.get("rsi_buy", 38)
                # Track 1: Pullback — RSI oversold + MA bullish
                pullback_4h = (sig == "BUY" and rsi < rsi_buy)

                # Track 2: Trend-follow — controlled by BTC regime
                mac = ind["ma_cross"][-1]
                mac_h = ind["macd_h"][-1]
                trend_4h = (
                    regime_cfg["allow_trend"]
                    and mac == 1
                    and mac_h > 0
                    and 40 <= rsi <= 70
                    and adx > self.ADX_THRESHOLD
                )

                entry_ok = (pullback_4h or trend_4h)
                entry_reason = ""
                if pullback_4h: entry_reason = "PULLBACK_4H"
                elif trend_4h: entry_reason = "TREND_4H"

                if not entry_ok:
                    continue

                # ── Fee filter: skip if TP2 net of fees < minimum ──────────────
                tp2_raw = regime_cfg["tp2"]
                tp1_raw = regime_cfg["tp1"]
                # Estimate total round-trip fees as % of allocation
                # Buy(1×) + TP1 sell(0.5×tp1) + TP2 sell(0.5×tp2)
                total_fee_pct = self.TAKER_FEE * (1 + tp1_raw / 2 + tp2_raw / 2)
                net_tp2 = tp2_raw - total_fee_pct
                if net_tp2 < self.MIN_NET_PROFIT:
                    log.info(f"   ⛔ {bin_sym} skipped — TP2 net={net_tp2*100:.1f}% < min({self.MIN_NET_PROFIT*100:.0f}%)")
                    continue

                rsi_floor = 20
                rsi_use = rsi
                weight = max(0.0, min(1.0, (rsi_buy - rsi_use) / (rsi_buy - rsi_floor)))
                if weight <= 0.1:
                    continue

                reserve = eq * self.RESERVE_CASH_PCT
                available = eq - reserve
                existing_pos_value = sum(
                    self.portfolio.positions[s] * self.prices.get(s, 0)
                    for s in self.portfolio.positions
                )
                investable = available - existing_pos_value
                max_alloc = investable / regime_cfg["max_active"]
                min_alloc = 20.0
                alloc = max(min_alloc, min(weight * available * 0.20, max_alloc))

                if self.portfolio.cash >= alloc:
                    qty = alloc / price
                    log.info(f"   → ENTRY {entry_reason} {bin_sym} (RSI={rsi:.0f}, ADX={adx:.0f})")
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
