# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Branch layout

Active development is on **`master`**, not `main`. `main` only contains the initial commit. `origin/HEAD` points at `main` so a fresh clone lands on the wrong branch — switch to `master` before reading code.

## Running

v4 is a modular package. No build step, no test suite:

```bash
pip install -r requirements.txt
python main.py
```

Paths the script expects (it does **not** create or validate them beyond one `os.path.exists` check):

- `/opt/data/trading_bot/.env` — loaded via `load_dotenv`; must define `COINBASE_API_KEY`, `COINBASE_API_SECRET`, optionally `DISCORD_WEBHOOK_URL`.
- `/tmp/trading_output/best_params_4h.json` — per-symbol indicator params from an upstream optimizer. `main()` exits with an error if missing. State is also written to `/tmp/trading_output/live_state.json`, so the directory must exist.
- `/tmp/trading_logs/` — auto-created; daily log file `live_YYYYMMDD.log`.

SIGINT/SIGTERM trigger `LiveTrader.stop()` → `_save()` flushes state before exit.

## README vs. code divergence

The README describes the *strategy design*, but its "技術棧" section does not match the code. When editing, trust the code:

- README says execution goes to **Binance**; code uses **`ccxt.coinbaseadvanced`** (market data only comes from Binance, see below).
- README says scheduling is **cron**; code uses the **`schedule`** library inside a long-running `while` loop.
- README mentions a **Discord webhook** for hourly reports — **there is no webhook code**. Only file + stdout logging exists.

The regime parameter table in the README (SL −3/−4/−5%, TP1 5/6/8%, TP2 10/12/15%, MaxActive 2/1/3 for RANGE/TRANSITION/TRENDING) accurately matches `_apply_regime()`.

## Module Map

**v4 Structure:**

- `config.py` — all constants, SYMBOL_MAP, REGIME_PARAMS, paths
- `indicators.py` — _ema, _rsi, _adx, compute, signal_at
- `portfolio.py` — Portfolio class (cash, positions, equity)
- `exchange.py` — unified Binance/Coinbase interface (fetch_ohlcv, to_cb/to_bin)
- `state.py` — save_state, load_state (with 24h TTL and position reconciliation)
- `notifier.py` — DiscordNotifier (hourly digest, silent fail if webhook not set)
- `trader.py` — LiveTrader class (350 lines, modular)
- `main.py` — entry point, logging, signal handlers
- `archive_v3.py` — original 668-line version (reference only)

All entry points: `python main.py` → load config → init trader → run event loop.

## Architecture

### Two-exchange split (namespace clarity via exchange.py)

Market data from Binance, orders to Coinbase Advanced. `exchange.py` centralizes all exchange logic:

- `fetch_ohlcv(bin_sym, timeframe, limit)` → (closes, highs, lows) from Binance
- `fetch_close(bin_sym)` → closes only
- `to_cb(bin_sym)` → convert "BTC/USDT" to "BTC-USD"
- `to_bin(cb_sym)` → convert "BTC-USD" to "BTC/USDT"
- `get_coinbase_exchange()` → authenticated Coinbase Advanced

trader.py imports only these four functions from exchange, never touches ccxt directly. `Portfolio.positions` is keyed by **cb_sym**. `_active_set`, `_blacklist`, `_pool_scores`, `params`, `SYMBOL_MAP` keys are all **bin_sym**. All conversions go through the exchange functions or SYMBOL_MAP.

### State Restoration (v4 addition)

On `authenticate()`, after loading live Coinbase balances, `load_state(self)` attempts to restore from `/tmp/trading_output/live_state.json`:
- Only if file exists and is < 24h old
- Only restores fields corresponding to live positions (avoids ghost records)
- Restores: `_entry_px`, `_peak_equity`, `_partial_sells`, `_active_set`, `_blacklist` (filtered by expiry), `_pause_until` (if future), rotation/score timestamps, rotation count
- Logs `🔁 State restored from {timestamp}` on success

**Critical**: Without this, every restart clears stop-loss levels, TP progress, blacklist, and peak equity — defeating risk management. v4 fixes this.

### BTC regime-switching

Every `tick()` calls `_btc_regime()` on BTC/USDT 4H: ADX classifies the market as `RANGE` (<25), `TRANSITION` (25–40), or `TRENDING` (>40). `_apply_regime()` looks up the regime in `config.REGIME_PARAMS` and returns the corresponding dict, stored in `self._regime_cfg` with four dials:

| field         | RANGE | TRANSITION | TRENDING |
|---------------|-------|------------|----------|
| `allow_trend` | False | True       | True     |
| `max_active`  | 2     | 1          | 3        |
| `tp1`         | 5%    | 6%         | 8%       |
| `tp2`         | 10%   | 12%        | 20%      |
| `sl`          | −3%   | −4%        | −5%      |

`_regime_cfg` is re-read by `_check_stop_loss` and `_check_take_profit` on every call, so the active risk levels float with the regime mid-position. Defaults (before `tick()` has run once) are the TRENDING values.

### Dual-track entry

Inside `tick()`, a non-held symbol must pass **one** of these on 4H data to buy:

1. **PULLBACK_4H** — `signal_at` returns `BUY` **and** `rsi < p["rsi_buy"]`. Always available.
2. **TREND_4H** — `regime_cfg["allow_trend"]` **and** MA fast > slow **and** `macd_h > 0` **and** `40 ≤ rsi ≤ 70` **and** `adx > ADX_THRESHOLD (25)`. Disabled when BTC is in RANGE.

### Fee filter

Before sizing an entry, estimates round-trip fees as `TAKER_FEE * (1 + tp1/2 + tp2/2)` and skips if `tp2 - fees < MIN_NET_PROFIT (3%)`. Uses the regime-adjusted TP levels, so RANGE coins (TP2=10%) are more easily filtered out than TRENDING ones (TP2=20%).

### Active-set rotation (fixed in v4)

20-coin pool in `SYMBOL_MAP`; only the top-N by `_score_pool()` can receive new buys.

- `_score_pool()` weights low RSI + bullish MA cross + positive MACD hist + 20-bar volatility; recomputed hourly (`SCORE_INTERVAL`).
- `_do_rotation()` every 14 days: refresh scores, pick top `self._regime_cfg["max_active"]` (3/1/2) non-blacklisted non-held as `_active_set`, force-sell any open position no longer in the set (reason `ROTATION`).
- **v4 fix**: rotation now uses `regime_cfg["max_active"]` instead of class constant `MAX_ACTIVE = 3`, so rotation size matches entry allocation size (RANGE=2, TRANSITION=1, TRENDING=3).

### Risk layers inside `tick()` (order matters)

1. **Drawdown pause** — `_update_peak_and_check_drawdown`. If equity drops `MAX_DRAWDOWN` (−15%) from peak, `_pause_until = now + 7 days`; `tick` returns early while paused.
2. **Stop-loss** — regime-adjusted `sl`. Triggering also adds the symbol to `_blacklist` for 14 days.
3. **Partial take-profit** — regime-adjusted `tp1` sells 50%; `tp2` closes the rest and frees the active slot. State in `_partial_sells[cb_sym] = {tp1_done, tp2_done}`.
4. **Signal exit** — `signal_at` returning `SELL`.
5. **Buy sizing** — only for `_active_set` symbols passing PULLBACK or TREND and the fee filter. Weight is a linear interp of RSI between `p["rsi_buy"]` (0%) and 20 (100%); allocation is `max(20, min(weight * equity * 0.20, (equity − 20% reserve − existing_pos_value) / regime_cfg["max_active"]))`.

### Scheduling

`run()` does one immediate `tick()`, then:
- `tick()` every `check_secs` (300s).
- `_save()` every hour (extra snapshot independent of tick cadence).
- Main loop polls `schedule.run_pending()` every 10s.

### Params file shape

`best_params_4h.json` per-symbol entries may wrap params under `"params"` or be inline — every read site does `raw["params"] if "params" in raw else raw`. Preserve that when touching any new path. Required keys: `ma_fast`, `ma_slow`, `macd_fast`, `macd_slow`, `macd_signal`, `rsi_period`, `rsi_buy`, `rsi_sell`.

`_btc_regime()` hard-codes its own param set (MA 20/50, MACD 12/26/9, RSI 14) and does **not** use the params file.

### Discord Integration (v4 addition)

`notifier.py` sends hourly status digests via Discord webhook if `DISCORD_WEBHOOK_URL` env var is set.
- Throttled to max 1 per hour (via `_last_notify_at`).
- Sends after every `tick()` completes.
- Silent fail if webhook URL not set or request times out.
- Includes: regime (ADX), portfolio (equity, return, DD), positions, active set, timestamp.

### Code Removal in v4

- `_1h_signal()` — never called, removed per Variant C findings (1H signals are noise). Available in archive_v3.py if needed.

---

## Outstanding Strategy Questions (To Resolve)

### 1. Regime Switching Frequency & Whipsaw Risk

**Current state:** ADX thresholds at 25 (RANGE/TRANSITION) and 40 (TRANSITION/TRENDING) are reasonable, but no mitigation for rapid oscillation.

**Problems:**
- If ADX bounces 24.5 ↔ 25.5 over several hours, you flip between RANGE and TRANSITION multiple times, each flip changing `max_active` (2 vs 1 coin) and entry rules, forcing unwanted rotations.
- ADX itself has ~1 hour lag (14-period) on 4H data, so regime detection is always one candle behind actual market state.

**TODO:**
- Add `_regime_history` tracking (last 10 regimes + timestamps).
- Measure: regime switch count per 24h. If >5 switches, consider adding a 1-hour "hold" zone (32–38 ADX) before switching.
- Consider regime detection on 1H or 2H timeframe instead of 4H for faster response.
- Log every regime switch with ADX value for post-hoc analysis.

### 2. PULLBACK Entry Fill Rate in RANGE Mode

**Current state:** PULLBACK entries require `rsi < 38` (oversold), only available in RANGE and TRANSITION modes.

**Problem:**
- In stable range-bound markets, RSI may oscillate around 45–55 and rarely touch 38. You may sit idle for hours waiting for a pullback that never comes.
- Meanwhile TREND entries are disabled in RANGE mode (per Variant C backtest results), leaving money on the table.

**TODO:**
- Log "PULLBACK entry opportunities missed" counter — how often is `rsi >= 38` when a PULLBACK signal fires?
- If fill rate < 20%, consider loosening the threshold to `rsi < 45` or allowing weak TREND entries even in RANGE (test separately).
- Alternative: Add a 1H "intraday trend" entry that fires regardless of regime, gated by ADX > 25 on 1H chart.

### 3. Realized Fees vs. Filter Minimum

**Current state:** Fee filter uses `MIN_NET_PROFIT = 3%` (conservative) and estimates round-trip fees. Actual realized fees are not tracked per trade.

**Problem:**
- Coinbase taker fee is ~0.6%, so round-trip is ~1.2%. TP2 10% (RANGE) - 1.2% = 8.8% net — well above 3%, so the filter is safe but opaque.
- If you scale into a position (multiple buys) or exit TP1 early (rotation pressure), fees compound, but the filter doesn't see this.
- No visibility into actual P&L net of slippage + fees; you don't know if the strategy is really making 3% per trade or just meeting the filter.

**TODO:**
- Add per-trade fee tracking: log entry fee, partial TP1 fee, final TP2 fee, and realized net P&L per symbol.
- Compare logged realized P&L against backtest expectations (Variant C: −0.4% return, −13.3% max DD).
- If actual fees exceed modeled fees by >50 bps, adjust MIN_NET_PROFIT or fee estimation formula.
- Store fee data in `_partial_sells[cb_sym]` alongside `{tp1_done, tp2_done}` for audit trail.

### 4. Pool & Rotation Churn

**Current state:** 20-coin pool, top-N active rotation every 14 days via rolling rank.

**TODO:**
- Log active set every hour: track set membership, churn rate (how often ranking changes), and repeated winners.
- If active set stability < 90% (members stay >13 of 14 days), rotation is too aggressive → reduce `SCORE_INTERVAL` or smooth the ranking.
- If same 3 coins dominate for weeks, check for regime exploitation (e.g., "always BTC/USDT in TRENDING") → may not generalize.
- Monitor: after restart and state restoration, verify that restored `_entry_px` and `_partial_sells` align with live Coinbase positions (no ghost records).

### 5. Variant C is Structurally Unprofitable — Migrate to Momentum Rotation (Variant A)

**Discovered 2026-04-24:**

Three independent backtests showed Variant C (PULLBACK + TREND dual-track, regime-adjusted) is
structurally broken on 4H timeframe across ALL market conditions:
- Lilith's 570d dataset (2024-10 → 2026-04, bull): 1 trade, -4.75%
- Local 720d backtest (2022-2023, bear+range+rebound): 123 trades, avg -5.23% per 180d segment
- PULLBACK signal (RSI < 38 on 4H) fires **0 times across 8 historical segments** — structural dead code
- TREND signal ~50% win rate, but SL triggers faster than TP → net negative in all segments

**Root cause:** Mean-reversion strategy on 4H is a dead zone — high-freq (5m-1H) works for stat arb,
1D+ works for long-term reversion, but 4H has neither signal density nor mean-reverting behavior.

**Migration target:** `backtest_momentum.py` Variant A (`VOL=6%, SL=-10%`):
- 3-year backtest: +61.9% return (vs Variant C's -5.2%)
- Rules: weekly rebalance, top-5 by 20-30d log return, BTC 1D MA200 kill switch, per-coin -10% SL, vol cap 6%
- Max DD still -72.5% — **unacceptable for live deployment**

**Decision:** Implement Variant A in `trader.py` but ship with `DRY_RUN=True` (paper mode).
Run 30-60 days to validate signal reproducibility and timing alignment vs backtest. Only flip to
`DRY_RUN=False` when BTC breaks MA200 down and stabilizes above again (next cycle bottom) —
momentum strategies perform best at cycle-start, worst at cycle-peak.

**Paper Mode Implementation Plan:**

| Component | Change |
|-----------|--------|
| `config.py` | Add `DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"` flag |
| `config.py` | Add momentum constants: `MOMENTUM_LOOKBACK=30`, `MOMENTUM_TOP_K=5`, `MOMENTUM_SL=-0.10`, `VOL_CAP=0.06` |
| `exchange.py` | New `get_binance_exchange()` for 1D OHLCV (strategy needs daily data, not 4H) |
| `trader.py` | Replace `tick()` logic with weekly rebalance loop. Keep Portfolio/State/Notifier intact. |
| `trader.py` | Branch in `_buy()` / `_sell()`: DRY_RUN=True → log-only, update Portfolio in memory; DRY_RUN=False → Coinbase order |
| `state.py` | Add paper-mode state file: `/tmp/trading_output/paper_state.json` (separate from live_state.json) |
| `main.py` | Print `🧪 PAPER MODE` banner on startup if DRY_RUN |
| `notifier.py` | Discord digest tags `[PAPER]` prefix in DRY_RUN mode |

**Shipping criteria (paper → live):**
1. Paper mode runs ≥ 30 days without crashes
2. Paper signals reproduce within ±5% of backtest for the same period (validate alignment)
3. BTC 1D closes below MA200, then re-crosses above and holds ≥ 2 weeks
4. Lilith's Hermes sweep confirms best-params haven't drifted on fresh 2025-2026 data

---

## Decision Log

- ✅ **ADX thresholds (25/40):** Validated by Variant C backtest. N/A under Variant A (no regime logic).
- ✅ **RANGE disables TREND entries:** Validated historically. N/A under Variant A.
- ✅ **Variant A momentum rotation (2026-04-24):** Chosen over Variant C after 3 backtests confirmed V.C structural failure. Params: `VOL=6%, SL=-10%, top-5, lookback-30d, BTC MA200 filter`.
- ✅ **Paper mode before live (2026-04-24):** $512 real balance too small to absorb historical -72% DD. Paper trade until next BTC cycle bottom confirms entry timing.
- ❌ **BTC 7-day return kill switch:** Sweep showed it over-triggers and destroys 2023 gains. Rejected.
- ⏳ **Regime hold zone:** N/A under Variant A (no regimes).
- ⏳ **PULLBACK fill rate:** Moot — PULLBACK track removed in migration.
- ⏳ **Fee tracking:** Still needed for paper-mode validation vs backtest expectations.
