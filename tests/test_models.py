"""Tests for data models — Position properties, Alert formatting, PositionSizeResult."""

import pytest
from datetime import datetime, timezone
from decimal import Decimal

from stop_loss_guardian.models import (
    Position, Alert, AlertType, Severity, AlertChannel,
    PositionSizeResult, AccountState, StopLossType,
)


# ---------------------------------------------------------------------------
# Position
# ---------------------------------------------------------------------------

class TestPositionHasStopLoss:
    def test_no_stop_loss_price(self):
        p = Position(symbol="X", quantity=Decimal("1"), entry_price=Decimal("50"))
        assert p.has_stop_loss is False

    def test_has_stop_loss_price(self):
        p = Position(
            symbol="X", quantity=Decimal("1"), entry_price=Decimal("50"),
            stop_loss_price=Decimal("45"),
        )
        assert p.has_stop_loss is True

    def test_stop_loss_zero_is_truthy(self):
        """Decimal('0') is not None — stop at $0 technically 'exists'."""
        p = Position(
            symbol="X", quantity=Decimal("1"), entry_price=Decimal("50"),
            stop_loss_price=Decimal("0"),
        )
        assert p.has_stop_loss is True


class TestPositionDrawdown:
    def test_positive_gain(self):
        p = Position(
            symbol="X", quantity=Decimal("1"),
            entry_price=Decimal("100"), current_price=Decimal("110"),
        )
        assert p.current_drawdown_pct == Decimal("10")

    def test_negative_loss(self):
        p = Position(
            symbol="X", quantity=Decimal("1"),
            entry_price=Decimal("100"), current_price=Decimal("80"),
        )
        assert p.current_drawdown_pct == Decimal("-20")

    def test_no_current_price(self):
        p = Position(symbol="X", quantity=Decimal("1"), entry_price=Decimal("100"))
        assert p.current_drawdown_pct is None

    def test_breakeven(self):
        p = Position(
            symbol="X", quantity=Decimal("1"),
            entry_price=Decimal("100"), current_price=Decimal("100"),
        )
        assert p.current_drawdown_pct == Decimal("0")


class TestPositionStopTriggered:
    def test_price_below_stop(self):
        p = Position(
            symbol="X", quantity=Decimal("1"), entry_price=Decimal("100"),
            current_price=Decimal("44"), stop_loss_price=Decimal("45"),
        )
        assert p.stop_loss_triggered is True

    def test_price_equals_stop(self):
        p = Position(
            symbol="X", quantity=Decimal("1"), entry_price=Decimal("100"),
            current_price=Decimal("45"), stop_loss_price=Decimal("45"),
        )
        assert p.stop_loss_triggered is True

    def test_price_above_stop(self):
        p = Position(
            symbol="X", quantity=Decimal("1"), entry_price=Decimal("100"),
            current_price=Decimal("50"), stop_loss_price=Decimal("45"),
        )
        assert p.stop_loss_triggered is False

    def test_no_stop_set(self):
        p = Position(
            symbol="X", quantity=Decimal("1"), entry_price=Decimal("100"),
            current_price=Decimal("44"),
        )
        assert p.stop_loss_triggered is False

    def test_no_current_price(self):
        p = Position(
            symbol="X", quantity=Decimal("1"), entry_price=Decimal("100"),
            stop_loss_price=Decimal("45"),
        )
        assert p.stop_loss_triggered is False


class TestPositionValue:
    def test_normal(self):
        p = Position(
            symbol="X", quantity=Decimal("10"), entry_price=Decimal("100"),
            current_price=Decimal("50"),
        )
        assert p.position_value == Decimal("500")

    def test_no_current_price(self):
        p = Position(symbol="X", quantity=Decimal("10"), entry_price=Decimal("100"))
        assert p.position_value is None


class TestPositionToDict:
    def test_includes_all_keys(self):
        p = Position(
            symbol="AAPL", quantity=Decimal("5"), entry_price=Decimal("150"),
            current_price=Decimal("145"), stop_loss_price=Decimal("140"),
            stop_loss_type=StopLossType.MANUAL,
        )
        d = p.to_dict()
        assert d["symbol"] == "AAPL"
        assert d["has_stop_loss"] is True
        assert d["stop_loss_type"] == "manual"
        assert d["current_drawdown_pct"] is not None

    def test_none_fields_serialized(self):
        p = Position(symbol="X", quantity=Decimal("1"), entry_price=Decimal("50"))
        d = p.to_dict()
        assert d["current_price"] is None
        assert d["stop_loss_price"] is None
        assert d["has_stop_loss"] is False


# ---------------------------------------------------------------------------
# Alert
# ---------------------------------------------------------------------------

class TestAlertFormatMessage:
    def test_critical_missing_stop(self):
        a = Alert(
            alert_type=AlertType.MISSING_STOP_LOSS,
            severity=Severity.CRITICAL,
            symbol="AAPL",
            message="No stop loss!",
            details={"entry_price": 150.0, "current_price": 120.0, "drawdown_pct": -20.0},
            suggested_stop_price=Decimal("130.00"),
            suggested_action="Set stop now",
        )
        msg = a.format_message()
        assert "AAPL" in msg
        assert "CRITICAL" in msg
        assert "Entry: $150" in msg
        assert "Current: $120" in msg
        assert "Drawdown:" in msg
        assert "Suggested Stop: $130" in msg
        assert "Set stop now" in msg

    def test_info_severity_emoji(self):
        a = Alert(
            alert_type=AlertType.EARNINGS_WARNING,
            severity=Severity.INFO,
            symbol="X",
            message="Earnings soon",
        )
        # Should not crash; emoji may vary
        msg = a.format_message()
        assert "X" in msg

    def test_no_details(self):
        a = Alert(
            alert_type=AlertType.DRAWDOWN_WARNING,
            severity=Severity.WARNING,
            symbol="Z",
            message="Watch out",
        )
        msg = a.format_message()
        assert "Entry:" not in msg  # no entry_price in details


# ---------------------------------------------------------------------------
# AccountState
# ---------------------------------------------------------------------------

class TestAccountState:
    def test_available_for_new_position(self):
        state = AccountState(
            buying_power=Decimal("500"),
            cash=Decimal("300"),
            total_equity=Decimal("888"),
            updated_at=datetime.now(timezone.utc),
        )
        assert state.available_for_new_position == Decimal("500")


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class TestEnums:
    def test_alert_types(self):
        assert AlertType.MISSING_STOP_LOSS.value == "missing_stop_loss"
        assert AlertType.DRAWDOWN_CRITICAL.value == "drawdown_critical"

    def test_severity_ordering(self):
        # Just ensure they all exist and have expected values
        assert Severity.INFO.value == "info"
        assert Severity.WARNING.value == "warning"
        assert Severity.URGENT.value == "urgent"
        assert Severity.CRITICAL.value == "critical"

    def test_alert_channels(self):
        assert AlertChannel.TELEGRAM.value == "telegram"
        assert AlertChannel.SMS.value == "sms"
        assert AlertChannel.PHONE_CALL.value == "phone_call"
