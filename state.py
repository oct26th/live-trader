"""Live Trader v4 — State Persistence (save & restore)"""
import os
import json
import time
from datetime import datetime
from config import STATE_PATH, BLACKLIST_DURATION


def save_state(trader):
    """Save full trader state to JSON."""
    state = {
        "timestamp": datetime.now().isoformat(),
        "portfolio": {
            "cash": trader.portfolio.cash,
            "equity": trader.portfolio.equity(trader.prices),
            "return": (
                (trader.portfolio.equity(trader.prices) / trader.portfolio.initial - 1)
                * 100
            ),
            "positions": trader.portfolio.positions,
            "trades": trader.portfolio.trades,
        },
        "_entry_px": trader._entry_px,
        "_peak_equity": trader._peak_equity,
        "_pause_until": trader._pause_until,
        "_partial_sells": trader._partial_sells,
        "_active_set": list(trader._active_set),
        "_last_rotation": trader._last_rotation,
        "_last_score_at": trader._last_score_at,
        "_blacklist": {k: v for k, v in trader._blacklist.items()},
        "_rotation_count": trader._rotation_count,
    }
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2)


def load_state(trader):
    """
    Restore trader state from JSON if it exists and is recent (< 24h).
    Only restore fields where positions still exist.
    """
    if not os.path.exists(STATE_PATH):
        return False

    try:
        with open(STATE_PATH) as f:
            state = json.load(f)
    except (json.JSONDecodeError, IOError):
        return False

    timestamp_str = state.get("timestamp")
    if not timestamp_str:
        return False

    try:
        timestamp = datetime.fromisoformat(timestamp_str)
        age_secs = (datetime.now() - timestamp).total_seconds()
        if age_secs > 24 * 3600:
            return False
    except Exception:
        return False

    now = time.time()

    # Restore fields only if they correspond to live positions
    live_positions = set(trader.portfolio.positions.keys())

    if state.get("_entry_px"):
        entry_px = state["_entry_px"]
        trader._entry_px = {k: v for k, v in entry_px.items() if k in live_positions}

    if state.get("_peak_equity"):
        trader._peak_equity = state["_peak_equity"]

    if state.get("_partial_sells"):
        partial_sells = state["_partial_sells"]
        trader._partial_sells = {k: v for k, v in partial_sells.items() if k in live_positions}

    if state.get("_active_set"):
        trader._active_set = set(state["_active_set"])

    if state.get("_blacklist"):
        blacklist = state["_blacklist"]
        trader._blacklist = {
            k: v for k, v in blacklist.items()
            if v > now
        }

    if state.get("_pause_until"):
        pause_until = state["_pause_until"]
        if pause_until and pause_until > now:
            trader._pause_until = pause_until

    if state.get("_last_rotation"):
        trader._last_rotation = state["_last_rotation"]

    if state.get("_last_score_at"):
        trader._last_score_at = state["_last_score_at"]

    if state.get("_rotation_count"):
        trader._rotation_count = state["_rotation_count"]

    trader.log.info(f"🔁 State restored from {timestamp.strftime('%Y-%m-%d %H:%M UTC')}")
    return True
