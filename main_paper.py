"""Live Trader v4 — Paper Mode Entry Point (Variant A Momentum Rotation).

Runs MomentumTrader with DRY_RUN=true by default. No real Coinbase orders.
To flip to live (NOT YET ENABLED):
    export DRY_RUN=false
See CLAUDE.md §5 for shipping criteria.
"""
import logging
import os
import signal
import sys
import time
from datetime import datetime

import schedule
from dotenv import load_dotenv

from config import DRY_RUN, LOG_DIR, LOG_FORMAT
from trader_momentum import MomentumTrader

BANNER_PAPER = r"""
  __________________________________________________
  🧪  LIVE TRADER v4 — VARIANT A (PAPER MODE)
     Weekly rebalance · top-5 momentum · BTC MA200 filter
     NO REAL ORDERS. Validation only.
  __________________________________________________
"""

BANNER_LIVE = r"""
  __________________________________________________
  🔴  LIVE TRADER v4 — VARIANT A (LIVE MODE)
     Real Coinbase orders active.
  __________________________________________________
"""


def setup_logging() -> logging.Logger:
    os.makedirs(LOG_DIR, exist_ok=True)
    tag = "paper" if DRY_RUN else "live_momentum"
    log = logging.getLogger("momentum")
    log.setLevel(logging.INFO)
    log.handlers.clear()
    fh = logging.FileHandler(f"{LOG_DIR}/{tag}_{datetime.now().strftime('%Y%m%d')}.log")
    fh.setFormatter(logging.Formatter(LOG_FORMAT))
    log.addHandler(fh)
    sh = logging.StreamHandler()
    sh.setFormatter(logging.Formatter(LOG_FORMAT))
    log.addHandler(sh)
    return log


def main() -> int:
    load_dotenv("/opt/data/trading_bot/.env")
    log = setup_logging()

    print(BANNER_PAPER if DRY_RUN else BANNER_LIVE)
    log.info(f"Starting Variant A | DRY_RUN={DRY_RUN}")

    trader = MomentumTrader()
    trader.log = log
    trader.authenticate()

    def handler(signum, _frame):
        log.info(f"Signal {signum} received.")
        trader.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, handler)
    signal.signal(signal.SIGTERM, handler)

    # First tick immediately, then hourly (covers SL checks + Sunday rebalance)
    trader.tick()
    schedule.every(1).hours.do(trader.tick)
    schedule.every(6).hours.do(trader.save_state)

    while trader.running:
        schedule.run_pending()
        time.sleep(10)

    return 0


if __name__ == "__main__":
    sys.exit(main())
