"""Live Trader v4 — Exchange Abstraction (Binance data + Coinbase orders)"""
import os
import numpy as np
import ccxt
from config import SYMBOL_MAP, CB_TO_BIN


def get_coinbase_exchange():
    """Initialize Coinbase Advanced exchange with auth."""
    ex = ccxt.coinbaseadvanced({
        'apiKey': os.getenv('COINBASE_API_KEY'),
        'secret': os.getenv('COINBASE_API_SECRET'),
        'options': {
            'apiType': 'advanced',
            'createMarketBuyOrderRequiresPrice': False,
        },
        'enableRateLimit': True,
    })
    return ex


def fetch_ohlcv(bin_sym, timeframe='4h', limit=300):
    """
    Fetch OHLCV from Binance.
    Returns (closes, highs, lows) arrays or (None, None, None) on error.
    """
    try:
        ex = ccxt.binance({'enableRateLimit': True})
        data = ex.fetch_ohlcv(bin_sym, timeframe, limit=limit)
        if not data:
            return None, None, None
        closes = np.array([c[4] for c in data], dtype=float)
        highs = np.array([c[2] for c in data], dtype=float)
        lows = np.array([c[3] for c in data], dtype=float)
        return closes, highs, lows
    except Exception:
        return None, None, None


def fetch_close(bin_sym, timeframe='4h', limit=300):
    """
    Fetch closes only from Binance.
    Returns numpy array or None on error.
    """
    closes, _, _ = fetch_ohlcv(bin_sym, timeframe, limit)
    return closes


def fetch_1d_closes(bin_sym, limit=250):
    """
    Fetch 1D closes from Binance (for momentum rotation strategy).
    Needs ≥ MA200 + lookback window, so default limit=250.
    Returns numpy array or None on error.
    """
    return fetch_close(bin_sym, timeframe='1d', limit=limit)


def to_cb(bin_sym):
    """Convert Binance symbol to Coinbase (e.g. "BTC/USDT" -> "BTC-USD")."""
    return SYMBOL_MAP.get(bin_sym, bin_sym)


def to_bin(cb_sym):
    """Convert Coinbase symbol to Binance (e.g. "BTC-USD" -> "BTC/USDT")."""
    return CB_TO_BIN.get(cb_sym, cb_sym)
