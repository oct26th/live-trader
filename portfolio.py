"""Live Trader v4 — Portfolio Management"""


class Portfolio:
    """Track cash, positions, and trade history."""

    def __init__(self, initial_usd):
        self.initial = initial_usd
        self.cash = initial_usd
        self.positions = {}  # {cb_sym: qty}
        self.trades = []

    def equity(self, prices):
        """Total equity = cash + market value of positions."""
        return self.cash + sum(
            qty * prices.get(s, 0) for s, qty in self.positions.items()
        )
