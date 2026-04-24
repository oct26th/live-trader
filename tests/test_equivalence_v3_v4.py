"""
Golden equivalence: verify v4 indicators produce identical output to archived v3.

The v4 refactor split the monolith into modules but must not change numerical behavior.
These tests import v3's compute/signal_at from archive_v3.py and compare against v4's
indicators module on the same inputs.
"""
import importlib.util
import os
import sys

import numpy as np
import pytest

from indicators import compute as compute_v4
from indicators import signal_at as signal_at_v4


@pytest.fixture(scope="module")
def v3_module():
    """Load archive_v3.py as a module for side-by-side comparison."""
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    archive_path = os.path.join(repo_root, "archive_v3.py")

    # archive_v3.py imports ccxt inside functions and tries to load dotenv;
    # we stub the dotenv call so module-level import doesn't fail.
    sys.path.insert(0, repo_root)

    # Prevent load_dotenv from exploding on missing .env
    import dotenv  # noqa: F401
    original_load = dotenv.load_dotenv
    dotenv.load_dotenv = lambda *a, **kw: False

    try:
        spec = importlib.util.spec_from_file_location("archive_v3", archive_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        dotenv.load_dotenv = original_load

    return mod


@pytest.fixture
def default_params():
    return {
        "ma_fast": 20,
        "ma_slow": 50,
        "macd_fast": 12,
        "macd_slow": 26,
        "macd_signal": 9,
        "rsi_period": 14,
        "rsi_buy": 38,
        "rsi_sell": 70,
    }


@pytest.fixture
def rising_series():
    return np.arange(1.0, 201.0)


@pytest.fixture
def noisy_series():
    rng = np.random.default_rng(42)
    t = np.arange(300)
    return 100 + 10 * np.sin(t * 0.1) + rng.normal(0, 1, 300)


# ── EMA ────────────────────────────────────────────────────────────────────────

def test_ema_matches_v3(v3_module, rising_series):
    v3 = v3_module._ema(rising_series, span=20)
    from indicators import _ema as v4
    out = v4(rising_series, span=20)
    assert np.allclose(v3, out)


# ── RSI ────────────────────────────────────────────────────────────────────────

def test_rsi_matches_v3(v3_module, noisy_series):
    v3 = v3_module._rsi(noisy_series, period=14)
    from indicators import _rsi as v4
    out = v4(noisy_series, period=14)
    assert np.allclose(v3, out)


# ── ADX ────────────────────────────────────────────────────────────────────────

def test_adx_matches_v3(v3_module, noisy_series):
    highs = noisy_series + 0.5
    lows = noisy_series - 0.5
    v3 = v3_module._adx(highs, lows, noisy_series, period=14)
    from indicators import _adx as v4
    out = v4(highs, lows, noisy_series, period=14)
    assert np.allclose(v3, out)


# ── compute (full indicator bundle) ────────────────────────────────────────────

def test_compute_matches_v3_no_high_low(v3_module, noisy_series, default_params):
    v3 = v3_module.compute(noisy_series, default_params)
    v4 = compute_v4(noisy_series, default_params)
    for key in ("ma_f", "ma_s", "macd_h", "mcross", "rsi", "ma_cross", "adx"):
        assert np.allclose(v3[key], v4[key]), f"Mismatch on {key}"


def test_compute_matches_v3_with_high_low(v3_module, noisy_series, default_params):
    highs = noisy_series + 0.5
    lows = noisy_series - 0.5
    v3 = v3_module.compute(noisy_series, default_params, high=highs, low=lows)
    v4 = compute_v4(noisy_series, default_params, high=highs, low=lows)
    for key in ("ma_f", "ma_s", "macd_h", "mcross", "rsi", "ma_cross", "adx"):
        assert np.allclose(v3[key], v4[key]), f"Mismatch on {key}"


# ── signal_at ──────────────────────────────────────────────────────────────────

def test_signal_at_matches_v3(v3_module, noisy_series, default_params):
    v3_ind = v3_module.compute(noisy_series, default_params)
    v4_ind = compute_v4(noisy_series, default_params)
    # Check the last 50 bars — warm-up at the start is all zeros anyway
    for i in range(250, 300):
        v3_sig = v3_module.signal_at(v3_ind, i, default_params)
        v4_sig = signal_at_v4(v4_ind, i, default_params)
        assert v3_sig == v4_sig, f"Mismatch at i={i}: v3={v3_sig}, v4={v4_sig}"


# ── SYMBOL_MAP ─────────────────────────────────────────────────────────────────

def test_symbol_map_matches_v3(v3_module):
    from config import SYMBOL_MAP as v4_map
    assert v3_module.SYMBOL_MAP == v4_map


def test_cb_to_bin_matches_v3(v3_module):
    from config import CB_TO_BIN as v4_inv
    assert v3_module.CB_TO_BIN == v4_inv
