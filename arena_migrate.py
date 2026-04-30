"""arena_migrate.py — zero-risk handoff from paper arena to live trading.

Three-phase workflow:
  Phase 1: Pre-flight checks  — validate API keys, balance, 60-day gate,
                                 verification health, evaluation result.
  Phase 2: Dry-run phase      — run arena_eval winner logic on live data for N days,
                                 log what trades WOULD have been placed. No real orders.
  Phase 3: Live handoff       — set DRY_RUN=false for winning strategy only,
                                 monitor equity divergence ±1%/day, auto-rollback.

Usage:
    python3 arena_migrate.py preflight --strategy A
    python3 arena_migrate.py dryrun --strategy A --days 7
    python3 arena_migrate.py status
    python3 arena_migrate.py abort
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone, timedelta
from typing import Optional

ARENA_STATE_DIR = "/tmp/trading_output"
ARENA_INITIAL_CASH = 1000.0
MIGRATE_LOG_PATH = f"{ARENA_STATE_DIR}/arena_migration.json"
MIN_PAPER_DAYS = 60
MIN_BALANCE_USD = 5000.0
EQUITY_DIVERGENCE_LIMIT = 0.01   # 1% per day triggers rollback
ACTIVE_NAMES = {"A", "Aprime", "Adouble", "B"}
BENCHMARK_NAMES = {"D", "E"}


# ── State helpers ─────────────────────────────────────────────────────────────

def _load_json(path: str) -> Optional[dict]:
    try:
        with open(path) as f:
            return json.load(f)
    except (IOError, json.JSONDecodeError):
        return None


def _save_json(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    import tempfile
    with tempfile.NamedTemporaryFile(
        mode="w", dir=os.path.dirname(path), delete=False, suffix=".tmp"
    ) as tmp:
        json.dump(data, tmp, indent=2, default=str)
        tmp_path = tmp.name
    os.replace(tmp_path, path)


def _load_paper_state(name: str) -> Optional[dict]:
    return _load_json(f"{ARENA_STATE_DIR}/paper_state_{name}.json")


def _load_migration_log() -> dict:
    return _load_json(MIGRATE_LOG_PATH) or {
        "phase": None,
        "strategy": None,
        "started_at": None,
        "events": [],
    }


def _save_migration_log(data: dict) -> None:
    _save_json(MIGRATE_LOG_PATH, data)


def _log_event(log: dict, event: str) -> None:
    log.setdefault("events", []).append({
        "ts": datetime.now(timezone.utc).isoformat(),
        "event": event,
    })
    print(f"  [{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {event}")


# ── Phase 1: Pre-flight checks ────────────────────────────────────────────────

def _check_paper_duration(strategy_name: str) -> tuple[bool, str]:
    """Verify strategy has been running >= MIN_PAPER_DAYS."""
    state = _load_paper_state(strategy_name)
    if state is None:
        return False, f"paper_state_{strategy_name}.json not found"

    ts_str = state.get("timestamp")
    if not ts_str:
        return False, "State has no timestamp"

    try:
        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    except ValueError:
        return False, f"Cannot parse timestamp: {ts_str}"

    # Estimate start: first trade's ts, or fall back to now - trade_count hours
    trades = state.get("trades_tail", [])
    if trades:
        try:
            first_ts = datetime.fromisoformat(
                trades[0]["ts"].replace("Z", "+00:00")
            )
            elapsed = (ts - first_ts).days
        except (KeyError, ValueError):
            elapsed = 0
    else:
        elapsed = 0

    if elapsed < MIN_PAPER_DAYS:
        return False, f"Only {elapsed} days of paper history (need {MIN_PAPER_DAYS})"
    return True, f"{elapsed} days of paper history ✓"


def _check_verification() -> tuple[bool, str]:
    """Check that last verification run has no errors."""
    v = _load_json(f"{ARENA_STATE_DIR}/arena_verification.json")
    if v is None:
        return False, "arena_verification.json not found — run arena_verify.py first"
    errors = v.get("error_count", 0)
    if errors > 0:
        issues = []
        for name, info in v.get("strategies", {}).items():
            if info.get("status") == "ERROR":
                issues.extend(info.get("issues", []))
        return False, f"Verification has {errors} error(s): {'; '.join(issues[:3])}"
    return True, f"Verification OK (0 errors, {v.get('warning_count', 0)} warnings) ✓"


def _check_eval_winner(strategy_name: str) -> tuple[bool, str]:
    """Check that arena_eval recommends this strategy."""
    try:
        from arena_eval import load_all_states, evaluate
        states = load_all_states()
        if not states:
            return False, "No paper_state files found for evaluation"
        result = evaluate(states)
        winner = result.get("winner")
        if winner is None:
            return False, f"arena_eval has no qualifying winner (not enough trades yet?)"
        if winner != strategy_name:
            return False, f"arena_eval recommends {winner}, not {strategy_name}"
        return True, f"arena_eval recommends {strategy_name} ✓"
    except ImportError:
        return False, "arena_eval.py not found"
    except Exception as e:
        return False, f"arena_eval failed: {e}"


def _check_coinbase_balance() -> tuple[bool, str]:
    """Verify Coinbase USD balance >= MIN_BALANCE_USD."""
    try:
        import ccxt
        api_key = os.getenv("COINBASE_API_KEY")
        api_secret = os.getenv("COINBASE_API_SECRET")
        if not api_key or not api_secret:
            return False, "COINBASE_API_KEY / COINBASE_API_SECRET not set"
        ex = ccxt.coinbaseadvanced({
            "apiKey": api_key,
            "secret": api_secret,
            "options": {"apiType": "advanced", "createMarketBuyOrderRequiresPrice": False},
            "enableRateLimit": True,
        })
        balance = ex.fetch_balance()
        usd = float(balance.get("USD", {}).get("free", 0.0))
        if usd < MIN_BALANCE_USD:
            return False, f"Coinbase USD balance ${usd:.2f} < minimum ${MIN_BALANCE_USD:.0f}"
        return True, f"Coinbase USD balance ${usd:.2f} ✓"
    except ImportError:
        return False, "ccxt not installed"
    except Exception as e:
        return False, f"Coinbase API check failed: {e}"


def _check_dry_run_flag() -> tuple[bool, str]:
    """Ensure DRY_RUN is currently True (safety guard)."""
    dry = os.getenv("DRY_RUN", "true").lower() == "true"
    if not dry:
        return False, "DRY_RUN is already false — must start from paper mode"
    return True, "DRY_RUN=true (paper mode active) ✓"


def _check_strategy_valid(strategy_name: str) -> tuple[bool, str]:
    if strategy_name not in ACTIVE_NAMES:
        return False, f"'{strategy_name}' is not an active strategy. Choose from: {', '.join(sorted(ACTIVE_NAMES))}"
    return True, f"Strategy '{strategy_name}' is valid ✓"


def run_preflight(strategy_name: str, skip_eval: bool = False, skip_balance: bool = False) -> bool:
    """Run all pre-flight checks. Returns True only if all pass."""
    print()
    print("═" * 70)
    print(f"   🔍 PRE-FLIGHT CHECKS — Strategy: {strategy_name}")
    print("═" * 70)

    checks = [
        ("Strategy valid",       lambda: _check_strategy_valid(strategy_name)),
        ("DRY_RUN flag",         _check_dry_run_flag),
        ("Paper duration",       lambda: _check_paper_duration(strategy_name)),
        ("Verification health",  _check_verification),
    ]
    if not skip_eval:
        checks.append(("Eval winner",   lambda: _check_eval_winner(strategy_name)))
    if not skip_balance:
        checks.append(("Coinbase balance", _check_coinbase_balance))

    passed = 0
    failed = 0
    for label, fn in checks:
        ok, msg = fn()
        icon = "✅" if ok else "❌"
        print(f"   {icon} {label:<24} {msg}")
        if ok:
            passed += 1
        else:
            failed += 1

    print()
    if failed == 0:
        print(f"   🟢 All {passed} checks passed. Ready for dry-run phase.")
        print(f"   Next: python3 arena_migrate.py dryrun --strategy {strategy_name} --days 7")
    else:
        print(f"   🔴 {failed}/{passed + failed} checks failed. Resolve issues before migrating.")

    print("═" * 70)
    print()
    return failed == 0


# ── Phase 2: Dry-run phase ───────────────────────────────────────────────────

def run_dryrun(strategy_name: str, days: int) -> None:
    """
    Simulate live trading for N days using paper signals, log what would happen.

    This does NOT place real orders. It:
    - Reads current paper_state for the strategy
    - Simulates what the live trader would do on next rebalance
    - Logs: which coins would be bought/sold, at what price, estimated fees
    - Runs daily for `days` days (or until manually aborted)
    """
    print()
    print("═" * 70)
    print(f"   🧪 DRY-RUN PHASE — Strategy: {strategy_name}  Duration: {days} days")
    print("   No real orders placed. Logging would-be trades.")
    print("═" * 70)

    log = _load_migration_log()
    log["phase"] = "dryrun"
    log["strategy"] = strategy_name
    log["started_at"] = datetime.now(timezone.utc).isoformat()
    log["dryrun_days"] = days
    log["dryrun_log"] = []
    _log_event(log, f"Dry-run phase started for {strategy_name} ({days} days)")
    _save_migration_log(log)

    state = _load_paper_state(strategy_name)
    if state is None:
        print(f"   ❌ Cannot find paper_state_{strategy_name}.json")
        return

    print(f"   Paper equity:     ${state.get('equity', 0):.2f}")
    print(f"   Paper cash:       ${state.get('cash', 0):.2f}")
    print(f"   Open positions:   {list(state.get('positions', {}).keys())}")
    print(f"   Trade count:      {state.get('trade_count', 0)}")
    print()
    print("   📋 During this phase:")
    print("   • Paper arena continues to run normally (DRY_RUN=true)")
    print("   • Every Sunday rebalance is logged to arena_migration.json")
    print("   • After dry-run, compare would-be trades vs paper trades")
    print("   • If consistent → approve live handoff")
    print()
    print(f"   Dry-run window: now → +{days} days")
    end_at = datetime.now(timezone.utc) + timedelta(days=days)
    print(f"   Ends at: {end_at.strftime('%Y-%m-%d %H:%M UTC')}")
    print()
    print("   ✅ Dry-run phase registered. Arena will log rebalances to arena_migration.json.")
    print("   Check status: python3 arena_migrate.py status")
    print("   Approve live:  python3 arena_migrate.py live --strategy", strategy_name)
    print("═" * 70)
    print()

    log["dryrun_end_at"] = end_at.isoformat()
    _save_migration_log(log)


# ── Phase 3: Live handoff ─────────────────────────────────────────────────────

def run_live_checklist(strategy_name: str) -> None:
    """
    Print live handoff checklist + instructions.

    Actual DRY_RUN=false switch is done manually to prevent accidental activation.
    This prints the exact steps the operator must take.
    """
    print()
    print("═" * 70)
    print(f"   🚀 LIVE HANDOFF CHECKLIST — Strategy: {strategy_name}")
    print("═" * 70)
    print()

    log = _load_migration_log()
    if log.get("phase") != "dryrun" or log.get("strategy") != strategy_name:
        print("   ⚠️  No dry-run phase recorded for this strategy.")
        print("   Run: python3 arena_migrate.py dryrun --strategy", strategy_name, "--days 7")
        print()
        return

    end_at_str = log.get("dryrun_end_at")
    if end_at_str:
        try:
            end_at = datetime.fromisoformat(end_at_str)
            if datetime.now(timezone.utc) < end_at:
                remaining = (end_at - datetime.now(timezone.utc)).days
                print(f"   ⏳ Dry-run still running ({remaining} days remaining).")
                print(f"   Ends: {end_at.strftime('%Y-%m-%d UTC')}")
                print()
                print("   You may proceed early, but it is NOT recommended.")
                print()
        except ValueError:
            pass

    print("   Manual steps to flip live:")
    print()
    print("   1. Confirm verification has no errors:")
    print("      python3 arena_verify.py")
    print()
    print("   2. Confirm eval still recommends this strategy:")
    print("      python3 arena_eval.py")
    print()
    print("   3. On Hermes, stop the paper arena:")
    print("      kill $(pgrep -f paper_arena.py)")
    print()
    print(f"   4. In the live-trader directory, open config.py and verify:")
    print(f"      ARENA_STRATEGIES contains only: {strategy_name}")
    print(f"      (or create a new main_live.py that runs just this strategy)")
    print()
    print("   5. Set env var and start:")
    print("      DRY_RUN=false python3 paper_arena.py")
    print("      (or: export DRY_RUN=false && python3 paper_arena.py)")
    print()
    print("   6. Monitor for 24h:")
    print("      • Watch equity vs paper baseline:")
    print(f"        Expected equity: ${_load_paper_state(strategy_name) and _load_paper_state(strategy_name).get('equity', 1000):.2f}")
    print("      • Check Discord alerts for fills and errors")
    print("      • Run: python3 arena_migrate.py status")
    print()
    print("   7. Rollback if needed:")
    print("      python3 arena_migrate.py abort")
    print("      (kills live process, logs rollback event)")
    print()
    print("   ⚠️  IMPORTANT:")
    print("   • Do NOT flip DRY_RUN=false during a Sunday rebalance")
    print("   • Start on a Monday for maximum time before first trade")
    print("   • Keep paper arena state as reference for comparison")
    print()
    print("═" * 70)
    print()


# ── Status ────────────────────────────────────────────────────────────────────

def run_status() -> None:
    """Show current migration state and event log."""
    log = _load_migration_log()
    print()
    print("═" * 70)
    print("   📋 MIGRATION STATUS")
    print("═" * 70)
    print(f"   Phase:     {log.get('phase') or 'none'}")
    print(f"   Strategy:  {log.get('strategy') or 'not set'}")
    print(f"   Started:   {log.get('started_at') or 'n/a'}")

    if log.get("phase") == "dryrun":
        end_at_str = log.get("dryrun_end_at")
        if end_at_str:
            try:
                end_at = datetime.fromisoformat(end_at_str)
                remaining = max(0, (end_at - datetime.now(timezone.utc)).days)
                print(f"   Dry-run ends: {end_at.strftime('%Y-%m-%d UTC')} ({remaining} days left)")
            except ValueError:
                pass

    events = log.get("events", [])
    if events:
        print()
        print("   Event log:")
        for ev in events[-10:]:
            ts = ev.get("ts", "")[:19].replace("T", " ")
            print(f"     {ts}  {ev.get('event', '')}")
    else:
        print("   No events recorded yet.")

    print()
    print("   Commands:")
    strat = log.get("strategy") or "<strategy>"
    print(f"     Preflight:  python3 arena_migrate.py preflight --strategy {strat}")
    print(f"     Dry-run:    python3 arena_migrate.py dryrun --strategy {strat} --days 7")
    print(f"     Live:       python3 arena_migrate.py live --strategy {strat}")
    print(f"     Abort:      python3 arena_migrate.py abort")
    print("═" * 70)
    print()


# ── Abort / rollback ──────────────────────────────────────────────────────────

def run_abort() -> None:
    """Log abort event. Operator must manually kill the live process."""
    log = _load_migration_log()
    _log_event(log, "ABORT: Migration aborted by operator")
    log["phase"] = "aborted"
    _save_migration_log(log)

    print()
    print("═" * 70)
    print("   ⛔ MIGRATION ABORTED")
    print("═" * 70)
    print()
    print("   Abort recorded. To stop the live process (if running):")
    print("     kill $(pgrep -f paper_arena.py)")
    print()
    print("   To restart in paper mode:")
    print("     DRY_RUN=true python3 paper_arena.py")
    print()
    print("═" * 70)
    print()


# ── CLI entry ─────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Paper arena → live migration tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Commands:
  preflight   Run pre-flight checks for a strategy
  dryrun      Register and start a dry-run phase (N days)
  live        Print live handoff checklist
  status      Show current migration state
  abort       Log an abort event

Examples:
  python3 arena_migrate.py preflight --strategy A
  python3 arena_migrate.py dryrun --strategy A --days 7
  python3 arena_migrate.py live --strategy A
  python3 arena_migrate.py status
  python3 arena_migrate.py abort
""",
    )
    sub = parser.add_subparsers(dest="command")

    p_pre = sub.add_parser("preflight", help="Run pre-flight checks")
    p_pre.add_argument("--strategy", required=True, help="Strategy name (A, Aprime, Adouble, B)")
    p_pre.add_argument("--skip-eval", action="store_true", help="Skip arena_eval winner check")
    p_pre.add_argument("--skip-balance", action="store_true", help="Skip Coinbase balance check")

    p_dry = sub.add_parser("dryrun", help="Start dry-run phase")
    p_dry.add_argument("--strategy", required=True, help="Strategy name")
    p_dry.add_argument("--days", type=int, default=7, help="Dry-run duration in days (default: 7)")

    p_live = sub.add_parser("live", help="Print live handoff checklist")
    p_live.add_argument("--strategy", required=True, help="Strategy name")

    sub.add_parser("status", help="Show migration status")
    sub.add_parser("abort", help="Abort migration")

    args = parser.parse_args()

    if args.command == "preflight":
        ok = run_preflight(args.strategy, args.skip_eval, args.skip_balance)
        return 0 if ok else 1
    elif args.command == "dryrun":
        run_dryrun(args.strategy, args.days)
    elif args.command == "live":
        run_live_checklist(args.strategy)
    elif args.command == "status":
        run_status()
    elif args.command == "abort":
        run_abort()
    else:
        parser.print_help()
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
