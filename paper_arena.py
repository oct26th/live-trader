"""Paper Arena — runs N strategies in parallel with shared market data.

Each strategy starts at $1000 paper cash, sees identical 1D OHLCV per tick, and
persists to its own paper_state_{name}.json. No real orders, ever.

Entry point:
    DRY_RUN=true python3 paper_arena.py
"""
from __future__ import annotations

import json
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
    ARENA_STATE_DIR,
    ARENA_STRATEGIES,
    DRY_RUN,
    LOG_DIR,
    LOG_FORMAT,
    SYMBOL_MAP,
)
from exchange import fetch_1d_ohlcv
from strategies import MarketData, PassiveStrategy, Strategy, build_strategy

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


# ── Event detector ───────────────────────────────────────────────────────────
class ArenaEventDetector:
    """Detect noteworthy state transitions and fire dedicated Discord alerts.

    Currently tracks:
    - FIRST_ENTRY: an active strategy (Momentum/Trend) opens its first-ever
      paper position. Filtered to active strategies only — passive D/E
      benchmarks deploy on tick 1 which is uninteresting.
    - FILTER_FLIP_ON: BTC 1D close crosses above MA200 from below (across ticks).
    - BENCHMARK_BEATEN: an active strategy's equity first surpasses D
      (BTC buy & hold). Gated on FIRST_ENTRY having already fired so early
      fee-drag wins (A at $1000 cash > D at $994 from entry fee) don't
      count. This is the single most meaningful event in the arena —
      alpha exists.

    Persisted to arena_events.json so the same event isn't re-announced after
    arena restart.
    """

    EVENTS_PATH = f"{ARENA_STATE_DIR}/arena_events.json"

    def __init__(self) -> None:
        self.log = logging.getLogger("arena.events")
        self._announced: set[tuple[str, str]] = set()  # (event_type, key)
        self._last_filter_state: bool | None = None
        self._load()

    def _load(self) -> None:
        if not os.path.exists(self.EVENTS_PATH):
            return
        try:
            with open(self.EVENTS_PATH) as f:
                data = json.load(f)
            self._announced = {tuple(e) for e in data.get("announced", [])}
            self._last_filter_state = data.get("last_filter_state")
        except (IOError, json.JSONDecodeError):
            pass

    def _save(self) -> None:
        os.makedirs(ARENA_STATE_DIR, exist_ok=True)
        with open(self.EVENTS_PATH, "w") as f:
            json.dump({
                "announced": [list(e) for e in sorted(self._announced)],
                "last_filter_state": self._last_filter_state,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }, f, indent=2)

    def detect(self, strategies: list[Strategy], md: MarketData) -> list[dict]:
        events: list[dict] = []
        # Snapshot at start of detect — used by BENCHMARK_BEATEN gate so it can't
        # see FIRST_ENTRY events that fire in this same tick. This prevents the
        # fragile "A just entered (cash + new fee-laden position) vs D's
        # already-fee-dragged equity" comparison from scoring an instant alpha.
        announced_at_tick_start = set(self._announced)

        # ── FIRST_ENTRY (active strategies only) ────────────────────────────
        for s in strategies:
            if isinstance(s, PassiveStrategy):
                continue  # passive deploys on tick 1, not noteworthy
            key = ("FIRST_ENTRY", s.name)
            if key in self._announced:
                continue
            if len(s.portfolio.positions) > 0:
                events.append({
                    "type": "FIRST_ENTRY",
                    "strategy": s.name,
                    "label": s.label,
                    "positions": dict(s.portfolio.positions),
                    "entry_px": dict(s._entry_px),
                    "btc_close": md.btc_close,
                    "btc_ma200": md.btc_ma200,
                })
                self._announced.add(key)

        # ── FILTER_FLIP_ON (BTC crosses above MA200 across ticks) ───────────
        current = md.btc_close > md.btc_ma200 if md.btc_close and md.btc_ma200 else False
        if self._last_filter_state is False and current is True:
            events.append({
                "type": "FILTER_FLIP_ON",
                "btc_close": md.btc_close,
                "btc_ma200": md.btc_ma200,
            })
        self._last_filter_state = current

        # ── BENCHMARK_BEATEN (active strategy first surpasses D's equity) ────
        d_strategy = next((s for s in strategies if s.name == "D"), None)
        if d_strategy is not None:
            d_equity = d_strategy.equity()
            for s in strategies:
                if isinstance(s, PassiveStrategy):
                    continue  # E vs D comparison not meaningful
                key = ("BENCHMARK_BEATEN", s.name)
                if key in self._announced:
                    continue
                # Gate: FIRST_ENTRY must have fired in a PREVIOUS tick (not this one)
                # — uses snapshot taken before FIRST_ENTRY processing in this tick.
                if ("FIRST_ENTRY", s.name) not in announced_at_tick_start:
                    continue
                if s.equity() > d_equity:
                    events.append({
                        "type": "BENCHMARK_BEATEN",
                        "strategy": s.name,
                        "label": s.label,
                        "strategy_equity": s.equity(),
                        "d_equity": d_equity,
                        "lead_pct": (s.equity() / d_equity - 1) * 100 if d_equity > 0 else 0,
                    })
                    self._announced.add(key)

        if events:
            self._save()
        return events


def post_event_alert(event: dict, log: logging.Logger) -> None:
    """Send a dedicated Discord embed for a single event (independent of digest)."""
    webhook = os.getenv("DISCORD_WEBHOOK_URL", "")
    if not webhook:
        return

    if event["type"] == "FIRST_ENTRY":
        positions_str = ", ".join(
            f"`{cb}` @ ${px:.4f}"
            for cb, px in event["entry_px"].items()
        ) or "none"
        embed = {
            "title": f"🚀 [{event['strategy']}] FIRST ENTRY",
            "description": event["label"],
            "color": 0xFFAA00,
            "fields": [
                {"name": "Positions", "value": positions_str, "inline": False},
                {
                    "name": "Market",
                    "value": f"BTC ${event['btc_close']:,.0f} | MA200 ${event['btc_ma200']:,.0f}",
                    "inline": False,
                },
            ],
            "footer": {"text": f"Paper Arena event @ {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"},
        }
    elif event["type"] == "FILTER_FLIP_ON":
        embed = {
            "title": "🟢 BTC FILTER FLIPPED ON",
            "description": "BTC 1D close just crossed above MA200. Momentum strategies (A/A'/A'') will pick top-K on next Sunday rebalance.",
            "color": 0x00FF00,
            "fields": [{
                "name": "Market",
                "value": f"BTC ${event['btc_close']:,.0f} > MA200 ${event['btc_ma200']:,.0f}",
                "inline": False,
            }],
            "footer": {"text": f"Paper Arena event @ {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"},
        }
    elif event["type"] == "BENCHMARK_BEATEN":
        embed = {
            "title": f"🏆 [{event['strategy']}] BEAT BTC HODL",
            "description": (
                f"{event['label']} — first time equity surpassed passive BTC benchmark. "
                f"Alpha hypothesis confirmed."
            ),
            "color": 0x00FF88,
            "fields": [{
                "name": "Equity",
                "value": (
                    f"`{event['strategy']}`: ${event['strategy_equity']:.2f}\n"
                    f"`D` (HODL): ${event['d_equity']:.2f}\n"
                    f"Lead: **+{event['lead_pct']:.2f}%**"
                ),
                "inline": False,
            }],
            "footer": {"text": f"Paper Arena event @ {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"},
        }
    else:
        return

    try:
        requests.post(webhook, json={"embeds": [embed]}, timeout=5)
        log.info(f"🚀 Event alert sent: {event['type']}")
    except Exception as exc:
        log.warning(f"Event alert failed: {exc}")


# ── Arena ────────────────────────────────────────────────────────────────────
class PaperArena:
    def __init__(self) -> None:
        self.log = logging.getLogger("arena")
        self.strategies: list[Strategy] = [build_strategy(s) for s in ARENA_STRATEGIES]
        for s in self.strategies:
            s.load()
        self.notifier = ArenaNotifier()
        self.events = ArenaEventDetector()
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
            try:
                s.tick(md)
            except Exception as exc:
                # Isolate per-strategy failures so a bug in one strategy doesn't
                # take down the whole arena. Errors are logged and the next
                # strategy continues. Strategy.tick() already wraps on_tick(),
                # so this is mainly a backstop for save() / equity() failures.
                self.log.error(
                    f"Strategy {s.name} crashed in tick(): {exc}", exc_info=True
                )

        # Event detection runs after strategy ticks so position changes are visible
        for ev in self.events.detect(self.strategies, md):
            self.log.info(f"🚀 EVENT: {ev['type']} {ev.get('strategy', '')}")
            post_event_alert(ev, self.log)

        self.notifier.send(self.strategies, md)
        self._log_summary()
        self._run_verification()

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

    def _run_verification(self) -> None:
        try:
            from arena_verify import run_verification
            result = run_verification()
            if result["error_count"] > 0:
                self.log.error(f"🚨 Verification ERRORS: {result['error_count']} — check arena_verification.json")
            elif result["warning_count"] > 0:
                self.log.warning(f"⚠️  Verification warnings: {result['warning_count']}")
        except Exception as exc:
            self.log.debug(f"Verification skipped: {exc}")

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
