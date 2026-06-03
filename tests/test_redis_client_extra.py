"""Additional RedisClient tests covering connect/close and error branches."""

import unittest.mock
from datetime import datetime, timezone

import pytest

import stop_loss_guardian.config as _cfg_module
from stop_loss_guardian.redis_client import RedisClient


@pytest.fixture
def redis_client():
    rc = RedisClient.__new__(RedisClient)
    rc.client = unittest.mock.MagicMock()
    return rc


class TestConnectClose:
    def test_connect_success(self):
        fake = unittest.mock.MagicMock()
        import stop_loss_guardian.redis_client as _rmod
        s = _rmod.settings
        # Some settings stubs lack redis_password; ensure it exists.
        if not hasattr(s, "redis_password"):
            s.redis_password = None
        with unittest.mock.patch(
            "stop_loss_guardian.redis_client.redis.Redis",
            return_value=fake, create=True,
        ):
            rc = RedisClient()
            rc.connect()
        assert rc.client is fake
        fake.ping.assert_called_once()

    def test_connect_failure_raises(self):
        s = _cfg_module.settings
        if not hasattr(s, "redis_password"):
            s.redis_password = None
        with unittest.mock.patch(
            "stop_loss_guardian.redis_client.redis.Redis",
            side_effect=RuntimeError("no redis"), create=True,
        ):
            rc = RedisClient()
            with pytest.raises(RuntimeError):
                rc.connect()

    def test_close(self, redis_client):
        client = redis_client.client
        redis_client.close()
        client.close.assert_called_once()
        assert redis_client.client is None

    def test_close_noop_when_no_client(self):
        rc = RedisClient.__new__(RedisClient)
        rc.client = None
        rc.close()  # no exception


class TestErrorBranches:
    def test_get_position_error_returns_none(self, redis_client):
        redis_client.client.hget.side_effect = RuntimeError("boom")
        assert redis_client.get_position("AAPL") is None

    def test_get_current_price_handles_bad_data(self, redis_client):
        redis_client.client.hget.return_value = '{"equity": "abc", "quantity": "1"}'
        assert redis_client.get_current_price("AAPL") is None

    def test_get_account_state_error_returns_none(self, redis_client):
        redis_client.client.get.side_effect = RuntimeError("boom")
        assert redis_client.get_account_state() is None

    def test_enrich_position_error_returns_nones(self, redis_client):
        redis_client.client.hget.return_value = '{"equity": "bad", "quantity": "x"}'
        result = redis_client.enrich_position(
            "AAPL", __import__("decimal").Decimal("10"),
            __import__("decimal").Decimal("1"),
        )
        assert result["current_price"] is None

    def test_get_stop_order_error_returns_none(self, redis_client):
        redis_client.client.hget.side_effect = RuntimeError("boom")
        assert redis_client.get_stop_order("AAPL") is None

    def test_set_drawdown_cooldown_error_swallowed(self, redis_client):
        redis_client.client.hset.side_effect = RuntimeError("boom")
        redis_client.set_drawdown_cooldown("AAPL", datetime.now(timezone.utc))

    def test_get_earnings_date_error_returns_none(self, redis_client):
        redis_client.client.get.side_effect = RuntimeError("boom")
        assert redis_client.get_earnings_date("AAPL") is None

    def test_get_all_stop_orders_parses_and_skips_bad(self, redis_client):
        redis_client.client.hgetall.return_value = {
            "AAPL": '{"stop_price": "90"}',
            "BAD": "not-json",
        }
        result = redis_client.get_all_stop_orders()
        assert "AAPL" in result
        assert "BAD" not in result

    def test_get_all_stop_orders_error_returns_empty(self, redis_client):
        redis_client.client.hgetall.side_effect = RuntimeError("boom")
        assert redis_client.get_all_stop_orders() == {}
