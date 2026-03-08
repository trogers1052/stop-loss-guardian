"""Tests for AlertDispatcher — escalation logic, channel routing, and dispatch flow."""

import pytest
import unittest.mock
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from stop_loss_guardian.alerting.dispatcher import AlertDispatcher
from stop_loss_guardian.models import Alert, AlertType, Severity, AlertChannel


# ---------------------------------------------------------------------------
# _get_channel
# ---------------------------------------------------------------------------

class TestGetChannel:
    def test_level_0_is_telegram(self, mock_dispatcher):
        assert mock_dispatcher._get_channel(0) == AlertChannel.TELEGRAM

    def test_level_1_is_sms(self, mock_dispatcher):
        assert mock_dispatcher._get_channel(1) == AlertChannel.SMS

    def test_level_2_is_phone_call(self, mock_dispatcher):
        assert mock_dispatcher._get_channel(2) == AlertChannel.PHONE_CALL

    def test_level_above_2_is_phone_call(self, mock_dispatcher):
        assert mock_dispatcher._get_channel(5) == AlertChannel.PHONE_CALL


# ---------------------------------------------------------------------------
# _determine_escalation
# ---------------------------------------------------------------------------

class TestDetermineEscalation:
    def test_critical_always_phone(self, mock_dispatcher):
        level = mock_dispatcher._determine_escalation(
            Severity.CRITICAL, current_level=0, last_alert_time=None,
        )
        assert level == 2

    def test_urgent_starts_at_sms(self, mock_dispatcher):
        level = mock_dispatcher._determine_escalation(
            Severity.URGENT, current_level=0, last_alert_time=None,
        )
        assert level == 1

    def test_urgent_stays_at_sms_if_already_there(self, mock_dispatcher):
        level = mock_dispatcher._determine_escalation(
            Severity.URGENT, current_level=1, last_alert_time=None,
        )
        assert level == 1

    def test_warning_defaults_to_telegram(self, mock_dispatcher):
        level = mock_dispatcher._determine_escalation(
            Severity.WARNING, current_level=0, last_alert_time=None,
        )
        assert level == 0

    def test_escalation_after_interval(self, mock_dispatcher):
        """If enough time has passed, escalate one level."""
        old = datetime.now(timezone.utc) - timedelta(hours=2)
        level = mock_dispatcher._determine_escalation(
            Severity.WARNING, current_level=0, last_alert_time=old,
        )
        assert level == 1

    def test_no_escalation_before_interval(self, mock_dispatcher):
        """Within escalation interval, stay at current level."""
        recent = datetime.now(timezone.utc) - timedelta(minutes=30)
        level = mock_dispatcher._determine_escalation(
            Severity.WARNING, current_level=0, last_alert_time=recent,
        )
        assert level == 0

    def test_never_exceeds_level_2(self, mock_dispatcher):
        old = datetime.now(timezone.utc) - timedelta(hours=5)
        level = mock_dispatcher._determine_escalation(
            Severity.WARNING, current_level=2, last_alert_time=old,
        )
        assert level == 2


# ---------------------------------------------------------------------------
# dispatch
# ---------------------------------------------------------------------------

class TestDispatch:
    def _make_alert(self, severity=Severity.WARNING, symbol="AAPL"):
        return Alert(
            alert_type=AlertType.MISSING_STOP_LOSS,
            severity=severity,
            symbol=symbol,
            message="Test alert",
            details={"entry_price": 100.0},
        )

    def test_telegram_dispatch(self, mock_dispatcher):
        alert = self._make_alert()
        result = mock_dispatcher.dispatch(alert, current_escalation_level=0)
        assert result is True
        mock_dispatcher.telegram.send_alert.assert_called_once()

    def test_sms_dispatch(self, mock_dispatcher):
        alert = self._make_alert(severity=Severity.URGENT)
        result = mock_dispatcher.dispatch(alert, current_escalation_level=1)
        assert result is True
        mock_dispatcher.twilio.send_sms.assert_called_once()

    def test_phone_call_dispatch(self, mock_dispatcher):
        alert = self._make_alert(severity=Severity.CRITICAL)
        result = mock_dispatcher.dispatch(alert, current_escalation_level=2)
        # CRITICAL → phone call = SMS + call
        mock_dispatcher.twilio.send_sms.assert_called_once()
        mock_dispatcher.twilio.make_call.assert_called_once()
        assert result is True

    def test_failed_telegram_returns_false(self, mock_dispatcher):
        mock_dispatcher.telegram.send_alert.return_value = False
        alert = self._make_alert()
        result = mock_dispatcher.dispatch(alert, current_escalation_level=0)
        assert result is False

    def test_failed_sms_returns_false(self, mock_dispatcher):
        mock_dispatcher.twilio.send_sms.return_value = None
        alert = self._make_alert(severity=Severity.URGENT)
        result = mock_dispatcher.dispatch(alert, current_escalation_level=1)
        assert result is False

    def test_success_logs_to_db(self, mock_dispatcher):
        alert = self._make_alert()
        mock_dispatcher.dispatch(alert, stop_loss_tracking_id=42)
        mock_dispatcher.repo.log_urgent_alert.assert_called_once()
        call_kwargs = mock_dispatcher.repo.log_urgent_alert.call_args
        assert call_kwargs[1]["stop_loss_tracking_id"] == 42

    def test_failure_does_not_log_to_db(self, mock_dispatcher):
        mock_dispatcher.telegram.send_alert.return_value = False
        alert = self._make_alert()
        mock_dispatcher.dispatch(alert)
        mock_dispatcher.repo.log_urgent_alert.assert_not_called()

    def test_db_log_failure_does_not_crash(self, mock_dispatcher):
        """If logging to DB fails, dispatch should still return True."""
        mock_dispatcher.repo.log_urgent_alert.side_effect = Exception("DB down")
        alert = self._make_alert()
        result = mock_dispatcher.dispatch(alert)
        assert result is True


# ---------------------------------------------------------------------------
# send_missing_stop_loss_alert
# ---------------------------------------------------------------------------

class TestSendMissingStopLossAlert:
    def test_no_drawdown_is_warning(self, mock_dispatcher):
        mock_dispatcher.dispatch = unittest.mock.MagicMock(return_value=True)
        mock_dispatcher.send_missing_stop_loss_alert(
            symbol="AAPL", entry_price=100.0, current_price=95.0,
            drawdown_pct=None, suggested_stop=90.0,
            escalation_level=0, last_alert_time=None, stop_loss_tracking_id=1,
        )
        alert = mock_dispatcher.dispatch.call_args[0][0]
        assert alert.severity == Severity.WARNING

    def test_critical_drawdown_is_critical(self, mock_dispatcher):
        mock_dispatcher.dispatch = unittest.mock.MagicMock(return_value=True)
        mock_dispatcher.send_missing_stop_loss_alert(
            symbol="AAPL", entry_price=100.0, current_price=88.0,
            drawdown_pct=-12.0, suggested_stop=90.0,
            escalation_level=0, last_alert_time=None, stop_loss_tracking_id=1,
        )
        alert = mock_dispatcher.dispatch.call_args[0][0]
        assert alert.severity == Severity.CRITICAL

    def test_warning_drawdown_is_urgent(self, mock_dispatcher):
        mock_dispatcher.dispatch = unittest.mock.MagicMock(return_value=True)
        mock_dispatcher.send_missing_stop_loss_alert(
            symbol="AAPL", entry_price=100.0, current_price=93.0,
            drawdown_pct=-7.0, suggested_stop=90.0,
            escalation_level=0, last_alert_time=None, stop_loss_tracking_id=1,
        )
        alert = mock_dispatcher.dispatch.call_args[0][0]
        assert alert.severity == Severity.URGENT


# ---------------------------------------------------------------------------
# send_drawdown_alert
# ---------------------------------------------------------------------------

class TestSendDrawdownAlert:
    def test_critical_drawdown(self, mock_dispatcher):
        mock_dispatcher.dispatch = unittest.mock.MagicMock(return_value=True)
        mock_dispatcher.send_drawdown_alert(
            symbol="AAPL", entry_price=100.0, current_price=85.0,
            drawdown_pct=-15.0, stop_loss_price=80.0, stop_loss_tracking_id=1,
        )
        alert = mock_dispatcher.dispatch.call_args[0][0]
        assert alert.severity == Severity.CRITICAL
        assert alert.alert_type == AlertType.DRAWDOWN_CRITICAL

    def test_warning_drawdown(self, mock_dispatcher):
        mock_dispatcher.dispatch = unittest.mock.MagicMock(return_value=True)
        mock_dispatcher.send_drawdown_alert(
            symbol="AAPL", entry_price=100.0, current_price=93.0,
            drawdown_pct=-7.0, stop_loss_price=90.0, stop_loss_tracking_id=1,
        )
        alert = mock_dispatcher.dispatch.call_args[0][0]
        assert alert.severity == Severity.WARNING
        assert alert.alert_type == AlertType.DRAWDOWN_WARNING

    def test_no_stop_loss_message(self, mock_dispatcher):
        mock_dispatcher.dispatch = unittest.mock.MagicMock(return_value=True)
        mock_dispatcher.send_drawdown_alert(
            symbol="AAPL", entry_price=100.0, current_price=85.0,
            drawdown_pct=-15.0, stop_loss_price=None, stop_loss_tracking_id=1,
        )
        alert = mock_dispatcher.dispatch.call_args[0][0]
        assert "NO STOP LOSS" in alert.message
