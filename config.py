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

# ── Paper Mode Arena — Multi-Strategy Bake-off ────────────────────────────────
# Paper arena runs N strategies in parallel from $1000 paper cash each, no real
# orders. Compare equity curves over 60-90 days to pick the winner before
# flipping any one of them to live. See CLAUDE.md §5 + §6.

DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"
ARENA_INITIAL_CASH = 1000.0
ARENA_MA200_PERIOD = 200          # BTC filter window (shared by momentum strategies)
ARENA_REBALANCE_WEEKDAY = 6       # Sunday UTC 00:00 (shared)
ARENA_PASSIVE_REBALANCE_WEEKDAY = 0  # Monday for passive monthly (first Monday-of-month logic)

# Strategy pre-registry (no fitting after launch — multiple-comparisons guardrail).
# Each entry instantiates a Strategy in strategies.py.
ARENA_STRATEGIES = [
    {
        "name": "A",
        "type": "momentum",
        "label": "Momentum top-5 30d (baseline)",
        "lookback": 30, "top_k": 5, "vol_cap": 0.06, "sl": -0.10,
    },
    {
        "name": "Aprime",
        "type": "momentum",
        "label": "Momentum top-5 60d (longer window)",
        "lookback": 60, "top_k": 5, "vol_cap": 0.06, "sl": -0.10,
    },
    {
        "name": "Adouble",
        "type": "momentum",
        "label": "Momentum top-3 30d (concentrated)",
        "lookback": 30, "top_k": 3, "vol_cap": 0.06, "sl": -0.10,
    },
    {
        "name": "B",
        "type": "trend",
        "label": "Trend follow BTC 1D (MA20/50 + ATR trail)",
        "ma_fast": 20, "ma_slow": 50, "atr_period": 14, "atr_mult": 2.0,
        "symbol": "BTC/USDT",
    },
    {
        "name": "D",
        "type": "passive",
        "label": "BTC buy & hold (benchmark)",
        "weights": {"BTC/USDT": 1.0}, "rebalance": None,
    },
    {
        "name": "E",
        "type": "passive",
        "label": "60/40 BTC/ETH monthly (passive benchmark)",
        "weights": {"BTC/USDT": 0.60, "ETH/USDT": 0.40},
        "rebalance": "monthly",
    },
]
ARENA_STATE_DIR = "/tmp/trading_output"  # paper_state_{name}.json

# ── Backwards-compat aliases for legacy trader_momentum.py / main_paper.py ────
# Kept so the original single-strategy paper mode still imports cleanly.
MOMENTUM_LOOKBACK_DAYS = 30
MOMENTUM_TOP_K = 5
MOMENTUM_SL = -0.10
MOMENTUM_VOL_CAP = 0.06
MOMENTUM_VOL_LOOKBACK_DAYS = 30
MOMENTUM_MA200_PERIOD = ARENA_MA200_PERIOD
MOMENTUM_REBALANCE_WEEKDAY = ARENA_REBALANCE_WEEKDAY
MOMENTUM_INITIAL_PAPER_CASH = ARENA_INITIAL_CASH

# ── Logging ───────────────────────────────────────────────────────────────────
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"

# ── Scheduling ────────────────────────────────────────────────────────────────
CHECK_INTERVAL_SECS = 300  # tick every 5 minutes
SAVE_INTERVAL_HOURS = 1
NOTIFY_INTERVAL_HOURS = 1
