"""Tests for notifier.py — Discord webhook throttling."""
import time
from unittest.mock import MagicMock, patch
import pytest

from notifier import DiscordNotifier


def test_no_webhook_disables_sending(monkeypatch):
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    n = DiscordNotifier()
    assert n.should_notify(time.time()) is False
    assert n.webhook_url == ""


def test_first_notification_is_always_allowed(monkeypatch):
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://example.com/webhook")
    n = DiscordNotifier()
    assert n.should_notify(time.time()) is True


def test_throttle_blocks_within_hour(monkeypatch):
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://example.com/webhook")
    n = DiscordNotifier()
    now = time.time()
    n.last_notify_at = now
    # 59 minutes later
    assert n.should_notify(now + 59 * 60) is False


def test_throttle_allows_after_hour(monkeypatch):
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://example.com/webhook")
    n = DiscordNotifier()
    now = time.time()
    n.last_notify_at = now
    # 61 minutes later
    assert n.should_notify(now + 61 * 60) is True


def test_send_no_webhook_is_noop(monkeypatch):
    """send() returns silently if no webhook configured."""
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    n = DiscordNotifier()
    trader = MagicMock()
    btc = {"regime": "TRENDING", "adx": 35.0, "trend": "bullish"}
    # Should not raise
    n.send(trader, btc)


def test_send_posts_on_first_call(monkeypatch):
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://example.com/webhook")
    n = DiscordNotifier()

    trader = MagicMock()
    trader.portfolio.equity.return_value = 1100.0
    trader.portfolio.initial = 1000.0
    trader._peak_equity = 1200.0
    trader.portfolio.positions = {"BTC-USD": 0.01}
    trader._active_set = {"BTC/USDT"}
    trader.prices = {}

    btc = {"regime": "TRENDING", "adx": 35.0, "trend": "bullish"}

    with patch("notifier.requests.post") as mock_post:
        n.send(trader, btc)
        mock_post.assert_called_once()
        url, kwargs = mock_post.call_args
        assert url[0] == "https://example.com/webhook"
        payload = kwargs["json"]
        assert "embeds" in payload
        assert len(payload["embeds"]) == 1


def test_send_is_silent_on_http_failure(monkeypatch):
    """Network errors must not crash the trading loop."""
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://example.com/webhook")
    n = DiscordNotifier()
    trader = MagicMock()
    trader.portfolio.equity.return_value = 1000.0
    trader.portfolio.initial = 1000.0
    trader._peak_equity = 1000.0
    trader.portfolio.positions = {}
    trader._active_set = set()
    trader.prices = {}
    btc = {"regime": "UNKNOWN", "adx": 0, "trend": "N/A"}

    with patch("notifier.requests.post", side_effect=Exception("network down")):
        # Must not raise
        n.send(trader, btc)
