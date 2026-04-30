"""arena_eval.py — evaluate all paper arena strategies and recommend one for live.

Usage:
    python3 arena_eval.py              # quick summary (stdout only)
    python3 arena_eval.py --full       # full report with per-strategy details
    python3 arena_eval.py --json out.json  # write JSON result to file
"""
import argparse
import glob
import json
import math
import os
import sys
from datetime import datetime, timezone
from typing import Optional

import numpy as np

ARENA_STATE_DIR = "/tmp/trading_output"
ARENA_INITIAL_CASH = 1000.0
BENCHMARK_NAMES = {"D", "E"}
ACTIVE_NAMES = {"A", "Aprime", "Adouble", "B"}
SHARPE_GATE = 0.5
WIN_RATE_GATE = 40.0  # %
WIN_RATE_MIN_TRADES = 5  # waive win-rate gate below this


# ── Loaders ───────────────────────────────────────────────────────────────────

def _load_json(path: str) -> Optional[dict]:
    try:
        with open(path) as f:
            return json.load(f)
    except (IOError, json.JSONDecodeError):
        return None


def load_all_states() -> dict[str, dict]:
    """Return a dict mapping strategy name → state dict for all found state files."""
    files = sorted(glob.glob(f"{ARENA_STATE_DIR}/paper_state_*.json"))
    states: dict[str, dict] = {}
    for path in files:
        s = _load_json(path)
        if s and "name" in s:
            states[s["name"]] = s
    return states


# ── Metrics ───────────────────────────────────────────────────────────────────

def _annualization_factor(trades: list[dict]) -> float:
    """sqrt(trades_per_year) derived from trade timestamps.

    Returns 1.0 if fewer than 2 timestamped trades or span < 1 day — caller
    should interpret Sharpe as per-trade in that case.
    """
    timestamps: list[datetime] = []
    for t in trades:
        ts_str = t.get("ts", "")
        if not ts_str:
            continue
        try:
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            timestamps.append(ts)
        except ValueError:
            continue

    if len(timestamps) < 2:
        return 1.0

    span_days = (max(timestamps) - min(timestamps)).total_seconds() / 86400.0
    if span_days < 1.0:
        return 1.0

    trades_per_year = len(trades) * 365.0 / span_days
    return math.sqrt(trades_per_year)


def compute_metrics(s: dict) -> dict:
    """Compute evaluation metrics for one strategy state dict.

    Returns a dict with keys:
        total_return_pct, max_dd_pct, win_rate_pct,
        sharpe, sortino, calmar, trade_count
    """
    equity = float(s.get("equity", ARENA_INITIAL_CASH))
    max_dd_pct = float(s.get("max_dd_pct", 0.0))
    trade_count = int(s.get("trade_count", 0))
    trades_tail: list[dict] = s.get("trades_tail", []) or []

    # 1. Total return
    total_return_pct = (equity / ARENA_INITIAL_CASH - 1.0) * 100.0

    # 2. Win rate (approximate if trade_count > 50, since only last 50 in trades_tail)
    if trades_tail:
        wins = sum(1 for t in trades_tail if float(t.get("pnl_pct", 0.0)) > 0)
        win_rate_pct = wins / len(trades_tail) * 100.0
    else:
        win_rate_pct = 0.0

    # 3. Collect per-trade returns for Sharpe / Sortino
    returns = np.array(
        [float(t.get("pnl_pct", 0.0)) for t in trades_tail], dtype=float
    )

    # 4. Sharpe ratio
    #
    # We have per-trade returns, not daily returns, so we cannot multiply by
    # sqrt(365) — that would inflate Sharpe by however many days separate
    # trades. Instead, infer trades-per-year from trade timestamps and
    # annualize as Sharpe_per_trade × sqrt(trades_per_year).
    #
    # Falls back to a per-trade Sharpe (no annualization) if timestamps are
    # missing or span < 1 day, so the gate threshold (0.5) is still meaningful
    # even in the early days of the paper run.
    if len(returns) < 5:
        sharpe = 0.0
        sortino = 0.0
    else:
        mean_r = float(np.mean(returns))
        std_r = float(np.std(returns, ddof=1))

        ann_factor = _annualization_factor(trades_tail)

        if std_r == 0.0:
            sharpe = 0.0
        else:
            sharpe = float((mean_r / std_r) * ann_factor)

        # Sortino uses downside std (only negative returns in denominator)
        neg_returns = returns[returns < 0]
        if len(neg_returns) == 0:
            # No losing trades — approximate Sortino as 2× Sharpe
            sortino = sharpe * 2.0
        else:
            downside_std = float(np.std(neg_returns, ddof=1)) if len(neg_returns) > 1 else float(abs(neg_returns[0]))
            if downside_std == 0.0:
                sortino = 0.0
            else:
                sortino = float((mean_r / downside_std) * ann_factor)

    # 5. Calmar ratio — deferred: fill in after all strategies computed (needs best Calmar)
    #    We compute the raw numerator/denominator here; caller handles the edge case.
    calmar_raw = (total_return_pct, abs(max_dd_pct))

    return {
        "name": s.get("name", "?"),
        "label": s.get("label", ""),
        "equity": equity,
        "total_return_pct": total_return_pct,
        "max_dd_pct": max_dd_pct,
        "win_rate_pct": win_rate_pct,
        "sharpe": sharpe,
        "sortino": sortino,
        "calmar_raw": calmar_raw,   # (return_pct, abs_dd_pct) — finalized in evaluate()
        "calmar": None,             # filled in by evaluate()
        "trade_count": trade_count,
        "trades_sampled": len(trades_tail),
        "timestamp": s.get("timestamp", ""),
    }


def _finalize_calmar(metrics_list: list[dict]) -> None:
    """Mutate calmar field in-place on each metrics dict.

    If max_dd_pct == 0, set calmar to best non-zero Calmar * 1.5.
    If all strategies have DD == 0, calmar = 0.
    """
    # Compute real Calmar for those with non-zero DD
    real_calmars: list[float] = []
    for m in metrics_list:
        ret_pct, abs_dd = m["calmar_raw"]
        if abs_dd > 0:
            m["calmar"] = ret_pct / abs_dd
            real_calmars.append(m["calmar"])
        else:
            m["calmar"] = None  # placeholder

    best_real = max(real_calmars) if real_calmars else 0.0

    for m in metrics_list:
        if m["calmar"] is None:
            m["calmar"] = best_real * 1.5 if best_real > 0 else 0.0


# ── Gate check ────────────────────────────────────────────────────────────────

def _gate_check(
    m: dict,
    d_return: Optional[float],
    e_return: Optional[float],
) -> tuple[bool, list[str]]:
    """Return (qualifies: bool, failures: list[str])."""
    failures: list[str] = []

    if m["sharpe"] < SHARPE_GATE:
        failures.append(f"Sharpe {m['sharpe']:.2f} < {SHARPE_GATE}")

    if m["trade_count"] >= WIN_RATE_MIN_TRADES:
        if m["win_rate_pct"] < WIN_RATE_GATE:
            failures.append(f"WinRate {m['win_rate_pct']:.1f}% < {WIN_RATE_GATE}%")
    # else: waived

    if d_return is not None and m["total_return_pct"] <= d_return:
        failures.append(
            f"Return {m['total_return_pct']:.2f}% ≤ D benchmark {d_return:.2f}%"
        )

    if e_return is not None and m["total_return_pct"] <= e_return:
        failures.append(
            f"Return {m['total_return_pct']:.2f}% ≤ E benchmark {e_return:.2f}%"
        )

    return len(failures) == 0, failures


# ── Evaluate ──────────────────────────────────────────────────────────────────

def evaluate(states: dict[str, dict]) -> dict:
    """Run full evaluation.

    Returns a dict with keys:
        as_of: str (ISO timestamp)
        all_metrics: dict[name -> metrics dict]
        benchmark_metrics: dict[name -> metrics dict]  (D and E)
        active_metrics: dict[name -> metrics dict]     (A, Aprime, Adouble, B)
        ranked: list[metrics dict]  (qualifying, sorted by Sharpe desc then Calmar desc)
        disqualified: list[dict]    (name, metrics, failures)
        winner: metrics dict | None
        benchmarks_missing: list[str]
        note: str
    """
    all_metrics: dict[str, dict] = {}
    for name, s in states.items():
        all_metrics[name] = compute_metrics(s)

    # Finalize Calmar across all strategies (shared pool for the edge case)
    _finalize_calmar(list(all_metrics.values()))

    benchmark_metrics = {n: m for n, m in all_metrics.items() if n in BENCHMARK_NAMES}
    active_metrics = {n: m for n, m in all_metrics.items() if n in ACTIVE_NAMES}

    benchmarks_missing: list[str] = [n for n in BENCHMARK_NAMES if n not in benchmark_metrics]

    d_return: Optional[float] = benchmark_metrics["D"]["total_return_pct"] if "D" in benchmark_metrics else None
    e_return: Optional[float] = benchmark_metrics["E"]["total_return_pct"] if "E" in benchmark_metrics else None

    ranked: list[dict] = []
    disqualified: list[dict] = []

    for name, m in active_metrics.items():
        qualifies, failures = _gate_check(m, d_return, e_return)
        if qualifies:
            ranked.append(m)
        else:
            disqualified.append({"name": name, "metrics": m, "failures": failures})

    # Sort qualifying: primary Sharpe desc, tiebreak Calmar desc
    ranked.sort(key=lambda m: (m["sharpe"], m.get("calmar") or 0.0), reverse=True)

    winner: Optional[dict] = ranked[0] if ranked else None

    note = ""
    if not winner:
        note = "No qualifying strategy found — extend paper period or review parameters"
    if benchmarks_missing:
        note_bm = f"Benchmark(s) missing: {', '.join(benchmarks_missing)} — gate checks skipped for those"
        note = (note + "  |  " + note_bm).strip(" |")

    return {
        "as_of": datetime.now(timezone.utc).isoformat(),
        "all_metrics": all_metrics,
        "benchmark_metrics": benchmark_metrics,
        "active_metrics": active_metrics,
        "ranked": ranked,
        "disqualified": disqualified,
        "winner": winner,
        "benchmarks_missing": benchmarks_missing,
        "note": note,
    }


# ── Reporting ─────────────────────────────────────────────────────────────────

def _fmt_pct(v: float, width: int = 7, sign: bool = True) -> str:
    s = f"{v:+.2f}%" if sign else f"{v:.2f}%"
    return s.rjust(width)


def _period_days(states: dict[str, dict]) -> int:
    """Estimate elapsed days from oldest trade ts to most recent state ts.

    Uses state timestamps as the "now" reference (not datetime.now), so the
    metric is meaningful when evaluating snapshots taken in the past or when
    the arena last ticked some time ago.
    """
    state_ts: list[datetime] = []
    trade_ts: list[datetime] = []

    def _parse(s: str) -> Optional[datetime]:
        try:
            ts = datetime.fromisoformat(s.replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            return ts
        except (ValueError, AttributeError):
            return None

    for s in states.values():
        ts = _parse(s.get("timestamp", "") or "")
        if ts is not None:
            state_ts.append(ts)
        for t in s.get("trades_tail", []) or []:
            tt = _parse(t.get("ts", "") or "")
            if tt is not None:
                trade_ts.append(tt)

    if not state_ts:
        return 0

    latest = max(state_ts)
    earliest = min(trade_ts) if trade_ts else min(state_ts)
    return max(0, (latest - earliest).days)


def print_report(result: dict, full: bool = False, states: Optional[dict] = None) -> None:
    """Print standings table, gates, winner recommendation."""
    as_of = result["as_of"][:16].replace("T", " ") + " UTC"
    bm = result["benchmark_metrics"]
    ranked = result["ranked"]
    disq = result["disqualified"]
    winner = result["winner"]
    active = result["active_metrics"]
    missing_bms = result["benchmarks_missing"]
    note = result["note"]

    n_strategies = len(result["all_metrics"])
    n_active = len(active)
    days = _period_days(states) if states else 0

    W = 65  # box width

    print()
    print("═" * W)
    print(f"   📊  PAPER ARENA EVALUATION  —  {as_of}")
    print(f"   Period: {days}d  |  Strategies: {n_strategies}  |  Active: {n_active}")
    print()

    # ── Benchmarks ────────────────────────────────────────────────────────
    print("   BENCHMARKS")
    for name in ("D", "E"):
        if name in bm:
            m = bm[name]
            label = m["label"]
            eq = m["equity"]
            ret = m["total_return_pct"]
            dd = m["max_dd_pct"]
            print(f"   {name} ({label:<30})  ${eq:,.2f}  {ret:+.2f}%  DD: {dd:.1f}%")
        else:
            print(f"   {name}  (not found — gate checks for this benchmark skipped)")
    if missing_bms:
        print(f"   ⚠️  Missing benchmarks: {', '.join(missing_bms)}")
    print()

    # ── Active rankings ───────────────────────────────────────────────────
    # Combine ranked + disqualified (sorted by equity for display)
    all_active_sorted: list[tuple[int, dict, Optional[list[str]]]] = []
    rank = 0
    for m in ranked:
        rank += 1
        all_active_sorted.append((rank, m, None))
    for entry in sorted(disq, key=lambda e: e["metrics"]["total_return_pct"], reverse=True):
        all_active_sorted.append((0, entry["metrics"], entry["failures"]))

    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    header = (
        f"   {'Rank':<5} {'Strategy':<10} {'Equity':>9} {'Return':>8}"
        f"  {'MaxDD':>7}  {'Sharpe':>7} {'Calmar':>7} {'WinRate':>8}  Status"
    )
    sep = (
        f"   {'────':<5} {'──────────':<10} {'───────':>9} {'──────':>8}"
        f"  {'──────':>7}  {'──────':>7} {'──────':>7} {'───────':>8}  ──────"
    )
    print("   ACTIVE STRATEGY RANKINGS")
    print(header)
    print(sep)

    for position, m, failures in all_active_sorted:
        medal = medals.get(position, "  ")
        rank_str = f"{position}" if position > 0 else " "
        eq = m["equity"]
        ret = m["total_return_pct"]
        dd = m["max_dd_pct"]
        sharpe = m["sharpe"]
        calmar = m.get("calmar") or 0.0
        wr = m["win_rate_pct"]
        name = m["name"]
        label_short = name

        if failures is None:
            status = "QUALIFIES"
        else:
            fail_short = failures[0].split(" ")[0] if failures else "FAIL"
            # Use the actual gate that failed
            gate_tags = []
            for f in failures:
                if "Sharpe" in f:
                    gate_tags.append("Sharpe<0.5")
                elif "WinRate" in f:
                    gate_tags.append("WinRate<40%")
                elif "≤ D" in f:
                    gate_tags.append("< D")
                elif "≤ E" in f:
                    gate_tags.append("< E")
            status = f"❌ {', '.join(gate_tags)}" if gate_tags else "❌ FAIL"

        print(
            f"   {rank_str:<2} {medal} {label_short:<10} ${eq:>8,.0f}  {ret:>+7.2f}%"
            f"  {dd:>+7.2f}%  {sharpe:>7.2f} {calmar:>7.2f} {wr:>7.1f}%  {status}"
        )
    print()

    # ── Full details (per-strategy) ────────────────────────────────────────
    if full:
        print("   FULL STRATEGY DETAILS")
        print()
        all_names = list(result["all_metrics"].keys())
        for name in all_names:
            m = result["all_metrics"][name]
            print(f"   [{name}]  {m['label']}")
            print(f"     Equity:       ${m['equity']:,.2f}")
            print(f"     Return:       {m['total_return_pct']:+.2f}%")
            print(f"     Max DD:       {m['max_dd_pct']:.2f}%")
            print(f"     Sharpe:       {m['sharpe']:.4f}")
            print(f"     Sortino:      {m['sortino']:.4f}")
            print(f"     Calmar:       {(m.get('calmar') or 0.0):.4f}")
            print(f"     Win rate:     {m['win_rate_pct']:.1f}%  (from {m.get('trades_sampled', 0)} trade samples)")
            print(f"     Trade count:  {m['trade_count']}")
            print(f"     Last tick:    {m['timestamp'][:19].replace('T', ' ')}")
            print()

    # ── Winner / recommendation ───────────────────────────────────────────
    if winner:
        w_name = winner["name"]
        w_ret = winner["total_return_pct"]
        w_sharpe = winner["sharpe"]

        # Beat margins vs benchmarks
        beat_parts = []
        if "D" in bm:
            margin_d = w_ret - bm["D"]["total_return_pct"]
            beat_parts.append(f"beats D ({margin_d:+.1f}%)")
        if "E" in bm:
            margin_e = w_ret - bm["E"]["total_return_pct"]
            beat_parts.append(f"E ({margin_e:+.1f}%)")
        beat_str = " and ".join(beat_parts)

        print(f"   🏆 RECOMMENDATION: Deploy strategy {w_name}")
        print(f"      Highest Sharpe ({w_sharpe:.2f}), {beat_str}")
        print(f"      Run: python3 arena_migrate.py --strategy {w_name} --dry-run-phase 7d")
    else:
        print("   ⏳ No qualifying strategy found — extend paper period or review parameters")
        if note and "missing" in note.lower():
            print(f"   ℹ️  {note}")
    print()

    if note and not note.startswith("No qualifying"):
        print(f"   ℹ️  {note}")
        print()

    print("═" * W)
    print()


# ── JSON serialization helper ─────────────────────────────────────────────────

def _result_for_json(result: dict) -> dict:
    """Return a JSON-serializable version of the result dict (strip calmar_raw tuple)."""
    def _clean(m: dict) -> dict:
        out = {k: v for k, v in m.items() if k != "calmar_raw"}
        return out

    return {
        "as_of": result["as_of"],
        "benchmarks_missing": result["benchmarks_missing"],
        "note": result["note"],
        "winner": _clean(result["winner"]) if result["winner"] else None,
        "ranked": [_clean(m) for m in result["ranked"]],
        "disqualified": [
            {"name": e["name"], "failures": e["failures"], "metrics": _clean(e["metrics"])}
            for e in result["disqualified"]
        ],
        "benchmark_metrics": {n: _clean(m) for n, m in result["benchmark_metrics"].items()},
        "active_metrics": {n: _clean(m) for n, m in result["active_metrics"].items()},
    }


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Evaluate paper arena strategies and recommend one for live deployment."
    )
    parser.add_argument(
        "--full", action="store_true", help="Show per-strategy detail block"
    )
    parser.add_argument(
        "--json", metavar="FILE", help="Write JSON result to FILE"
    )
    parser.add_argument(
        "--dir", metavar="DIR", default=ARENA_STATE_DIR,
        help=f"Override state dir (default: {ARENA_STATE_DIR})"
    )
    args = parser.parse_args()

    # Allow overriding state dir (useful for tests)
    state_dir = args.dir

    files = sorted(glob.glob(f"{state_dir}/paper_state_*.json"))
    if not files:
        print(f"❌ No paper_state_*.json files found in {state_dir}")
        print("   Is the arena running? Check: ps -ef | grep paper_arena")
        return 1

    states = load_all_states() if state_dir == ARENA_STATE_DIR else _load_states_from_dir(state_dir)

    if not states:
        print("❌ All state files unreadable or empty.")
        return 1

    result = evaluate(states)
    print_report(result, full=args.full, states=states)

    if args.json:
        out_path = args.json
        try:
            with open(out_path, "w") as f:
                json.dump(_result_for_json(result), f, indent=2)
            print(f"   📄 JSON written to {out_path}")
        except IOError as e:
            print(f"   ❌ Failed to write JSON: {e}", file=sys.stderr)
            return 1

    return 0 if result["winner"] else 0  # always exit 0; caller reads stdout


def _load_states_from_dir(state_dir: str) -> dict[str, dict]:
    """Load states from an arbitrary directory (for --dir override)."""
    files = sorted(glob.glob(f"{state_dir}/paper_state_*.json"))
    states: dict[str, dict] = {}
    for path in files:
        s = _load_json(path)
        if s and "name" in s:
            states[s["name"]] = s
    return states


if __name__ == "__main__":
    sys.exit(main())
