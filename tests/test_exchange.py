"""Tests for exchange.py — symbol conversion (no network)."""
import pytest

from exchange import to_cb, to_bin


def test_to_cb_converts_btc():
    assert to_cb("BTC/USDT") == "BTC-USD"


def test_to_bin_converts_btc():
    assert to_bin("BTC-USD") == "BTC/USDT"


def test_roundtrip_all_symbols():
    from config import SYMBOL_MAP
    for bin_sym in SYMBOL_MAP:
        assert to_bin(to_cb(bin_sym)) == bin_sym


def test_unknown_symbol_passes_through():
    """Unknown symbols return unchanged (graceful degradation)."""
    assert to_cb("UNKNOWN/USDT") == "UNKNOWN/USDT"
    assert to_bin("UNKNOWN-USD") == "UNKNOWN-USD"
