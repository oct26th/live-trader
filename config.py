"""Live Trader v4 — Configuration and Constants"""
import os

# ── 20-Coin Observation Pool ──────────────────────────────────────────────────
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

# ── Risk & Portfolio Constants ────────────────────────────────────────────────
STOP_LOSS_PCT = -5.0
MAX_DRAWDOWN = -15.0
PAUSE_DURATION = 7 * 24 * 3600
TP1_PCT = 8.0
TP2_PCT = 15.0
ADX_THRESHOLD = 25
MAX_ACTIVE = 3
RESERVE_CASH_PCT = 0.20
ROTATION_INTERVAL = 14 * 24 * 3600
SCORE_INTERVAL = 3600
BLACKLIST_DURATION = 14 * 24 * 3600
TAKER_FEE = 0.006
MIN_NET_PROFIT = 0.03

# ── BTC Regime Parameters ─────────────────────────────────────────────────────
REGIME_PARAMS = {
    "TRENDING": {
        "allow_trend": True,
        "max_active": 3,
        "tp1": 0.08,
        "tp2": 0.20,
        "sl": -0.05,
    },
    "TRANSITION": {
        "allow_trend": True,
        "max_active": 1,
        "tp1": 0.06,
        "tp2": 0.12,
        "sl": -0.04,
    },
    "RANGE": {
        "allow_trend": False,
        "max_active": 2,
        "tp1": 0.05,
        "tp2": 0.10,
        "sl": -0.03,
    },
    "UNKNOWN": {
        "allow_trend": True,
        "max_active": 3,
        "tp1": 0.08,
        "tp2": 0.20,
        "sl": -0.05,
    },
}

# ── Paths ─────────────────────────────────────────────────────────────────────
PARAMS_PATH = "/tmp/trading_output/best_params_4h.json"
STATE_PATH = "/tmp/trading_output/live_state.json"
PAPER_STATE_PATH = "/tmp/trading_output/paper_state.json"
LOG_DIR = "/tmp/trading_logs"

# ── Variant A (Momentum Rotation) — Paper Mode ────────────────────────────────
# Controls whether trader_momentum.py executes real orders or logs-only.
# Default: true (paper). Flip to "false" only after passing shipping criteria
# documented in CLAUDE.md §5.
DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"

# Best params from 32-combo sweep (2026-04-24). See CLAUDE.md §5 Decision Log.
MOMENTUM_LOOKBACK_DAYS = 30       # 20d log-return window (was 20; 30 filters alt noise)
MOMENTUM_TOP_K = 5                # hold top-5 equally weighted
MOMENTUM_SL = -0.10               # per-coin stop-loss (−10%)
MOMENTUM_VOL_CAP = 0.06           # exclude coins with 30d daily σ > 6% (sweep sweet spot)
MOMENTUM_VOL_LOOKBACK_DAYS = 30
MOMENTUM_MA200_PERIOD = 200       # BTC 1D MA200 market filter
MOMENTUM_REBALANCE_WEEKDAY = 6    # Sunday UTC 00:00
MOMENTUM_INITIAL_PAPER_CASH = 1000.0  # paper starts at $1000 so 1 paper $ ~= 2 real $

# ── Logging ───────────────────────────────────────────────────────────────────
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"

# ── Scheduling ────────────────────────────────────────────────────────────────
CHECK_INTERVAL_SECS = 300  # tick every 5 minutes
SAVE_INTERVAL_HOURS = 1
NOTIFY_INTERVAL_HOURS = 1
