"""Shared test fixtures for stop-loss-guardian.

Sets up module stubs and provides reusable fixtures so that individual
test files don't need to repeat the import-time patching boilerplate.
"""

import sys
import types
import unittest.mock
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional

import pytest

# ---------------------------------------------------------------------------
# Stub third-party packages that guardian code imports at module level.
# Must happen before any stop_loss_guardian imports.
# ---------------------------------------------------------------------------
_STUB_MODS = [
    "twilio", "twilio.rest", "twilio.base", "twilio.base.exceptions",
    "psycopg2", "psycopg2.extras", "psycopg2.pool",
    "redis",
    "httpx",
    "telegram",
    "pydantic_settings",
]
for _mod in _STUB_MODS:
    if _mod not in sys.modules:
        sys.modules[_mod] = types.ModuleType(_mod)

# Provide attributes that module code accesses at import time
sys.modules["twilio.rest"].Client = object
sys.modules["twilio.base.exceptions"].TwilioRestException = Exception
sys.modules["psycopg2"].connect = lambda **kw: None
sys.modules["psycopg2.extras"].RealDictCursor = object
sys.modules["psycopg2.pool"].SimpleConnectionPool = unittest.mock.MagicMock

# pydantic_settings stub
sys.modules["pydantic_settings"].BaseSettings = object

# ---------------------------------------------------------------------------
# Stub Settings singleton
# ---------------------------------------------------------------------------
import stop_loss_guardian.config as _cfg_module  # noqa: E402


class FakeSettings:
    """Minimal Settings stub with all fields the codebase accesses."""
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
    # Database
    db_host = "localhost"
    db_port = 5432
    db_user = "trader"
    db_password = "trader5"
    db_name = "trading_platform"
    # Redis
    redis_host = "localhost"
    redis_port = 6379
    redis_db = 0
    redis_positions_key = "robinhood:positions"
    redis_buying_power_key = "robinhood:buying_power"
    # Alerting
    telegram_bot_token = None
    telegram_chat_id = None
    twilio_account_sid = None
    twilio_auth_token = None
    twilio_phone_number = None
    alert_phone_number = ""
    log_level = "INFO"
    # Portfolio monitoring
    portfolio_monitor_enabled = True
    portfolio_max_stops_per_day = 3
    portfolio_daily_loss_halt_pct = 0.06
    portfolio_heat_warn_pct = 0.08
    portfolio_heat_halt_pct = 0.12
    # Properties
    twilio_enabled = False
    telegram_enabled = False


_cfg_module.settings = FakeSettings()

# ---------------------------------------------------------------------------
# Now import the modules under test
# ---------------------------------------------------------------------------
from stop_loss_guardian.guardian import StopLossGuardian  # noqa: E402
from stop_loss_guardian.models import (  # noqa: E402
    Position, StopLossRecord, Alert, AlertType, Severity, AlertChannel,
    PositionSizeResult, AccountState,
)
from stop_loss_guardian.alerting.dispatcher import AlertDispatcher  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def fake_settings():
    """Return the active FakeSettings instance."""
    return _cfg_module.settings


@pytest.fixture
def mock_repo():
    """Fully-mocked Repository."""
    repo = unittest.mock.MagicMock()
    repo.conn = unittest.mock.MagicMock()
    repo.conn.closed = False
    return repo


@pytest.fixture
def mock_redis():
    """Fully-mocked RedisClient."""
    r = unittest.mock.MagicMock()
    r.get_drawdown_cooldowns.return_value = {}
    r.get_earnings_date.return_value = None
    r.get_stop_order.return_value = None
    r.get_all_stop_orders.return_value = {}
    return r


@pytest.fixture
def mock_telegram():
    """Fully-mocked TelegramClient."""
    tg = unittest.mock.MagicMock()
    tg.send_alert.return_value = True
    tg.send_message_sync.return_value = True
    return tg


@pytest.fixture
def mock_twilio():
    """Fully-mocked TwilioClient."""
    tw = unittest.mock.MagicMock()
    tw.enabled = False
    tw.send_sms.return_value = "SM_test_sid"
    tw.make_call.return_value = "CA_test_sid"
    return tw


@pytest.fixture
def mock_dispatcher(mock_repo, mock_twilio, mock_telegram):
    """AlertDispatcher with all dependencies mocked."""
    d = AlertDispatcher(
        repository=mock_repo,
        twilio_client=mock_twilio,
        telegram_client=mock_telegram,
    )
    return d


@pytest.fixture
def guardian(mock_repo, mock_redis, mock_telegram, mock_twilio):
    """StopLossGuardian with all I/O mocked, ready for unit tests."""
    g = StopLossGuardian.__new__(StopLossGuardian)
    g.repo = mock_repo
    g.redis = mock_redis
    g.telegram = mock_telegram
    g.twilio = mock_twilio
    g.position_sizer = unittest.mock.MagicMock()
    g.position_sizer.suggest_stop_loss.return_value = Decimal("90.00")
    g.dispatcher = unittest.mock.MagicMock()
    g.dispatcher.send_missing_stop_loss_alert.return_value = True
    g.dispatcher.send_drawdown_alert.return_value = True
    g.portfolio_monitor = None
    g._running = False
    g._critical_drawdown_cooldowns = {}
    g._earnings_alert_cooldowns = {}
    return g


def make_position(
    symbol: str = "AAPL",
    quantity: Decimal = Decimal("10"),
    entry_price: Decimal = Decimal("100.00"),
    current_price: Optional[Decimal] = Decimal("95.00"),
    stop_loss_price: Optional[Decimal] = None,
    stop_loss_type: Optional[str] = None,
    price_updated_at: Optional[datetime] = None,
    position_id: Optional[int] = 1,
    entry_date: Optional[datetime] = None,
) -> Position:
    """Create a Position with sensible defaults for tests."""
    return Position(
        symbol=symbol,
        quantity=quantity,
        entry_price=entry_price,
        current_price=current_price,
        stop_loss_price=stop_loss_price,
        stop_loss_type=stop_loss_type,
        price_updated_at=price_updated_at or datetime.now(timezone.utc) - timedelta(minutes=1),
        position_id=position_id,
        entry_date=entry_date,
    )


def make_tracking(
    symbol: str = "AAPL",
    alert_count: int = 0,
    alert_escalation_level: str = "none",
    acknowledged: bool = False,
    stop_loss_price: Optional[Decimal] = None,
    next_earnings_date: Optional[datetime] = None,
    tracking_id: int = 1,
) -> StopLossRecord:
    """Create a StopLossRecord with sensible defaults for tests."""
    now = datetime.now(timezone.utc)
    return StopLossRecord(
        id=tracking_id,
        symbol=symbol,
        position_id=1,
        entry_price=Decimal("100.00"),
        quantity=Decimal("10"),
        stop_loss_price=stop_loss_price,
        stop_loss_type=None,
        stop_loss_pct=None,
        current_price=Decimal("95.00"),
        current_drawdown_pct=Decimal("-5.00"),
        missing_stop_alert_sent=alert_count > 0,
        alert_count=alert_count,
        alert_escalation_level=alert_escalation_level,
        acknowledged=acknowledged,
        next_earnings_date=next_earnings_date,
        created_at=now,
        updated_at=now,
    )
