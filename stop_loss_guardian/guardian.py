"""Stop Loss Guardian - Main monitoring logic.

The platform's #1 job is keeping losses small.
This service monitors all open positions and SCREAMS if any position lacks a stop loss.
"""

import logging
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import List, Optional

from .config import settings
from .models import Position, Alert, AlertType, Severity
from .db.repository import Repository
from .redis_client import RedisClient
from .alerting.dispatcher import AlertDispatcher
from .alerting.twilio_client import TwilioClient
from .alerting.telegram_client import TelegramClient
from .position_sizer import PositionSizer

logger = logging.getLogger(__name__)


class StopLossGuardian:
    """Monitors positions for missing stop losses and alerts with escalation.

    The MOH Lesson: A 34.5% loss requires a 52.7% gain to recover.
    A 10% loss only requires 11.1%.

    This service ensures you NEVER hold a position without a stop loss.
    """

    def __init__(self):
        self.repo = Repository()
        self.redis = RedisClient()
        self.twilio = TwilioClient()
        self.telegram = TelegramClient()
        self.dispatcher: Optional[AlertDispatcher] = None
        self.position_sizer = PositionSizer()
        self._running = False

    def start(self):
        """Initialize connections and start monitoring."""
        logger.info("Starting Stop Loss Guardian")

        # Connect to services
        self.repo.connect()
        self.redis.connect()

        if settings.twilio_enabled:
            self.twilio.connect()

        self.dispatcher = AlertDispatcher(
            repository=self.repo,
            twilio_client=self.twilio,
            telegram_client=self.telegram,
        )

        self._running = True
        self._run_monitoring_loop()

    def stop(self):
        """Stop monitoring and cleanup."""
        logger.info("Stopping Stop Loss Guardian")
        self._running = False
        self.repo.close()
        self.redis.close()

    def _run_monitoring_loop(self):
        """Main monitoring loop."""
        logger.info(f"Starting monitoring loop (interval: {settings.check_interval_seconds}s)")

        while self._running:
            try:
                # Ensure database connection is healthy
                self.repo.ensure_connected()
                self._check_all_positions()
            except Exception as e:
                logger.error(f"Error in monitoring loop: {e}", exc_info=True)

            # Sleep until next check
            time.sleep(settings.check_interval_seconds)

    def _check_all_positions(self):
        """Check all open positions for stop loss compliance."""
        logger.debug("Checking all positions...")

        # Cleanup closed positions from tracking
        self.repo.cleanup_closed_positions()

        # Get open positions from journal
        open_positions = self.repo.get_open_positions()

        if not open_positions:
            logger.debug("No open positions to monitor")
            return

        # Enrich with current prices from Redis
        positions = self._enrich_positions(open_positions)

        # Sync to stop_loss_tracking table
        self._sync_positions_to_tracking(positions)

        # Check each position
        for position in positions:
            self._check_position(position)

        logger.info(f"Checked {len(positions)} positions")

    def _enrich_positions(self, positions: List[Position]) -> List[Position]:
        """Enrich positions with current market data from Redis."""
        enriched = []

        for pos in positions:
            redis_data = self.redis.enrich_position(
                symbol=pos.symbol,
                entry_price=pos.entry_price,
                quantity=pos.quantity,
            )

            pos.current_price = redis_data.get("current_price")
            pos.current_equity = redis_data.get("current_equity")
            pos.percent_change = redis_data.get("percent_change")
            pos.equity_change = redis_data.get("equity_change")

            if redis_data.get("updated_at"):
                try:
                    pos.price_updated_at = datetime.fromisoformat(redis_data["updated_at"])
                except:
                    pass

            # Get stop loss info from tracking table
            tracking = self.repo.get_stop_loss_tracking(pos.symbol)
            if tracking:
                pos.stop_loss_price = tracking.stop_loss_price
                pos.stop_loss_type = tracking.stop_loss_type
                pos.stop_loss_pct = tracking.stop_loss_pct

            enriched.append(pos)

        return enriched

    def _sync_positions_to_tracking(self, positions: List[Position]):
        """Ensure all positions are in the stop_loss_tracking table."""
        for pos in positions:
            self.repo.upsert_stop_loss_tracking(
                symbol=pos.symbol,
                position_id=pos.position_id,
                entry_price=pos.entry_price,
                quantity=pos.quantity,
                current_price=pos.current_price,
                current_drawdown_pct=pos.current_drawdown_pct,
            )

    def _check_position(self, position: Position):
        """Check a single position for stop loss compliance."""

        # Get tracking record for escalation state
        tracking = self.repo.get_stop_loss_tracking(position.symbol)
        if not tracking:
            return

        # Skip if acknowledged
        if tracking.acknowledged:
            return

        # Check 1: Missing stop loss
        if not position.has_stop_loss:
            self._handle_missing_stop_loss(position, tracking)
            return

        # Check 2: Significant drawdown
        if position.current_drawdown_pct is not None:
            drawdown = float(position.current_drawdown_pct)

            if drawdown <= -float(settings.drawdown_critical_pct):
                self._handle_critical_drawdown(position, tracking)
            elif drawdown <= -float(settings.drawdown_warning_pct):
                self._handle_warning_drawdown(position, tracking)

        # Check 3: Stop loss triggered (price below stop)
        if position.stop_loss_triggered:
            self._handle_stop_triggered(position, tracking)

    def _handle_missing_stop_loss(self, position: Position, tracking):
        """Handle a position with no stop loss configured."""

        # Determine if we should send an alert (escalation logic)
        should_alert = self._should_send_alert(tracking)

        if not should_alert:
            return

        # Calculate suggested stop loss
        suggested_stop = self.position_sizer.suggest_stop_loss(
            entry_price=position.entry_price,
            method="percentage",
        )

        # Determine escalation level
        escalation_level = self._get_escalation_level(tracking)

        # Send alert
        self.dispatcher.send_missing_stop_loss_alert(
            symbol=position.symbol,
            entry_price=float(position.entry_price),
            current_price=float(position.current_price) if position.current_price else None,
            drawdown_pct=float(position.current_drawdown_pct) if position.current_drawdown_pct else None,
            suggested_stop=float(suggested_stop),
            escalation_level=escalation_level,
            last_alert_time=tracking.updated_at,
            stop_loss_tracking_id=tracking.id,
        )

        # Update tracking
        channel = "telegram" if escalation_level == 0 else "sms" if escalation_level == 1 else "phone_call"
        self.repo.mark_alert_sent(position.symbol, channel)

        logger.warning(
            f"ALERT: {position.symbol} has NO STOP LOSS! "
            f"Entry: ${position.entry_price}, Current: ${position.current_price}, "
            f"Drawdown: {position.current_drawdown_pct}%"
        )

    def _handle_critical_drawdown(self, position: Position, tracking):
        """Handle a position with critical drawdown (> 10%)."""
        # Only alert once per critical threshold
        # This is separate from missing stop loss alerts

        self.dispatcher.send_drawdown_alert(
            symbol=position.symbol,
            entry_price=float(position.entry_price),
            current_price=float(position.current_price),
            drawdown_pct=float(position.current_drawdown_pct),
            stop_loss_price=float(position.stop_loss_price) if position.stop_loss_price else None,
            stop_loss_tracking_id=tracking.id,
        )

        logger.critical(
            f"CRITICAL DRAWDOWN: {position.symbol} down {abs(position.current_drawdown_pct):.1f}%!"
        )

    def _handle_warning_drawdown(self, position: Position, tracking):
        """Handle a position with warning drawdown (> 5%)."""
        # Log but don't necessarily alert for warnings if stop is set
        if position.has_stop_loss:
            logger.warning(
                f"Drawdown warning: {position.symbol} down {abs(position.current_drawdown_pct):.1f}% "
                f"(stop at ${position.stop_loss_price})"
            )
        else:
            # No stop loss + drawdown = escalate
            self._handle_missing_stop_loss(position, tracking)

    def _handle_stop_triggered(self, position: Position, tracking):
        """Handle a position where stop loss has been triggered."""
        # This is informational - the stop should execute automatically on Robinhood
        logger.info(
            f"Stop loss triggered for {position.symbol}: "
            f"Current ${position.current_price} < Stop ${position.stop_loss_price}"
        )

        # Send an informational alert
        message = (
            f"Stop loss triggered for {position.symbol}!\n"
            f"Current: ${position.current_price}\n"
            f"Stop: ${position.stop_loss_price}\n"
            f"Check that the stop order executed on Robinhood."
        )

        self.telegram.send_alert(message)

    def _should_send_alert(self, tracking) -> bool:
        """Determine if we should send an alert based on timing and escalation."""
        # First alert: always send
        if tracking.alert_count == 0:
            return True

        # Check if enough time has passed for escalation
        time_since_last = datetime.now(timezone.utc) - tracking.updated_at
        escalation_interval = timedelta(minutes=settings.escalation_interval_minutes)

        return time_since_last >= escalation_interval

    def _get_escalation_level(self, tracking) -> int:
        """Get the appropriate escalation level."""
        current_level = {
            "none": 0,
            "telegram": 0,
            "sms": 1,
            "phone_call": 2,
        }.get(tracking.alert_escalation_level, 0)

        # Escalate if we've sent max alerts at current level
        if current_level == 0 and tracking.alert_count >= settings.max_telegram_alerts:
            return 1
        elif current_level == 1 and tracking.alert_count >= settings.max_telegram_alerts + settings.max_sms_alerts:
            return 2

        return current_level

    def check_position_size(
        self,
        symbol: str,
        entry_price: Decimal,
        stop_price: Decimal,
    ) -> str:
        """Check if a potential trade is properly sized.

        Returns formatted recommendation string.
        """
        # Get current account balance
        account_state = self.redis.get_account_state()
        if not account_state:
            return "Error: Could not get account balance from Redis"

        return self.position_sizer.format_recommendation(
            symbol=symbol,
            entry_price=entry_price,
            stop_price=stop_price,
            account_balance=account_state.total_equity,
        )

    def set_stop_loss(
        self,
        symbol: str,
        stop_price: Decimal,
        stop_type: str = "manual",
    ):
        """Manually set a stop loss for a position."""
        # Get entry price to calculate percentage
        positions = self.repo.get_open_positions()
        position = next((p for p in positions if p.symbol == symbol), None)

        if not position:
            logger.warning(f"No open position found for {symbol}")
            return

        stop_pct = ((position.entry_price - stop_price) / position.entry_price) * 100

        self.repo.update_stop_loss(
            symbol=symbol,
            stop_loss_price=stop_price,
            stop_loss_type=stop_type,
            stop_loss_pct=stop_pct,
        )

        # Send confirmation
        message = (
            f"Stop loss set for {symbol}\n"
            f"Entry: ${position.entry_price}\n"
            f"Stop: ${stop_price} ({stop_pct:.1f}% below)\n"
            f"Risk per share: ${position.entry_price - stop_price:.2f}"
        )
        self.telegram.send_alert(message)

        logger.info(f"Stop loss set for {symbol} at ${stop_price}")
