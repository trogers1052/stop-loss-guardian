"""Alert dispatcher with escalation logic."""

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from ..config import settings
from ..models import Alert, AlertChannel, Severity
from ..db.repository import Repository
from .twilio_client import TwilioClient
from .telegram_client import TelegramClient

logger = logging.getLogger(__name__)


class AlertDispatcher:
    """Dispatches alerts with escalation from Telegram → SMS → Phone Call."""

    def __init__(
        self,
        repository: Repository,
        twilio_client: TwilioClient,
        telegram_client: TelegramClient,
    ):
        self.repo = repository
        self.twilio = twilio_client
        self.telegram = telegram_client

    def dispatch(
        self,
        alert: Alert,
        current_escalation_level: int = 0,
        last_alert_time: Optional[datetime] = None,
        stop_loss_tracking_id: Optional[int] = None,
    ) -> bool:
        """Dispatch an alert with appropriate escalation.

        Escalation levels:
            0 - Telegram (routine, can be ignored)
            1 - SMS (urgent, should wake you up)
            2 - Phone call (critical, MUST wake you up)

        Args:
            alert: Alert to send
            current_escalation_level: Current escalation level (0-2)
            last_alert_time: When the last alert was sent
            stop_loss_tracking_id: ID for linking to database record

        Returns:
            True if alert was sent successfully
        """
        # Determine escalation level based on severity and history
        escalation_level = self._determine_escalation(
            alert.severity,
            current_escalation_level,
            last_alert_time,
        )

        # Get the channel for this escalation level
        channel = self._get_channel(escalation_level)
        message = alert.format_message()

        # Send the alert
        success = False
        twilio_sid = None

        if channel == AlertChannel.TELEGRAM:
            success = self.telegram.send_alert(message)
        elif channel == AlertChannel.SMS:
            result = self.twilio.send_sms(message)
            success = result is not None
            twilio_sid = result
        elif channel == AlertChannel.PHONE_CALL:
            # For phone calls, also send SMS as backup
            sms_result = self.twilio.send_sms(message)
            call_result = self.twilio.make_call(message[:200])
            success = sms_result is not None or call_result is not None
            twilio_sid = call_result or sms_result

        # Log the alert to database
        if success:
            try:
                self.repo.log_urgent_alert(
                    alert=alert,
                    channel=channel,
                    escalation_level=escalation_level,
                    stop_loss_tracking_id=stop_loss_tracking_id,
                    twilio_sid=twilio_sid,
                )
            except Exception as e:
                logger.error(f"Failed to log alert: {e}")

        log_level = logging.INFO if success else logging.ERROR
        logger.log(
            log_level,
            f"Alert {'sent' if success else 'FAILED'}: {alert.alert_type.value} for {alert.symbol} "
            f"via {channel.value} (level {escalation_level})"
        )

        return success

    def _determine_escalation(
        self,
        severity: Severity,
        current_level: int,
        last_alert_time: Optional[datetime],
    ) -> int:
        """Determine the appropriate escalation level.

        Logic:
        - CRITICAL severity always goes to phone call (level 2)
        - URGENT severity starts at SMS (level 1)
        - If enough time has passed and alert wasn't acknowledged, escalate
        - Never escalate beyond level 2
        """
        # Critical alerts always get the highest level
        if severity == Severity.CRITICAL:
            return 2

        # Urgent alerts start at SMS
        if severity == Severity.URGENT and current_level < 1:
            return 1

        # Check if we should escalate based on time
        if last_alert_time:
            time_since_last = datetime.now(timezone.utc) - last_alert_time
            escalation_interval = timedelta(minutes=settings.escalation_interval_minutes)

            if time_since_last >= escalation_interval:
                # Time to escalate
                new_level = min(current_level + 1, 2)

                # But respect max alerts per channel
                if current_level == 0 and new_level == 1:
                    # Moving from Telegram to SMS
                    logger.info(f"Escalating from Telegram to SMS (no response in {time_since_last})")
                elif current_level == 1 and new_level == 2:
                    # Moving from SMS to Phone
                    logger.info(f"Escalating from SMS to Phone call (no response in {time_since_last})")

                return new_level

        # Default based on severity
        if severity == Severity.WARNING:
            return max(current_level, 0)  # At least Telegram
        elif severity == Severity.URGENT:
            return max(current_level, 1)  # At least SMS
        else:
            return current_level

    def _get_channel(self, escalation_level: int) -> AlertChannel:
        """Get the alert channel for an escalation level."""
        if escalation_level >= 2:
            return AlertChannel.PHONE_CALL
        elif escalation_level == 1:
            return AlertChannel.SMS
        else:
            return AlertChannel.TELEGRAM

    def send_missing_stop_loss_alert(
        self,
        symbol: str,
        entry_price: float,
        current_price: Optional[float],
        drawdown_pct: Optional[float],
        suggested_stop: Optional[float],
        escalation_level: int,
        last_alert_time: Optional[datetime],
        stop_loss_tracking_id: Optional[int],
    ) -> bool:
        """Send alert for a position missing stop loss."""
        from ..models import AlertType

        # Determine severity based on drawdown
        if drawdown_pct and drawdown_pct <= -float(settings.drawdown_critical_pct):
            severity = Severity.CRITICAL
        elif drawdown_pct and drawdown_pct <= -float(settings.drawdown_warning_pct):
            severity = Severity.URGENT
        else:
            severity = Severity.WARNING

        message = f"Position {symbol} has NO STOP LOSS set!"
        if drawdown_pct:
            message += f" Currently down {abs(drawdown_pct):.1f}%."
        message += " Set a stop loss immediately to protect your capital."

        alert = Alert(
            alert_type=AlertType.MISSING_STOP_LOSS,
            severity=severity,
            symbol=symbol,
            message=message,
            details={
                "entry_price": entry_price,
                "current_price": current_price,
                "drawdown_pct": drawdown_pct,
            },
            suggested_stop_price=suggested_stop,
            suggested_action="Set stop loss at suggested level or below",
        )

        return self.dispatch(
            alert,
            current_escalation_level=escalation_level,
            last_alert_time=last_alert_time,
            stop_loss_tracking_id=stop_loss_tracking_id,
        )

    def send_drawdown_alert(
        self,
        symbol: str,
        entry_price: float,
        current_price: float,
        drawdown_pct: float,
        stop_loss_price: Optional[float],
        stop_loss_tracking_id: Optional[int],
    ) -> bool:
        """Send alert for significant drawdown."""
        from ..models import AlertType

        is_critical = drawdown_pct <= -float(settings.drawdown_critical_pct)
        severity = Severity.CRITICAL if is_critical else Severity.WARNING

        message = f"Position {symbol} is down {abs(drawdown_pct):.1f}%!"
        if stop_loss_price:
            message += f" Stop loss at ${stop_loss_price:.2f}."
        else:
            message += " NO STOP LOSS SET!"

        alert = Alert(
            alert_type=AlertType.DRAWDOWN_CRITICAL if is_critical else AlertType.DRAWDOWN_WARNING,
            severity=severity,
            symbol=symbol,
            message=message,
            details={
                "entry_price": entry_price,
                "current_price": current_price,
                "drawdown_pct": drawdown_pct,
                "stop_loss_price": stop_loss_price,
            },
            suggested_action="Review position and consider taking action",
        )

        return self.dispatch(
            alert,
            current_escalation_level=1 if is_critical else 0,  # Critical starts at SMS
            stop_loss_tracking_id=stop_loss_tracking_id,
        )
