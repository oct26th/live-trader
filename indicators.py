"""Live Trader v4 — Technical Indicators"""
import numpy as np


def _ema(arr, span):
    """Exponential moving average."""
    a = 2 / (span + 1)
    out = np.empty(len(arr), dtype=float)
    out[0] = arr[0]
    for i in range(1, len(arr)):
        out[i] = a * arr[i] + (1 - a) * out[i - 1]
    return out


def _rsi(arr, period=14):
    """Relative strength index."""
    d = np.diff(arr, prepend=arr[0])
    g, l = np.maximum(d, 0.0), np.maximum(-d, 0.0)
    ag, al = np.empty(len(arr), dtype=float), np.empty(len(arr), dtype=float)
    ag[0], al[0] = g[0], l[0]
    for i in range(1, len(arr)):
        ag[i] = (ag[i - 1] * (period - 1) + g[i]) / period
        al[i] = (al[i - 1] * (period - 1) + l[i]) / period
    rs = np.divide(ag, al, out=np.zeros_like(ag), where=al != 0)
    return 100 - (100 / (1 + rs))


def _adx(high, low, close, period=14):
    """Average directional index."""
    n = len(close)
    if n < period * 3:
        return np.zeros(n)
    tr = np.zeros(n)
    dp = np.zeros(n)
    dm = np.zeros(n)
    for i in range(1, n):
        hl = high[i] - low[i]
        hc = abs(high[i] - close[i - 1])
        lc = abs(low[i] - close[i - 1])
        tr[i] = max(hl, hc, lc)
        up = high[i] - high[i - 1]
        dn = low[i - 1] - low[i]
        dp[i] = up if up > dn and up > 0 else 0
        dm[i] = dn if dn > up and dn > 0 else 0
    tr_s = np.zeros(n)
    dp_s = np.zeros(n)
    dm_s = np.zeros(n)
    tr_s[period] = tr[1 : period + 1].sum()
    dp_s[period] = dp[1 : period + 1].sum()
    dm_s[period] = dm[1 : period + 1].sum()
    for i in range(period + 1, n):
        tr_s[i] = tr_s[i - 1] - tr_s[i - 1] / period + tr[i]
        dp_s[i] = dp_s[i - 1] - dp_s[i - 1] / period + dp[i]
        dm_s[i] = dm_s[i - 1] - dm_s[i - 1] / period + dm[i]
    di_p = np.divide(dp_s, tr_s, out=np.zeros_like(tr_s), where=tr_s != 0) * 100
    di_m = np.divide(dm_s, tr_s, out=np.zeros_like(tr_s), where=tr_s != 0) * 100
    dx = np.divide(
        np.abs(di_p - di_m),
        di_p + di_m,
        out=np.zeros_like(di_p),
        where=(di_p + di_m) != 0,
    ) * 100
    adx = np.zeros(n)
    adx[period * 2] = dx[period : period * 2].mean()
    for i in range(period * 2 + 1, n):
        adx[i] = (adx[i - 1] * (period - 1) + dx[i]) / period
    return adx


def compute(close, p, high=None, low=None):
    """Compute all indicators given params dict."""
    ma_f = _ema(close, p["ma_fast"])
    ma_s = _ema(close, p["ma_slow"])
    mline = _ema(close, p["macd_fast"]) - _ema(close, p["macd_slow"])
    msig = _ema(mline, p["macd_signal"])
    macd_h = mline - msig
    rsi_v = _rsi(close, p["rsi_period"])
    mac = np.where(
        ma_f > ma_s, 1.0, np.where(ma_f < ma_s, -1.0, 0.0)
    )
    mcx = np.zeros(len(close), dtype=float)
    for i in range(1, len(close)):
        if macd_h[i] > 0 > macd_h[i - 1]:
            mcx[i] = 1.0
        elif macd_h[i] < 0 < macd_h[i - 1]:
            mcx[i] = -1.0
    adx_v = (
        _adx(high, low, close, p["rsi_period"])
        if high is not None and low is not None
        else np.zeros(len(close))
    )
    return {
        "ma_f": ma_f,
        "ma_s": ma_s,
        "macd_h": macd_h,
        "mcross": mcx,
        "rsi": rsi_v,
        "ma_cross": mac,
        "adx": adx_v,
    }


def signal_at(ind, i, p):
    """Compute BUY/SELL/HOLD signal at candle i."""
    mac = ind["ma_cross"][i]
    mac_h = ind["macd_h"][i]
    mx = ind["mcross"][i]
    rsi = ind["rsi"][i]
    c1 = (mac == 1) and (mac_h > 0) and (rsi < p["rsi_buy"])
    c2 = (mx == 1) and (mac == 1) and (30 < rsi < 55)
    if c1 or c2:
        return "BUY"
    if rsi > p["rsi_sell"] or mac < 0:
        return "SELL"
    return "HOLD"
