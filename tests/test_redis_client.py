"""Tests for RedisClient — position enrichment, cooldowns, earnings dates.

All Redis operations are mocked at the client level (no real Redis needed).
"""

import json
import pytest
import unittest.mock
from datetime import datetime, timezone, timedelta
from decimal import Decimal

from stop_loss_guardian.redis_client import RedisClient
from stop_loss_guardian.models import AccountState


@pytest.fixture
def redis_client():
    """RedisClient with a mocked internal redis.Redis client."""
    rc = RedisClient.__new__(RedisClient)
    rc.client = unittest.mock.MagicMock()
    return rc


# ---------------------------------------------------------------------------
# get_positions
# ---------------------------------------------------------------------------

class TestGetPositions:
    def test_returns_parsed_positions(self, redis_client):
        redis_client.client.hgetall.return_value = {
            "AAPL": json.dumps({"symbol": "AAPL", "quantity": "10", "equity": "1500"}),
            "GOOG": json.dumps({"symbol": "GOOG", "quantity": "5", "equity": "7500"}),
        }
        result = redis_client.get_positions()
        assert len(result) == 2
        assert result["AAPL"]["symbol"] == "AAPL"

    def test_empty_hash(self, redis_client):
        redis_client.client.hgetall.return_value = {}
        assert redis_client.get_positions() == {}

    def test_bad_json_skipped(self, redis_client):
        redis_client.client.hgetall.return_value = {
            "AAPL": "not-json",
            "GOOG": json.dumps({"symbol": "GOOG"}),
        }
        result = redis_client.get_positions()
        assert "GOOG" in result
        assert "AAPL" not in result

    def test_redis_error_returns_empty(self, redis_client):
        redis_client.client.hgetall.side_effect = Exception("Connection lost")
        assert redis_client.get_positions() == {}


# ---------------------------------------------------------------------------
# get_position
# ---------------------------------------------------------------------------

class TestGetPosition:
    def test_found(self, redis_client):
        redis_client.client.hget.return_value = json.dumps({"symbol": "AAPL"})
        result = redis_client.get_position("AAPL")
        assert result["symbol"] == "AAPL"

    def test_not_found(self, redis_client):
        redis_client.client.hget.return_value = None
        assert redis_client.get_position("AAPL") is None


# ---------------------------------------------------------------------------
# get_current_price
# ---------------------------------------------------------------------------

class TestGetCurrentPrice:
    def test_calculates_from_equity(self, redis_client):
        redis_client.client.hget.return_value = json.dumps({
            "equity": "1500.00", "quantity": "10",
        })
        price = redis_client.get_current_price("AAPL")
        assert price == Decimal("150")

    def test_zero_quantity_returns_none(self, redis_client):
        redis_client.client.hget.return_value = json.dumps({
            "equity": "1500.00", "quantity": "0",
        })
        assert redis_client.get_current_price("AAPL") is None

    def test_no_position_returns_none(self, redis_client):
        redis_client.client.hget.return_value = None
        assert redis_client.get_current_price("AAPL") is None


# ---------------------------------------------------------------------------
# enrich_position
# ---------------------------------------------------------------------------

class TestEnrichPosition:
    def test_enriches_with_redis_data(self, redis_client):
        redis_client.client.hget.return_value = json.dumps({
            "equity": "950.00", "quantity": "10",
            "percent_change": "-5.0", "equity_change": "-50.0",
            "updated_at": "2026-02-21T10:00:00+00:00",
        })
        result = redis_client.enrich_position("AAPL", Decimal("100"), Decimal("10"))
        assert result["current_price"] == Decimal("95")
        assert result["percent_change"] == Decimal("-5.0")
        assert result["updated_at"] == "2026-02-21T10:00:00+00:00"

    def test_missing_position_returns_nones(self, redis_client):
        redis_client.client.hget.return_value = None
        result = redis_client.enrich_position("AAPL", Decimal("100"), Decimal("10"))
        assert result["current_price"] is None
        assert result["updated_at"] is None

    def test_zero_quantity_in_redis(self, redis_client):
        redis_client.client.hget.return_value = json.dumps({
            "equity": "0", "quantity": "0",
            "percent_change": "0", "equity_change": "0",
        })
        result = redis_client.enrich_position("AAPL", Decimal("100"), Decimal("10"))
        assert result["current_price"] is None


# ---------------------------------------------------------------------------
# get_account_state
# ---------------------------------------------------------------------------

class TestGetAccountState:
    def test_parses_account(self, redis_client):
        redis_client.client.get.return_value = json.dumps({
            "buying_power": "500.00", "cash": "300.00",
            "total_equity": "888.00",
            "updated_at": "2026-02-21T10:00:00",
        })
        state = redis_client.get_account_state()
        assert isinstance(state, AccountState)
        assert state.total_equity == Decimal("888.00")
        assert state.buying_power == Decimal("500.00")

    def test_no_data_returns_none(self, redis_client):
        redis_client.client.get.return_value = None
        assert redis_client.get_account_state() is None

    def test_bad_json_returns_none(self, redis_client):
        redis_client.client.get.return_value = "not-json"
        assert redis_client.get_account_state() is None


# ---------------------------------------------------------------------------
# get_stop_order
# ---------------------------------------------------------------------------

class TestGetStopOrder:
    def test_found(self, redis_client):
        redis_client.client.hget.return_value = json.dumps({
            "stop_price": "90.00", "quantity": "10",
        })
        result = redis_client.get_stop_order("AAPL")
        assert result["stop_price"] == "90.00"

    def test_not_found(self, redis_client):
        redis_client.client.hget.return_value = None
        assert redis_client.get_stop_order("AAPL") is None


# ---------------------------------------------------------------------------
# Drawdown cooldowns
# ---------------------------------------------------------------------------

class TestDrawdownCooldowns:
    def test_get_empty(self, redis_client):
        redis_client.client.hgetall.return_value = {}
        assert redis_client.get_drawdown_cooldowns() == {}

    def test_get_parses_timestamps(self, redis_client):
        now = datetime.now(timezone.utc)
        redis_client.client.hgetall.return_value = {
            "AAPL": now.isoformat(),
        }
        result = redis_client.get_drawdown_cooldowns()
        assert "AAPL" in result
        assert result["AAPL"].tzinfo is not None

    def test_get_handles_naive_timestamps(self, redis_client):
        redis_client.client.hgetall.return_value = {
            "AAPL": "2026-02-21T10:00:00",  # no timezone
        }
        result = redis_client.get_drawdown_cooldowns()
        assert result["AAPL"].tzinfo == timezone.utc

    def test_get_skips_unparseable(self, redis_client):
        redis_client.client.hgetall.return_value = {
            "AAPL": "not-a-timestamp",
            "GOOG": datetime.now(timezone.utc).isoformat(),
        }
        result = redis_client.get_drawdown_cooldowns()
        assert "AAPL" not in result
        assert "GOOG" in result

    def test_set_cooldown(self, redis_client):
        now = datetime.now(timezone.utc)
        redis_client.set_drawdown_cooldown("AAPL", now)
        redis_client.client.hset.assert_called_once_with(
            "guardian:drawdown_cooldowns", "AAPL", now.isoformat(),
        )

    def test_redis_error_on_get_returns_empty(self, redis_client):
        redis_client.client.hgetall.side_effect = Exception("timeout")
        assert redis_client.get_drawdown_cooldowns() == {}


# ---------------------------------------------------------------------------
# get_earnings_date
# ---------------------------------------------------------------------------

class TestGetEarningsDate:
    def test_found(self, redis_client):
        redis_client.client.hget.return_value = json.dumps({
            "next_earnings_date": "2026-03-15",
        })
        assert redis_client.get_earnings_date("AAPL") == "2026-03-15"

    def test_not_found(self, redis_client):
        redis_client.client.hget.return_value = None
        assert redis_client.get_earnings_date("AAPL") is None

    def test_bad_json(self, redis_client):
        redis_client.client.hget.return_value = "not-json"
        assert redis_client.get_earnings_date("AAPL") is None
