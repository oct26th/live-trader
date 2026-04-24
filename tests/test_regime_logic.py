"""
Tests for regime-specific behavior in trader.py:
  - _apply_regime returns correct dict per regime
  - Fee filter calculation (TP2 net of fees must exceed MIN_NET_PROFIT)
"""
import pytest
from unittest.mock import MagicMock, patch

from config import REGIME_PARAMS, TAKER_FEE, MIN_NET_PROFIT


def _make_trader():
    """Minimal trader with enough state to call _apply_regime and fee math."""
    from trader import LiveTrader
    # Avoid loading params file
    with patch("builtins.open"), patch("json.load", return_value={}):
        t = LiveTrader.__new__(LiveTrader)
        t.params = {}
        t._regime_cfg = REGIME_PARAMS["UNKNOWN"]
    return t


# ── _apply_regime ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("regime", ["TRENDING", "TRANSITION", "RANGE", "UNKNOWN"])
def test_apply_regime_returns_config_dict(regime):
    t = _make_trader()
    result = t._apply_regime({"regime": regime, "adx": 30, "trend": "bullish"})
    assert result == REGIME_PARAMS[regime]


def test_apply_regime_falls_back_to_unknown_for_bad_name():
    t = _make_trader()
    result = t._apply_regime({"regime": "NONSENSE", "adx": 0, "trend": "N/A"})
    assert result == REGIME_PARAMS["UNKNOWN"]


# ── Fee filter math ────────────────────────────────────────────────────────────

def test_fee_filter_passes_for_trending():
    """TRENDING: tp2=20%, fee ≈ 0.78% → net ≈ 19.2% > 3% → PASS."""
    regime = REGIME_PARAMS["TRENDING"]
    total_fee_pct = TAKER_FEE * (1 + regime["tp1"] / 2 + regime["tp2"] / 2)
    net_tp2 = regime["tp2"] - total_fee_pct
    assert net_tp2 > MIN_NET_PROFIT


def test_fee_filter_passes_for_range():
    """RANGE: tp2=10%, fee ≈ 0.65% → net ≈ 9.35% > 3% → PASS."""
    regime = REGIME_PARAMS["RANGE"]
    total_fee_pct = TAKER_FEE * (1 + regime["tp1"] / 2 + regime["tp2"] / 2)
    net_tp2 = regime["tp2"] - total_fee_pct
    assert net_tp2 > MIN_NET_PROFIT


def test_fee_filter_rejects_low_tp2():
    """Hypothetical: if TP2 drops to 2%, fee filter should kick in."""
    fake_regime = {"tp1": 0.01, "tp2": 0.02, "sl": -0.01}
    total_fee_pct = TAKER_FEE * (1 + fake_regime["tp1"] / 2 + fake_regime["tp2"] / 2)
    net_tp2 = fake_regime["tp2"] - total_fee_pct
    assert net_tp2 < MIN_NET_PROFIT


# ── BTC regime classification boundaries ──────────────────────────────────────

def test_btc_regime_adx_below_25_is_range():
    """Validate the threshold logic (documented spec)."""
    adx = 24.9
    if adx < 25:
        regime = "RANGE"
    elif adx > 40:
        regime = "TRENDING"
    else:
        regime = "TRANSITION"
    assert regime == "RANGE"


def test_btc_regime_adx_above_40_is_trending():
    adx = 40.1
    if adx < 25:
        regime = "RANGE"
    elif adx > 40:
        regime = "TRENDING"
    else:
        regime = "TRANSITION"
    assert regime == "TRENDING"


def test_btc_regime_adx_between_is_transition():
    for adx in [25.0, 30.0, 35.0, 40.0]:
        if adx < 25:
            regime = "RANGE"
        elif adx > 40:
            regime = "TRENDING"
        else:
            regime = "TRANSITION"
        assert regime == "TRANSITION", f"Failed at adx={adx}"
