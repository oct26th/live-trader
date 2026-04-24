"""Tests for config.py — static data validation."""
import pytest

from config import (
    SYMBOL_MAP,
    CB_TO_BIN,
    REGIME_PARAMS,
    STOP_LOSS_PCT,
    MAX_DRAWDOWN,
    TAKER_FEE,
    MIN_NET_PROFIT,
    MAX_ACTIVE,
    RESERVE_CASH_PCT,
)


def test_symbol_map_has_20_pairs():
    assert len(SYMBOL_MAP) == 20


def test_cb_to_bin_is_exact_inverse():
    """Every bin_sym maps back to itself via SYMBOL_MAP → CB_TO_BIN."""
    for bin_sym, cb_sym in SYMBOL_MAP.items():
        assert CB_TO_BIN[cb_sym] == bin_sym


def test_symbol_map_values_are_unique():
    """No two bin_syms map to the same cb_sym (required for inverse to work)."""
    assert len(set(SYMBOL_MAP.values())) == len(SYMBOL_MAP)


def test_symbol_map_has_btc_as_reference():
    """BTC is required for regime detection."""
    assert "BTC/USDT" in SYMBOL_MAP
    assert SYMBOL_MAP["BTC/USDT"] == "BTC-USD"


# ── Regime params ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("regime", ["TRENDING", "TRANSITION", "RANGE", "UNKNOWN"])
def test_regime_params_defined_for_all_regimes(regime):
    assert regime in REGIME_PARAMS
    params = REGIME_PARAMS[regime]
    assert set(params.keys()) == {"allow_trend", "max_active", "tp1", "tp2", "sl"}


def test_regime_params_sl_is_negative():
    for name, p in REGIME_PARAMS.items():
        assert p["sl"] < 0, f"{name} SL must be negative"


def test_regime_params_tp_ordering():
    """TP2 > TP1 > 0 for every regime."""
    for name, p in REGIME_PARAMS.items():
        assert p["tp1"] > 0, f"{name} TP1 must be positive"
        assert p["tp2"] > p["tp1"], f"{name} TP2 must exceed TP1"


def test_regime_params_range_disables_trend():
    """In RANGE regime, trend-follow track is disabled."""
    assert REGIME_PARAMS["RANGE"]["allow_trend"] is False


def test_regime_params_trending_allows_trend():
    assert REGIME_PARAMS["TRENDING"]["allow_trend"] is True


def test_regime_params_max_active_range_2_trending_3():
    """RANGE=2, TRANSITION=1, TRENDING=3 (per spec)."""
    assert REGIME_PARAMS["RANGE"]["max_active"] == 2
    assert REGIME_PARAMS["TRANSITION"]["max_active"] == 1
    assert REGIME_PARAMS["TRENDING"]["max_active"] == 3


def test_regime_params_range_has_tightest_sl():
    """RANGE regime has the tightest stop-loss (smallest magnitude negative)."""
    assert abs(REGIME_PARAMS["RANGE"]["sl"]) < abs(REGIME_PARAMS["TRENDING"]["sl"])


# ── Constants sanity ───────────────────────────────────────────────────────────

def test_stop_loss_is_negative():
    assert STOP_LOSS_PCT < 0


def test_max_drawdown_is_negative():
    assert MAX_DRAWDOWN < 0


def test_taker_fee_is_reasonable():
    """Fee should be between 0.1% and 1%."""
    assert 0.001 <= TAKER_FEE <= 0.01


def test_min_net_profit_exceeds_fee():
    """Min net profit must beat round-trip fee."""
    assert MIN_NET_PROFIT > 2 * TAKER_FEE


def test_reserve_cash_pct_is_fraction():
    assert 0 < RESERVE_CASH_PCT < 1
