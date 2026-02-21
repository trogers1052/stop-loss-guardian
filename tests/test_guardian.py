"""
Unit tests for StopLossGuardian helper methods.

Covers:
  - _should_send_drawdown_alert / _set_drawdown_cooldown  (issue 4)
  - _is_price_stale                                        (issue 5)

These methods have no external dependencies (no DB, no Redis, no alerting).
We stub the third-party packages that may not be installed locally and
bypass __init__ with __new__ so no real connections are opened.
"""

import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional

# ---------------------------------------------------------------------------
# Stub third-party packages that guardian.py imports at module level.
# Must happen before any stop_loss_guardian imports.
# ---------------------------------------------------------------------------
_STUB_MODS = [
    "twilio", "twilio.rest", "twilio.base", "twilio.base.exceptions",
    "psycopg2", "psycopg2.extras",
    "redis",
    "telegram",
    "pydantic_settings",
]
for _mod in _STUB_MODS:
    if _mod not in sys.modules:
        sys.modules[_mod] = types.ModuleType(_mod)

# Provide attributes that the module code accesses at import time
sys.modules["twilio.rest"].Client = object
sys.modules["twilio.base.exceptions"].TwilioRestException = Exception
sys.modules["psycopg2"].connect = lambda **kw: None
sys.modules["psycopg2.extras"].RealDictCursor = object

# pydantic_settings stub — BaseSettings just needs to be a valid base class
sys.modules["pydantic_settings"].BaseSettings = object

# ---------------------------------------------------------------------------
# Now stub the Settings class itself so guardian.py's `settings` singleton
# has the values we need without reading env vars or .env files.
# ---------------------------------------------------------------------------
import stop_loss_guardian.config as _cfg_module

class _FakeSettings:
    # Risk parameters
    max_risk_per_trade_pct = Decimal("2.0")
    max_position_pct = Decimal("20.0")
    default_stop_loss_pct = Decimal("10.0")
    # Alert thresholds
    drawdown_warning_pct = Decimal("5.0")
    drawdown_critical_pct = Decimal("10.0")
    earnings_warning_days = 5
    # Escalation
    escalation_interval_minutes = 60
    max_telegram_alerts = 2
    max_sms_alerts = 2
    # Monitoring
    check_interval_seconds = 60
    market_hours_only = True
    price_staleness_minutes = 15
    # Connectivity (unused in these tests, but imported at module level)
    db_host = "localhost"
    db_port = 5432
    db_user = "trader"
    db_password = "trader5"
    db_name = "trading_platform"
    redis_host = "localhost"
    redis_port = 6379
    redis_db = 0
    redis_positions_key = "robinhood:positions"
    redis_buying_power_key = "robinhood:buying_power"
    telegram_bot_token = None
    telegram_chat_id = None
    twilio_account_sid = None
    twilio_auth_token = None
    twilio_phone_number = None
    alert_phone_number = ""
    log_level = "INFO"
    # Properties
    twilio_enabled = False
    telegram_enabled = False

_cfg_module.settings = _FakeSettings()

# ---------------------------------------------------------------------------
# Import guardian *after* stubs are in place
# ---------------------------------------------------------------------------
from stop_loss_guardian.guardian import StopLossGuardian
from stop_loss_guardian.models import Position


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_guardian() -> StopLossGuardian:
    """Instantiate StopLossGuardian without triggering __init__ I/O."""
    g = StopLossGuardian.__new__(StopLossGuardian)
    g._critical_drawdown_cooldowns = {}
    # Stub the Redis client so cooldown persistence calls are no-ops in tests.
    g.redis = unittest.mock.MagicMock()
    return g


def _make_position(
    symbol: str = "AAPL",
    entry_price: float = 100.0,
    current_price: float = 95.0,
    price_updated_at: Optional[datetime] = None,
) -> Position:
    return Position(
        symbol=symbol,
        quantity=Decimal("10"),
        entry_price=Decimal(str(entry_price)),
        current_price=Decimal(str(current_price)),
        price_updated_at=price_updated_at,
    )


# ---------------------------------------------------------------------------
# Issue 4: critical drawdown cooldown
# ---------------------------------------------------------------------------

class TestDrawdownCooldown(unittest.TestCase):

    def setUp(self):
        self.guardian = _make_guardian()

    def test_first_alert_always_fires(self):
        self.assertTrue(self.guardian._should_send_drawdown_alert("AAPL"))

    def test_alert_suppressed_immediately_after_sending(self):
        self.guardian._set_drawdown_cooldown("AAPL")
        self.assertFalse(self.guardian._should_send_drawdown_alert("AAPL"))

    def test_alert_fires_again_after_cooldown_expires(self):
        # Backdate the recorded time so the cooldown has elapsed
        past = datetime.now(timezone.utc) - timedelta(hours=2)
        self.guardian._critical_drawdown_cooldowns["AAPL"] = past
        self.assertTrue(self.guardian._should_send_drawdown_alert("AAPL"))

    def test_alert_still_suppressed_before_cooldown_expires(self):
        # Recorded 30 min ago; escalation_interval is 60 min
        recent = datetime.now(timezone.utc) - timedelta(minutes=30)
        self.guardian._critical_drawdown_cooldowns["AAPL"] = recent
        self.assertFalse(self.guardian._should_send_drawdown_alert("AAPL"))

    def test_cooldowns_are_per_symbol(self):
        self.guardian._set_drawdown_cooldown("AAPL")
        self.assertFalse(self.guardian._should_send_drawdown_alert("AAPL"))
        self.assertTrue(self.guardian._should_send_drawdown_alert("WPM"))

    def test_set_drawdown_cooldown_records_now(self):
        before = datetime.now(timezone.utc)
        self.guardian._set_drawdown_cooldown("AAPL")
        after = datetime.now(timezone.utc)
        recorded = self.guardian._critical_drawdown_cooldowns["AAPL"]
        self.assertGreaterEqual(recorded, before)
        self.assertLessEqual(recorded, after)


# ---------------------------------------------------------------------------
# Issue 5: price staleness
# ---------------------------------------------------------------------------

class TestIsPriceStale(unittest.TestCase):

    def setUp(self):
        self.guardian = _make_guardian()

    def test_none_updated_at_is_stale(self):
        pos = _make_position(price_updated_at=None)
        self.assertTrue(self.guardian._is_price_stale(pos))

    def test_fresh_price_is_not_stale(self):
        pos = _make_position(
            price_updated_at=datetime.now(timezone.utc) - timedelta(minutes=1)
        )
        self.assertFalse(self.guardian._is_price_stale(pos))

    def test_price_older_than_threshold_is_stale(self):
        # Default threshold is 15 min; 20 min old → stale
        pos = _make_position(
            price_updated_at=datetime.now(timezone.utc) - timedelta(minutes=20)
        )
        self.assertTrue(self.guardian._is_price_stale(pos))

    def test_price_exactly_at_threshold_is_stale(self):
        # Strictly greater-than: exactly 15 min is stale
        pos = _make_position(
            price_updated_at=datetime.now(timezone.utc) - timedelta(minutes=15)
        )
        self.assertTrue(self.guardian._is_price_stale(pos))

    def test_timezone_naive_recent_datetime_is_not_stale(self):
        # Redis may return a naive datetime; treat as UTC
        naive_recent = datetime.utcnow() - timedelta(minutes=1)
        self.assertIsNone(naive_recent.tzinfo)  # confirm it's naive
        pos = _make_position(price_updated_at=naive_recent)
        self.assertFalse(self.guardian._is_price_stale(pos))

    def test_timezone_naive_old_datetime_is_stale(self):
        naive_old = datetime.utcnow() - timedelta(hours=2)
        pos = _make_position(price_updated_at=naive_old)
        self.assertTrue(self.guardian._is_price_stale(pos))

    def test_fresh_timezone_aware_datetime_is_not_stale(self):
        pos = _make_position(
            price_updated_at=datetime.now(timezone.utc) - timedelta(seconds=30)
        )
        self.assertFalse(self.guardian._is_price_stale(pos))


# ---------------------------------------------------------------------------
# Issue 5 (extended): missing-stop alert still fires when price is stale,
# but stale price / drawdown data must be suppressed from the alert payload.
# ---------------------------------------------------------------------------

class TestMissingStopWithStalePriceGate(unittest.TestCase):
    """
    _check_position must ALWAYS alert for a missing stop loss, even when
    price data is stale.  However, it must NOT forward the stale current_price
    or drawdown_pct to the dispatcher — those fields should be None so the
    trader isn't shown hours-old figures as if they were live.
    """

    def setUp(self):
        self.guardian = _make_guardian()
        # Wire up just enough of the guardian for _check_position to run
        self.tracking = unittest.mock.MagicMock()
        self.tracking.acknowledged = False
        self.tracking.alert_count = 0
        self.tracking.updated_at = datetime.now(timezone.utc)
        self.tracking.alert_escalation_level = "none"
        self.tracking.id = 1

        self.guardian.repo = unittest.mock.MagicMock()
        self.guardian.repo.get_stop_loss_tracking.return_value = self.tracking
        self.guardian.repo.mark_alert_sent = unittest.mock.MagicMock()

        self.guardian.dispatcher = unittest.mock.MagicMock()
        self.guardian.position_sizer = unittest.mock.MagicMock()
        self.guardian.position_sizer.suggest_stop_loss.return_value = Decimal("90.00")

    def _make_stale_no_stop(self):
        """Position WITHOUT a stop loss and stale price data (25 min old)."""
        pos = _make_position(
            symbol="AAPL",
            entry_price=100.0,
            current_price=92.0,
            price_updated_at=datetime.now(timezone.utc) - timedelta(minutes=25),
        )
        # has_stop_loss is a property: True iff stop_loss_price is not None.
        # Leave it as None (default) → no stop loss.
        return pos

    def _make_fresh_no_stop(self):
        """Position WITHOUT a stop loss and fresh price data."""
        return _make_position(
            symbol="AAPL",
            entry_price=100.0,
            current_price=92.0,
            price_updated_at=datetime.now(timezone.utc) - timedelta(minutes=1),
        )

    def _make_stale_with_stop(self):
        """Position WITH a stop loss set and stale price data."""
        from stop_loss_guardian.models import Position
        pos = Position(
            symbol="AAPL",
            quantity=Decimal("10"),
            entry_price=Decimal("100.0"),
            current_price=Decimal("92.0"),
            stop_loss_price=Decimal("85.0"),  # stop IS set
            price_updated_at=datetime.now(timezone.utc) - timedelta(minutes=25),
        )
        return pos

    def test_alert_fires_even_when_price_is_stale(self):
        """The missing-stop alert must fire regardless of price freshness."""
        pos = self._make_stale_no_stop()
        self.guardian._check_position(pos)
        self.guardian.dispatcher.send_missing_stop_loss_alert.assert_called_once()

    def test_stale_price_suppressed_in_alert_payload(self):
        """When price is stale, current_price and drawdown_pct must be None in the alert."""
        pos = self._make_stale_no_stop()
        self.guardian._check_position(pos)

        call_kwargs = self.guardian.dispatcher.send_missing_stop_loss_alert.call_args[1]
        self.assertIsNone(
            call_kwargs.get("current_price"),
            "current_price should be None when price data is stale",
        )
        self.assertIsNone(
            call_kwargs.get("drawdown_pct"),
            "drawdown_pct should be None when price data is stale",
        )

    def test_fresh_price_included_in_alert_payload(self):
        """When price is fresh, current_price must be forwarded (not suppressed)."""
        pos = self._make_fresh_no_stop()
        self.guardian._check_position(pos)

        call_kwargs = self.guardian.dispatcher.send_missing_stop_loss_alert.call_args[1]
        self.assertIsNotNone(
            call_kwargs.get("current_price"),
            "current_price should be present when price data is fresh",
        )

    def test_drawdown_check_skipped_when_price_stale_and_stop_exists(self):
        """
        When a stop IS set but price is stale, no alert should fire.
        The drawdown/trigger checks require fresh price data.
        """
        pos = self._make_stale_with_stop()
        self.guardian._check_position(pos)

        self.guardian.dispatcher.send_drawdown_alert.assert_not_called()
        self.guardian.dispatcher.send_missing_stop_loss_alert.assert_not_called()


import unittest.mock  # noqa: E402 — needed by the class above


if __name__ == "__main__":
    unittest.main()
