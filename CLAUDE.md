# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Branch layout

Active development is on **`master`**, not `main`. `main` only contains the initial commit. `origin/HEAD` points at `main` so a fresh clone lands on the wrong branch — switch to `master` before reading code.

## Running

Single-file bot, no build step, no test suite:

```bash
python live_trader_v3.py
```

Paths the script expects (it does **not** create or validate them beyond one `os.path.exists` check):

- `/opt/data/trading_bot/.env` — loaded via `load_dotenv`; must define `COINBASE_API_KEY`, `COINBASE_API_SECRET`.
- `/tmp/trading_output/best_params_4h.json` — per-symbol indicator params from an upstream optimizer. `main()` exits with an error if missing. State is also written back to `/tmp/trading_output/live_state.json`, so the directory must exist.
- `/tmp/trading_logs/` — auto-created; daily log file `live_YYYYMMDD.log`.

SIGINT/SIGTERM trigger `LiveTrader.stop()` → `_save()` flushes state before exit.

## README vs. code divergence

The README describes the *strategy design*, but its "技術棧" section does not match the code. When editing, trust the code:

- README says execution goes to **Binance**; code uses **`ccxt.coinbaseadvanced`** (market data only comes from Binance, see below).
- README says scheduling is **cron**; code uses the **`schedule`** library inside a long-running `while` loop.
- README mentions a **Discord webhook** for hourly reports — **there is no webhook code**. Only file + stdout logging exists.

The regime parameter table in the README (SL −3/−4/−5%, TP1 5/6/8%, TP2 10/12/15%, MaxActive 2/1/3 for RANGE/TRANSITION/TRENDING) accurately matches `_apply_regime()`.

## Architecture

### Two-exchange split (namespace gotcha)

Market data from Binance, orders to Coinbase Advanced. Every symbol has two names kept in sync via `SYMBOL_MAP` (e.g. `BTC/USDT` → `BTC-USD`) and reverse `CB_TO_BIN`:

- `fetch_close()` / `fetch_ohlcv()` use `ccxt.binance` (no auth) for 4H and 1H OHLCV.
- `get_exchange()`, `_buy`, `_sell` use `ccxt.coinbaseadvanced` with API keys from env.
- `Portfolio.positions` is keyed by **cb_sym**. `_active_set`, `_blacklist`, `_pool_scores`, `params`, `SYMBOL_MAP` keys are all **bin_sym**. Anything touching both must translate explicitly.

### BTC regime-switching (the v3 core idea)

Every `tick()` starts by running `_btc_regime()` on BTC/USDT 4H: ADX classifies the market as `RANGE` (<25), `TRANSITION` (25–40), or `TRENDING` (>40). `_apply_regime()` returns a dict stored on `self._regime_cfg` with four dials:

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

### Active-set rotation (separate from entry logic)

20-coin pool in `SYMBOL_MAP`; only the top-N by `_score_pool()` can receive new buys.

- `_score_pool()` weights low RSI + bullish MA cross + positive MACD hist + 20-bar volatility; recomputed hourly (`SCORE_INTERVAL`).
- `_do_rotation()` every 14 days: refresh scores, pick top `MAX_ACTIVE` (3) non-blacklisted non-held as `_active_set`, force-sell any open position no longer in the set (reason `ROTATION`).
- **Gotcha**: rotation uses the class constant `MAX_ACTIVE = 3` while entry allocation divides by `regime_cfg["max_active"]` (which can be 1 or 2). The "slot count" in RANGE/TRANSITION regimes is therefore implicit in sizing, not in rotation size.

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

### Persisted state, restart behaviour

`_save()` writes a single JSON with portfolio, peak, pause/rotation/score timestamps, blacklist, partial-sell progress, trade log. There is **no load path** — `authenticate()` rebuilds `positions` from live Coinbase balances, but these fields reset on every restart:

- `_entry_px` — lost → stop-loss and TP pct calculations use the next observed buy price.
- `_peak_equity` — resets to current USD cash.
- `_partial_sells`, `_blacklist`, `_active_set`, `_pause_until`.

This is load-bearing for any reasoning about behaviour after a redeploy or crash.

### Unused code to be aware of

`_1h_signal()` is defined but not called anywhere in `tick()` — entry logic is purely 4H. Don't assume it's wired in.
