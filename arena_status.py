"""Read-only snapshot of paper arena state — `python3 arena_status.py` to see it.

Reads /tmp/trading_output/paper_state_*.json and arena_events.json.
Prints standings table + open positions + event log.

Run on Hermes (where the arena is running). For local convenience:

    npx zeabur@latest service exec --id 69e883934bdf5ec1ab0a471c -- \\
        python3 /opt/data/trading_bot/live-trader/arena_status.py
"""
import glob
import json
import os
import sys
from datetime import datetime, timezone

ARENA_STATE_DIR = "/tmp/trading_output"
INITIAL_CASH = 1000.0


def load(path: str) -> dict | None:
    try:
        with open(path) as f:
            return json.load(f)
    except (IOError, json.JSONDecodeError):
        return None


def fmt_pct(v: float, width: int = 7) -> str:
    s = f"{v:+.2f}%"
    return s.rjust(width)


def main() -> int:
    files = sorted(glob.glob(f"{ARENA_STATE_DIR}/paper_state_*.json"))
    if not files:
        print(f"❌ No paper_state files in {ARENA_STATE_DIR}")
        print("   Is arena running? Check: ps -ef | grep paper_arena")
        return 1

    states = [s for s in (load(p) for p in files) if s is not None]
    if not states:
        print("❌ All state files unreadable.")
        return 1

    # ── Header ────────────────────────────────────────────────────────────
    print()
    print("═" * 80)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(f"   📊  PAPER ARENA STATUS  —  {now}")
    print("═" * 80)

    # ── Filter state ──────────────────────────────────────────────────────
    events_path = f"{ARENA_STATE_DIR}/arena_events.json"
    events_data = load(events_path) or {}
    last_filter = events_data.get("last_filter_state")
    filter_str = "🟢 ON" if last_filter else ("🔴 OFF" if last_filter is False else "⚪ unknown")
    print(f"   BTC filter state (vs MA200):   {filter_str}")
    print()

    # ── Standings ─────────────────────────────────────────────────────────
    print(f"   {'Strategy':<10} {'Equity':>10} {'Return':>8} {'MaxDD':>7} {'Pos':>4} {'Trades':>7}  Last tick (UTC)")
    print(f"   {'─' * 10} {'─' * 10} {'─' * 8} {'─' * 7} {'─' * 4} {'─' * 7}  {'─' * 19}")
    states_sorted = sorted(states, key=lambda s: s.get("equity", 0.0), reverse=True)
    for s in states_sorted:
        eq = float(s.get("equity", 0.0))
        ret = (eq / INITIAL_CASH - 1) * 100
        dd = float(s.get("max_dd_pct", 0.0))
        n_pos = len(s.get("positions", {}))
        n_trades = int(s.get("trade_count", 0))
        ts = (s.get("timestamp") or "")[:19].replace("T", " ")
        marker = "🥇" if s == states_sorted[0] else ("🥈" if s is states_sorted[1] else ("🥉" if s is states_sorted[2] else "  "))
        print(
            f"   {marker}{s['name']:<8} ${eq:>8.2f}  {fmt_pct(ret)}  {fmt_pct(dd, 6)}  "
            f"{n_pos:>4}  {n_trades:>7}  {ts}"
        )
    print()

    # ── Open positions (only show strategies with any) ────────────────────
    have_pos = [s for s in states if s.get("positions", {})]
    if have_pos:
        print("   📦 OPEN POSITIONS")
        for s in have_pos:
            print(f"      [{s['name']}]  {s.get('label', '')}")
            entry_px = s.get("entry_px", {})
            for cb_sym, qty in s.get("positions", {}).items():
                ep = float(entry_px.get(cb_sym, 0))
                print(f"        {cb_sym:<10} qty={float(qty):.6f}  entry=${ep:.4f}")
        print()
    else:
        print("   📦 No open positions across any strategy.\n")

    # ── Events log ────────────────────────────────────────────────────────
    announced = events_data.get("announced", [])
    print("   🔔 EVENT LOG")
    if announced:
        print(f"      {len(announced)} event(s) fired:")
        for entry in announced:
            t, name = (entry[0], entry[1]) if len(entry) >= 2 else (entry[0], "")
            print(f"        • {t}  {name}")
    else:
        print("      No events fired yet — waiting for filter to flip ON")
        print("      or any active strategy to take its first position.")
    print()

    # ── Recent trades (across all strategies, last 10) ────────────────────
    all_trades = []
    for s in states:
        for t in s.get("trades_tail", []):
            t["__strategy"] = s["name"]
            all_trades.append(t)
    if all_trades:
        all_trades.sort(key=lambda t: t.get("ts", ""), reverse=True)
        print("   📜 RECENT TRADES (last 10 across all strategies)")
        for t in all_trades[:10]:
            ts = (t.get("ts") or "")[:19].replace("T", " ")
            sym = t.get("sym", "")
            pnl = float(t.get("pnl_pct", 0)) * 100
            reason = t.get("reason", "")
            entry = float(t.get("entry_px", 0))
            exit_px = float(t.get("exit_px", 0))
            print(
                f"      [{t['__strategy']}] {ts}  {sym:<10} "
                f"${entry:.2f}→${exit_px:.2f}  {pnl:+.2f}%  ({reason})"
            )
        print()

    print("═" * 80)
    return 0


if __name__ == "__main__":
    sys.exit(main())
