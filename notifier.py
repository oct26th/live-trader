"""Live Trader v4 — Discord Notifications"""
import os
import requests
from datetime import datetime


class DiscordNotifier:
    """Send hourly trade reports via Discord webhook."""

    def __init__(self, paper_mode: bool = False):
        self.webhook_url = os.getenv("DISCORD_WEBHOOK_URL", "")
        self.last_notify_at = None
        self.paper_mode = paper_mode

    def should_notify(self, current_time):
        """Check if enough time has passed since last notification."""
        if not self.webhook_url:
            return False
        if self.last_notify_at is None:
            return True
        return (current_time - self.last_notify_at) >= 3600  # 1 hour

    def send(self, trader, btc_regime):
        """Send a summary embed to Discord."""
        if not self.webhook_url:
            return

        import time
        current_time = time.time()
        if not self.should_notify(current_time):
            return

        eq = trader.portfolio.equity(trader.prices)
        ret = (eq / trader.portfolio.initial - 1) * 100
        dd = (eq / trader._peak_equity - 1) * 100 if trader._peak_equity else 0
        positions_str = ", ".join(trader.portfolio.positions.keys()) or "None"
        active_str = ", ".join(sorted(trader._active_set)) or "None"

        title_prefix = "🧪 [PAPER] " if self.paper_mode else "🤖 "
        embed = {
            "title": f"{title_prefix}Live Trader v4",
            "color": 0x888888 if self.paper_mode else (0x00FF00 if ret > 0 else 0xFF0000),
            "fields": [
                {
                    "name": "Market Regime",
                    "value": f"[{btc_regime['regime']} ADX={btc_regime['adx']} {btc_regime['trend']}]",
                    "inline": False,
                },
                {
                    "name": "Portfolio",
                    "value": f"Equity: ${eq:.2f} | Return: {ret:+.2f}% | DD: {dd:+.2f}%",
                    "inline": False,
                },
                {
                    "name": "Positions",
                    "value": positions_str,
                    "inline": True,
                },
                {
                    "name": "Active Set",
                    "value": active_str,
                    "inline": True,
                },
                {
                    "name": "Timestamp",
                    "value": datetime.now().strftime("%Y-%m-%d %H:%M UTC"),
                    "inline": True,
                },
            ],
        }

        try:
            requests.post(
                self.webhook_url,
                json={"embeds": [embed]},
                timeout=5,
            )
            self.last_notify_at = current_time
        except Exception as e:
            pass  # Silent fail
