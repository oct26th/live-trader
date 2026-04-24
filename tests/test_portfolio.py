"""Unit tests for portfolio.py."""
from portfolio import Portfolio


def test_initial_portfolio_is_all_cash():
    p = Portfolio(1000.0)
    assert p.cash == 1000.0
    assert p.initial == 1000.0
    assert p.positions == {}
    assert p.trades == []


def test_equity_with_no_positions_equals_cash():
    p = Portfolio(500.0)
    assert p.equity({}) == 500.0
    assert p.equity({"BTC-USD": 50000.0}) == 500.0  # no positions held


def test_equity_with_single_position():
    p = Portfolio(100.0)
    p.positions["BTC-USD"] = 0.01
    prices = {"BTC-USD": 50000.0}
    assert p.equity(prices) == 100.0 + 0.01 * 50000.0  # 600.0


def test_equity_with_multiple_positions():
    p = Portfolio(200.0)
    p.positions["BTC-USD"] = 0.001
    p.positions["ETH-USD"] = 0.1
    prices = {"BTC-USD": 50000.0, "ETH-USD": 3000.0}
    # 200 + 0.001*50000 + 0.1*3000 = 200 + 50 + 300 = 550
    assert p.equity(prices) == 550.0


def test_equity_with_missing_price_defaults_to_zero():
    """Position without a price in `prices` contributes 0."""
    p = Portfolio(100.0)
    p.positions["BTC-USD"] = 0.01
    assert p.equity({}) == 100.0  # missing price → 0 contribution


def test_equity_after_cash_mutation():
    p = Portfolio(100.0)
    p.cash = 50.0
    p.positions["BTC-USD"] = 0.001
    assert p.equity({"BTC-USD": 50000.0}) == 100.0  # 50 + 50
