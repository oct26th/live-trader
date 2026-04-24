"""Tests for state.py — save/load round-trip and restore semantics."""
import json
import os
import time
import logging
import tempfile
from unittest.mock import patch, MagicMock
import pytest

from portfolio import Portfolio


@pytest.fixture
def tmp_state_path(tmp_path, monkeypatch):
    """Redirect STATE_PATH to a temp file for test isolation."""
    path = str(tmp_path / "live_state.json")
    import config
    monkeypatch.setattr(config, "STATE_PATH", path)
    # state.py imports STATE_PATH at module load, so we need to reload or patch
    import state
    monkeypatch.setattr(state, "STATE_PATH", path)
    return path


@pytest.fixture
def mock_trader():
    """A minimal trader-like object with all state fields."""
    t = MagicMock()
    t.portfolio = Portfolio(1000.0)
    t.portfolio.cash = 800.0
    t.portfolio.positions = {"BTC-USD": 0.01}
    t.portfolio.trades = []
    t.prices = {"BTC-USD": 50000.0}
    t._entry_px = {"BTC-USD": 49000.0}
    t._peak_equity = 1200.0
    t._pause_until = None
    t._partial_sells = {"BTC-USD": {"tp1_done": False, "tp2_done": False}}
    t._pool_scores = {}
    t._active_set = {"BTC/USDT", "ETH/USDT"}
    t._last_rotation = time.time() - 3600
    t._last_score_at = time.time() - 300
    t._blacklist = {}
    t._rotation_count = 5
    t.log = logging.getLogger("test")
    return t


# ── Save ───────────────────────────────────────────────────────────────────────

def test_save_creates_file(tmp_state_path, mock_trader):
    from state import save_state
    save_state(mock_trader)
    assert os.path.exists(tmp_state_path)


def test_save_contains_expected_keys(tmp_state_path, mock_trader):
    from state import save_state
    save_state(mock_trader)
    with open(tmp_state_path) as f:
        data = json.load(f)
    assert "timestamp" in data
    assert "portfolio" in data
    assert "_peak_equity" in data
    assert "_active_set" in data
    assert "_blacklist" in data
    assert data["_rotation_count"] == 5


def test_save_serializes_set_as_list(tmp_state_path, mock_trader):
    """_active_set is a set in memory but must become a list in JSON."""
    from state import save_state
    save_state(mock_trader)
    with open(tmp_state_path) as f:
        data = json.load(f)
    assert isinstance(data["_active_set"], list)
    assert set(data["_active_set"]) == {"BTC/USDT", "ETH/USDT"}


# ── Load ───────────────────────────────────────────────────────────────────────

def test_load_returns_false_when_no_file(tmp_state_path, mock_trader):
    from state import load_state
    # File doesn't exist
    assert load_state(mock_trader) is False


def test_load_returns_false_on_corrupt_json(tmp_state_path, mock_trader):
    with open(tmp_state_path, "w") as f:
        f.write("{not valid json")
    from state import load_state
    assert load_state(mock_trader) is False


def test_load_restores_peak_equity(tmp_state_path, mock_trader):
    from state import save_state, load_state
    mock_trader._peak_equity = 1500.0
    save_state(mock_trader)

    # Reset and reload
    fresh = MagicMock()
    fresh.portfolio = Portfolio(1000.0)
    fresh.portfolio.positions = {"BTC-USD": 0.01}
    fresh._entry_px = {}
    fresh._peak_equity = None
    fresh._pause_until = None
    fresh._partial_sells = {}
    fresh._active_set = set()
    fresh._blacklist = {}
    fresh._last_rotation = None
    fresh._last_score_at = None
    fresh._rotation_count = 0
    fresh.log = logging.getLogger("test")

    assert load_state(fresh) is True
    assert fresh._peak_equity == 1500.0


def test_load_only_restores_entry_px_for_live_positions(tmp_state_path, mock_trader):
    """Ghost _entry_px entries (no matching position) must be dropped."""
    from state import save_state, load_state
    mock_trader._entry_px = {"BTC-USD": 49000.0, "ETH-USD": 3000.0, "GHOST-USD": 1.0}
    save_state(mock_trader)

    fresh = MagicMock()
    fresh.portfolio = Portfolio(1000.0)
    fresh.portfolio.positions = {"BTC-USD": 0.01}  # only BTC is live
    fresh._entry_px = {}
    fresh._peak_equity = None
    fresh._pause_until = None
    fresh._partial_sells = {}
    fresh._active_set = set()
    fresh._blacklist = {}
    fresh._last_rotation = None
    fresh._last_score_at = None
    fresh._rotation_count = 0
    fresh.log = logging.getLogger("test")

    load_state(fresh)
    assert fresh._entry_px == {"BTC-USD": 49000.0}


def test_load_filters_expired_blacklist(tmp_state_path, mock_trader):
    """Blacklist entries with past unlock_ts must be discarded on load."""
    from state import save_state, load_state
    now = time.time()
    mock_trader._blacklist = {
        "OLD/USDT": now - 3600,        # already expired
        "ACTIVE/USDT": now + 86400,    # still active
    }
    save_state(mock_trader)

    fresh = MagicMock()
    fresh.portfolio = Portfolio(1000.0)
    fresh.portfolio.positions = {}
    fresh._entry_px = {}
    fresh._peak_equity = None
    fresh._pause_until = None
    fresh._partial_sells = {}
    fresh._active_set = set()
    fresh._blacklist = {}
    fresh._last_rotation = None
    fresh._last_score_at = None
    fresh._rotation_count = 0
    fresh.log = logging.getLogger("test")

    load_state(fresh)
    assert "OLD/USDT" not in fresh._blacklist
    assert "ACTIVE/USDT" in fresh._blacklist


def test_load_skips_past_pause_until(tmp_state_path, mock_trader):
    """If _pause_until is in the past, don't restore it."""
    from state import save_state, load_state
    mock_trader._pause_until = time.time() - 3600
    save_state(mock_trader)

    fresh = MagicMock()
    fresh.portfolio = Portfolio(1000.0)
    fresh.portfolio.positions = {}
    fresh._entry_px = {}
    fresh._peak_equity = None
    fresh._pause_until = None
    fresh._partial_sells = {}
    fresh._active_set = set()
    fresh._blacklist = {}
    fresh._last_rotation = None
    fresh._last_score_at = None
    fresh._rotation_count = 0
    fresh.log = logging.getLogger("test")

    load_state(fresh)
    assert fresh._pause_until is None


def test_load_rejects_stale_file(tmp_state_path, mock_trader):
    """State older than 24h must not be restored."""
    from state import save_state, load_state
    save_state(mock_trader)
    # Manually backdate the timestamp in the file to 25h ago
    from datetime import datetime, timedelta
    stale_ts = (datetime.now() - timedelta(hours=25)).isoformat()
    with open(tmp_state_path) as f:
        data = json.load(f)
    data["timestamp"] = stale_ts
    with open(tmp_state_path, "w") as f:
        json.dump(data, f)

    fresh = MagicMock()
    fresh.portfolio = Portfolio(1000.0)
    fresh.portfolio.positions = {"BTC-USD": 0.01}
    fresh._entry_px = {}
    fresh._peak_equity = None
    fresh._pause_until = None
    fresh._partial_sells = {}
    fresh._active_set = set()
    fresh._blacklist = {}
    fresh._last_rotation = None
    fresh._last_score_at = None
    fresh._rotation_count = 0
    fresh.log = logging.getLogger("test")

    assert load_state(fresh) is False
    # Nothing should be restored
    assert fresh._peak_equity is None
