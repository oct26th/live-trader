"""arena_verify.py — reads all paper_state files and validates correctness.

Verification rules:
  1. Basic integrity  — cash >= 0, equity > 0, entry_px/positions key match,
                        max_dd_pct <= 0, timestamp parseable ISO8601.
  2. Rebalance check  — MomentumStrategy (A, Aprime, Adouble): last_rebalance_date
                        must fall on a Sunday; stale filter_on without positions warns.
  3. State consistency — positions / entry_px symmetry, no negative quantities.
  4. Cross-strategy   — D and E should have positions once initialized;
                        any strategy below 50% of ARENA_INITIAL_CASH warns.

Run standalone:
    python3 arena_verify.py

Import from paper_arena.py:
    from arena_verify import run_verification
    result = run_verification()
    if result["error_count"] > 0:
        self.log.error(f"Verification ERRORS: {result['error_count']}")
"""
from __future__ import annotations

import glob
import json
import os
import sys
from datetime import datetime, timezone
from typing import Any

ARENA_STATE_DIR = "/tmp/trading_output"
ARENA_INITIAL_CASH = 1000.0
VERIFICATION_PATH = f"{ARENA_STATE_DIR}/arena_verification.json"

# Strategy names that use MomentumStrategy — subject to rebalance-day check.
MOMENTUM_STRATEGY_NAMES = {"A", "Aprime", "Adouble"}

# Passive benchmark names — expected to have positions once initialized.
PASSIVE_STRATEGY_NAMES = {"D", "E"}

# Rebalance weekday (6 = Sunday UTC, matches config.ARENA_REBALANCE_WEEKDAY).
REBALANCE_WEEKDAY = 6

# Warn if equity drops below this fraction of initial cash.
EQUITY_WARNING_THRESHOLD = 0.5

# Maximum days since last rebalance before we flag a potential skip.
REBALANCE_STALE_DAYS = 7


# ── File loading ──────────────────────────────────────────────────────────────

def load_state(path: str) -> dict[str, Any] | None:
    """Load and JSON-parse a single state file. Returns None on any error."""
    try:
        with open(path) as f:
            return json.load(f)
    except (IOError, OSError, json.JSONDecodeError):
        return None


def _discover_state_files() -> dict[str, str]:
    """Return {name: path} for all paper_state_*.json files in ARENA_STATE_DIR."""
    pattern = os.path.join(ARENA_STATE_DIR, "paper_state_*.json")
    found: dict[str, str] = {}
    for path in sorted(glob.glob(pattern)):
        basename = os.path.basename(path)  # paper_state_NAME.json
        name = basename[len("paper_state_"):-len(".json")]
        if name:
            found[name] = path
    return found


# ── Per-strategy verification ─────────────────────────────────────────────────

def _check_basic_integrity(s: dict[str, Any]) -> list[str]:
    """Rule 1 — basic field integrity."""
    issues: list[str] = []

    cash = s.get("cash")
    equity = s.get("equity")
    positions: dict = s.get("positions") or {}
    entry_px: dict = s.get("entry_px") or {}
    trade_count = s.get("trade_count")
    trades_tail = s.get("trades_tail") or []
    max_dd_pct = s.get("max_dd_pct")
    timestamp = s.get("timestamp")

    # cash >= 0
    if cash is None:
        issues.append("Missing field: cash")
    elif not isinstance(cash, (int, float)):
        issues.append(f"Cash is not numeric: {cash!r}")
    elif cash < 0:
        issues.append(f"Cash negative: {cash:.2f}")

    # equity > 0
    if equity is None:
        issues.append("Missing field: equity")
    elif not isinstance(equity, (int, float)):
        issues.append(f"Equity is not numeric: {equity!r}")
    elif equity <= 0:
        issues.append(f"Equity non-positive: {equity:.2f}")

    # equity >= cash  (equity = cash + holdings value, so equity < cash implies negative holdings value)
    if (
        isinstance(cash, (int, float))
        and isinstance(equity, (int, float))
        and cash >= 0
        and equity > 0
        and equity < cash - 0.01  # small epsilon for float arithmetic
    ):
        issues.append(
            f"Equity ({equity:.2f}) less than cash ({cash:.2f}) — "
            "implies negative holdings value"
        )

    # entry_px keys must match positions keys exactly
    pos_keys = set(positions.keys())
    entry_keys = set(entry_px.keys())
    if pos_keys != entry_keys:
        extra_entry = entry_keys - pos_keys
        extra_pos = pos_keys - entry_keys
        if extra_entry:
            issues.append(f"Ghost entry prices (no open positions): {sorted(extra_entry)}")
        if extra_pos:
            issues.append(f"Positions without entry prices: {sorted(extra_pos)}")

    # trade_count >= len(trades_tail) always — trades_tail is a truncated tail
    if trade_count is not None and isinstance(trade_count, int):
        tail_len = len(trades_tail)
        if trade_count < tail_len:
            issues.append(
                f"trade_count ({trade_count}) < len(trades_tail) ({tail_len}) — "
                "impossible: trade_count should be total, tail is truncated"
            )
    elif trade_count is None:
        issues.append("Missing field: trade_count")

    # max_dd_pct <= 0
    if max_dd_pct is None:
        issues.append("Missing field: max_dd_pct")
    elif not isinstance(max_dd_pct, (int, float)):
        issues.append(f"max_dd_pct is not numeric: {max_dd_pct!r}")
    elif max_dd_pct > 0:
        issues.append(
            f"max_dd_pct positive ({max_dd_pct:.4f}) — drawdown must be <= 0"
        )

    # timestamp parseable ISO8601
    if timestamp is None:
        issues.append("Missing field: timestamp")
    elif isinstance(timestamp, str):
        try:
            datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        except ValueError:
            issues.append(f"Timestamp not parseable as ISO8601: {timestamp!r}")
    else:
        issues.append(f"Timestamp is not a string: {timestamp!r}")

    return issues


def _check_rebalance(s: dict[str, Any]) -> list[str]:
    """Rule 2 — rebalance-day and stale-filter checks (Momentum strategies only)."""
    issues: list[str] = []

    last_rb = s.get("last_rebalance_date")
    positions: dict = s.get("positions") or {}
    extra: dict = s.get("extra") or {}
    filter_on: bool | None = extra.get("filter_on")
    timestamp_raw = s.get("timestamp", "")

    # last_rebalance_date must be a Sunday
    if last_rb is not None:
        try:
            rb_date = datetime.strptime(last_rb, "%Y-%m-%d").date()
            if rb_date.weekday() != REBALANCE_WEEKDAY:
                day_name = rb_date.strftime("%A")
                issues.append(
                    f"last_rebalance_date {last_rb} is a {day_name}, "
                    f"expected Sunday (weekday 6)"
                )
        except ValueError:
            issues.append(
                f"last_rebalance_date not parseable as YYYY-MM-DD: {last_rb!r}"
            )

    # Stale filter: filter_on=True, no positions, rebalance overdue
    if filter_on is True and len(positions) == 0 and last_rb is not None:
        try:
            rb_date = datetime.strptime(last_rb, "%Y-%m-%d").date()
            # Derive "now" from state timestamp if available, else use real UTC now
            if timestamp_raw and isinstance(timestamp_raw, str):
                try:
                    state_ts = datetime.fromisoformat(
                        timestamp_raw.replace("Z", "+00:00")
                    ).date()
                except ValueError:
                    state_ts = datetime.now(timezone.utc).date()
            else:
                state_ts = datetime.now(timezone.utc).date()
            days_since = (state_ts - rb_date).days
            if days_since > REBALANCE_STALE_DAYS:
                issues.append(
                    f"Rebalance may have been skipped: filter_on=True, no positions, "
                    f"last_rebalance_date={last_rb} ({days_since}d ago)"
                )
        except (ValueError, TypeError):
            pass  # date parse already flagged above

    return issues


def _check_state_consistency(s: dict[str, Any]) -> list[str]:
    """Rule 3 — state consistency checks."""
    issues: list[str] = []

    positions: dict = s.get("positions") or {}
    entry_px: dict = s.get("entry_px") or {}

    # Negative position quantities
    for sym, qty in positions.items():
        if isinstance(qty, (int, float)) and qty < 0:
            issues.append(f"Negative position quantity for {sym}: {qty}")

    # Negative or zero entry prices
    for sym, px in entry_px.items():
        if isinstance(px, (int, float)) and px <= 0:
            issues.append(f"Non-positive entry price for {sym}: {px}")

    return issues


def verify_strategy(s: dict[str, Any]) -> list[str]:
    """Run all per-strategy verification rules.

    Returns a list of issue strings, empty if the state is clean.
    Issues prefixed with 'ERROR:' are errors; others are warnings.
    (Callers use classify_issues() to separate them.)
    """
    name = s.get("name", "?")
    issues: list[str] = []

    issues.extend(_check_basic_integrity(s))
    issues.extend(_check_state_consistency(s))

    if name in MOMENTUM_STRATEGY_NAMES:
        issues.extend(_check_rebalance(s))

    return issues


# ── Severity classification ───────────────────────────────────────────────────

# Issues whose text starts with any of these prefixes are warnings, not errors.
_WARNING_PREFIXES = (
    "Rebalance may have been skipped",
    "Ghost entry prices",
    "Positions without entry prices",
)


def _is_warning(issue: str) -> bool:
    return any(issue.startswith(p) for p in _WARNING_PREFIXES)


def _classify(issues: list[str]) -> tuple[list[str], list[str]]:
    """Split issues into (errors, warnings)."""
    errors = [i for i in issues if not _is_warning(i)]
    warnings = [i for i in issues if _is_warning(i)]
    return errors, warnings


# ── Cross-strategy checks ─────────────────────────────────────────────────────

def _cross_strategy_checks(
    all_states: dict[str, dict[str, Any]]
) -> dict[str, list[str]]:
    """Rule 4 — checks that span multiple strategies.

    Returns {name: [issue, ...]} for any issues found.
    """
    cross_issues: dict[str, list[str]] = {}

    for name, s in all_states.items():
        issues: list[str] = []

        equity = s.get("equity")
        positions: dict = s.get("positions") or {}
        extra: dict = s.get("extra") or {}

        # D and E must have positions once initialized
        if name in PASSIVE_STRATEGY_NAMES:
            initialized = extra.get("initialized", False)
            if initialized and len(positions) == 0:
                issues.append(
                    f"{name} is initialized but holds no positions — "
                    "passive benchmarks should always be deployed"
                )

        # Equity below 50% of initial cash — warning
        if isinstance(equity, (int, float)) and equity > 0:
            if equity < ARENA_INITIAL_CASH * EQUITY_WARNING_THRESHOLD:
                pct = (equity / ARENA_INITIAL_CASH - 1) * 100
                issues.append(
                    f"Equity ${equity:.2f} ({pct:+.1f}%) is below "
                    f"{EQUITY_WARNING_THRESHOLD*100:.0f}% of initial cash "
                    f"${ARENA_INITIAL_CASH:.0f} — severe loss warning"
                )

        if issues:
            cross_issues[name] = issues

    return cross_issues


# ── Main runner ───────────────────────────────────────────────────────────────

def run_verification() -> dict[str, Any]:
    """Load all paper_state files, run all checks, return a summary dict.

    Return schema:
    {
        "timestamp": "...",
        "ok_count": int,
        "warning_count": int,
        "error_count": int,
        "missing": ["name", ...],
        "strategies": {
            "A": {"status": "OK"|"WARNING"|"ERROR", "issues": [...], "warnings": [...]},
            ...
        }
    }
    """
    state_files = _discover_state_files()

    all_states: dict[str, dict[str, Any]] = {}
    missing: list[str] = []

    for name, path in state_files.items():
        state = load_state(path)
        if state is None:
            missing.append(name)
        else:
            all_states[name] = state

    # Per-strategy checks
    per_strategy: dict[str, dict[str, Any]] = {}
    for name, s in all_states.items():
        issues = verify_strategy(s)
        errors, warnings = _classify(issues)
        if errors:
            status = "ERROR"
        elif warnings:
            status = "WARNING"
        else:
            status = "OK"
        per_strategy[name] = {
            "status": status,
            "issues": errors + warnings,   # combined for JSON output
            "errors": errors,
            "warnings": warnings,
        }

    # Cross-strategy checks — merge into per_strategy
    cross = _cross_strategy_checks(all_states)
    for name, issues in cross.items():
        if name not in per_strategy:
            # Strategy loaded but had no per-strategy issues yet
            per_strategy[name] = {
                "status": "OK",
                "issues": [],
                "errors": [],
                "warnings": [],
            }
        errors, warnings = _classify(issues)
        per_strategy[name]["errors"].extend(errors)
        per_strategy[name]["warnings"].extend(warnings)
        per_strategy[name]["issues"].extend(errors + warnings)
        # Upgrade status if needed
        if errors:
            per_strategy[name]["status"] = "ERROR"
        elif warnings and per_strategy[name]["status"] == "OK":
            per_strategy[name]["status"] = "WARNING"

    # Aggregate counts
    ok_count = sum(1 for v in per_strategy.values() if v["status"] == "OK")
    warning_count = sum(1 for v in per_strategy.values() if v["status"] == "WARNING")
    error_count = sum(1 for v in per_strategy.values() if v["status"] == "ERROR")

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "ok_count": ok_count,
        "warning_count": warning_count,
        "error_count": error_count,
        "missing": missing,
        "strategies": {
            name: {
                "status": v["status"],
                "issues": v["issues"],
            }
            for name, v in per_strategy.items()
        },
    }


# ── CLI entry ─────────────────────────────────────────────────────────────────

def main() -> int:
    """Print verification results and write arena_verification.json.

    Returns 0 if all strategies OK, 1 if any errors, 2 if no state files found.
    """
    result = run_verification()

    state_files = _discover_state_files()
    if not state_files:
        print(f"No paper_state_*.json files found in {ARENA_STATE_DIR}")
        return 2

    # Print per-strategy status (sorted by name for determinism)
    for name in sorted(result["strategies"]):
        info = result["strategies"][name]
        status = info["status"]
        issues = info["issues"]

        if status == "OK":
            icon = "✅"  # ✅
            print(f"{icon} [{name}] OK")
        elif status == "WARNING":
            for issue in issues:
                icon = "⚠️ " if _is_warning(issue) else "❌"
                print(f"{icon} [{name}] {issue}")
        else:  # ERROR
            for issue in issues:
                icon = "❌"  # ❌
                print(f"{icon} [{name}] {issue}")

    if result["missing"]:
        for name in result["missing"]:
            print(f"⚠️  [{name}] State file missing or unreadable")

    # Summary line
    ok = result["ok_count"]
    warn = result["warning_count"]
    err = result["error_count"]
    parts = [f"{ok} OK"]
    if warn:
        parts.append(f"{warn} warning{'s' if warn != 1 else ''}")
    if err:
        parts.append(f"{err} error{'s' if err != 1 else ''}")
    print(f"\nSummary: {', '.join(parts)}")

    # Write JSON output
    try:
        os.makedirs(ARENA_STATE_DIR, exist_ok=True)
        with open(VERIFICATION_PATH, "w") as f:
            json.dump(result, f, indent=2)
    except (IOError, OSError) as exc:
        print(f"Warning: could not write {VERIFICATION_PATH}: {exc}")

    return 1 if result["error_count"] > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
