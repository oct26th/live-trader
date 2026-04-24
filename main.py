"""Live Trader v4 — Entry Point"""
import os
import sys
import signal
import logging
from datetime import datetime
from dotenv import load_dotenv

from config import PARAMS_PATH, LOG_DIR, LOG_FORMAT
from trader import LiveTrader


def setup_logging():
    """Initialize logger."""
    os.makedirs(LOG_DIR, exist_ok=True)
    log = logging.getLogger("live_trader")
    log.setLevel(logging.INFO)
    fh = logging.FileHandler(
        f"{LOG_DIR}/live_{datetime.now().strftime('%Y%m%d')}.log"
    )
    fh.setFormatter(logging.Formatter(LOG_FORMAT))
    log.addHandler(fh)
    log.addHandler(logging.StreamHandler())
    return log


def main():
    """Start the trading bot."""
    os.environ["TQDM_DISABLE"] = "1"
    load_dotenv("/opt/data/trading_bot/.env")

    log = setup_logging()

    if not os.path.exists(PARAMS_PATH):
        log.error(f"Run optimizer first: {PARAMS_PATH}")
        return

    trader = LiveTrader(PARAMS_PATH, capital_each=50.0)
    trader.log = log

    def signal_handler(signum, frame):
        trader.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    trader.run()


if __name__ == "__main__":
    main()
