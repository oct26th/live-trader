"""Paper Arena — runs N strategies in parallel with shared market data.

Each strategy starts at $1000 paper cash, sees identical 1D OHLCV per tick, and
persists to its own paper_state_{name}.json. No real orders, ever.

Entry point:
    DRY_RUN=true python3 paper_arena.py
"""
from __future__ import annotations

import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone

import numpy as np
import schedule
from dotenv import load_dotenv

import requests

from config import (
    ARENA_INITIAL_CASH,
    ARENA_MA200_PERIOD,
    ARENA_STRATEGIES,
    DRY_RUN,
    LOG_DIR,
    LOG_FORMAT,
    SYMBOL_MAP,
)
from exchange import fetch_1d_ohlcv
from strategies import MarketData, Strategy, build_strategy

BTC_BIN = "BTC/USDT"
LOOKBACK_BUFFER = 70  # extra history on top of MA200 for any lookback variants


# ── Universe fetch (one round-trip per tick, shared by all strategies) ──────
def fetch_market_data(log: logging.Logger) -> MarketData | None:
    closes_map: dict[str, np.ndarray] = {}
    highs_map: dict[str, np.ndarray] = {}
    lows_map: dict[str, np.ndarray] = {}
    needed = ARENA_MA200_PERIOD + LOOKBACK_BUFFER  # = 270 bars

    for bin_sym in SYMBOL_MAP.keys():
        closes, highs, lows = fetch_1d_ohlcv(bin_sym, limit=needed)
        if closes is None or len(closes) < needed - 30:
            log.debug(f"skipping {bin_sym} — only {0 if closes is None else len(closes)} bars")
            continue
        closes_map[bin_sym] = closes
        highs_map[bin_sym] = highs
        lows_map[bin_sym] = lows
        time.sleep(0.1)

    btc = closes_map.get(BTC_BIN)
    if btc is None or len(btc) < ARENA_MA200_PERIOD:
        log.warning("BTC data insufficient for MA200; arena tick aborted.")
        return None

    btc_ma200 = float(np.mean(btc[-ARENA_MA200_PERIOD:]))
    btc_close = float(btc[-1])

    return MarketData(
        ts=datetime.now(timezone.utc),
        closes_map=closes_map,
        highs_map=highs_map,
        lows_map=lows_map,
        btc_close=btc_close,
        btc_ma200=btc_ma200,
    )


# ── Discord digest ──────────────────────────────────────────────────────────
class ArenaNotifier:
    """One Discord embed per arena tick, listing all strategies side-by-side."""

    def __init__(self) -> None:
        self.webhook_url = os.getenv("DISCORD_WEBHOOK_URL", "")
        self._last_at = None

    def _should_send(self, now: float) -> bool:
        if not self.webhook_url:
            return False
        if self._last_at is None:
            return True
        return (now - self._last_at) >= 3600  # hourly cap

    def send(self, strategies: list[Strategy], md: MarketData) -> None:
        now = time.time()
        if not self._should_send(now):
            return

        rows = []
        for s in strategies:
            eq = s.equity()
            ret = (eq / ARENA_INITIAL_CASH - 1) * 100
            rows.append({
                "name": f"{'🟢' if ret >= 0 else '🔴'} {s.name}",
                "value": (
                    f"`{s.label}`\n"
                    f"Equity ${eq:.2f} ({ret:+.2f}%) | DD {s._max_dd*100:+.1f}% | "
                    f"Pos {len(s.portfolio.positions)} | Trades {len(s.portfolio.trades)}"
                ),
                "inline": False,
            })

        market_field = {
            "name": "Market",
            "value": f"BTC ${md.btc_close:,.0f} | MA200 ${md.btc_ma200:,.0f} | "
                     f"filter {'ON' if md.btc_close > md.btc_ma200 else 'OFF'}",
            "inline": False,
        }

        embed = {
            "title": "🧪 Paper Arena — Strategy Bake-off",
            "color": 0x888888,
            "fields": [market_field] + rows,
            "footer": {"text": f"{md.ts.strftime('%Y-%m-%d %H:%M UTC')}"},
        }
        try:
            requests.post(self.webhook_url, json={"embeds": [embed]}, timeout=5)
            self._last_at = now
        except Exception as exc:
            logging.getLogger("arena").warning(f"Discord post failed: {exc}")


# ── Arena ────────────────────────────────────────────────────────────────────
class PaperArena:
    def __init__(self) -> None:
        self.log = logging.getLogger("arena")
        self.strategies: list[Strategy] = [build_strategy(s) for s in ARENA_STRATEGIES]
        for s in self.strategies:
            s.load()
        self.notifier = ArenaNotifier()
        self.running = True

    def tick(self) -> None:
        md = fetch_market_data(self.log)
        if md is None:
            return

        self.log.info(
            f"📊 Tick — BTC ${md.btc_close:,.0f} MA200 ${md.btc_ma200:,.0f} "
            f"filter {'ON' if md.btc_close > md.btc_ma200 else 'OFF'}"
        )

        for s in self.strategies:
            s.tick(md)

        self.notifier.send(self.strategies, md)
        self._log_summary()

    def _log_summary(self) -> None:
        rows = sorted(self.strategies, key=lambda s: s.equity(), reverse=True)
        self.log.info("──── Standings ────")
        for s in rows:
            eq = s.equity()
            ret = (eq / ARENA_INITIAL_CASH - 1) * 100
            self.log.info(
                f"  {s.name:>8s}  ${eq:>8.2f}  {ret:+6.2f}%  DD {s._max_dd*100:+5.1f}%  "
                f"pos={len(s.portfolio.positions)}  trades={len(s.portfolio.trades)}"
            )

    def stop(self, *_args) -> None:
        self.log.info("⏹ Arena stopping — flushing all strategy state.")
        for s in self.strategies:
            s.save()
        self.running = False


# ── Logging + entry ──────────────────────────────────────────────────────────
BANNER = r"""
  ╔══════════════════════════════════════════════════════════╗
  ║   🧪 PAPER ARENA — multi-strategy bake-off                ║
  ║   No real orders. Each strategy starts at $1000 paper.    ║
  ╚══════════════════════════════════════════════════════════╝
"""


def setup_logging() -> logging.Logger:
    os.makedirs(LOG_DIR, exist_ok=True)
    log = logging.getLogger("arena")
    log.setLevel(logging.INFO)
    log.handlers.clear()
    fh = logging.FileHandler(f"{LOG_DIR}/arena_{datetime.now().strftime('%Y%m%d')}.log")
    fh.setFormatter(logging.Formatter(LOG_FORMAT))
    log.addHandler(fh)
    sh = logging.StreamHandler()
    sh.setFormatter(logging.Formatter(LOG_FORMAT))
    log.addHandler(sh)
    # Also propagate strategy.* loggers
    strat_log = logging.getLogger("strategy")
    strat_log.setLevel(logging.INFO)
    strat_log.handlers = log.handlers[:]
    strat_log.propagate = False
    return log


def main() -> int:
    load_dotenv("/opt/data/trading_bot/.env")
    log = setup_logging()
    print(BANNER)

    if not DRY_RUN:
        log.error("DRY_RUN=false detected. Paper Arena refuses to run live.")
        return 1

    arena = PaperArena()
    log.info(f"Loaded {len(arena.strategies)} strategies:")
    for s in arena.strategies:
        log.info(f"  • {s.name} — {s.label}")

    def handler(signum, _frame):
        log.info(f"Signal {signum} received.")
        arena.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, handler)
    signal.signal(signal.SIGTERM, handler)

    arena.tick()
    schedule.every(1).hours.do(arena.tick)

    while arena.running:
        schedule.run_pending()
        time.sleep(10)

    return 0


if __name__ == "__main__":
    sys.exit(main())
