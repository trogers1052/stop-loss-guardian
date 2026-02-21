"""Stop Loss Guardian - Main monitoring logic.

The platform's #1 job is keeping losses small.
This service monitors all open positions and SCREAMS if any position lacks a stop loss.
"""

import logging
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Dict, List, Optional

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
        # In-memory cooldowns for critical drawdown alerts (symbol → last alert time).
        # Resets on restart — worst case is one extra alert, which is acceptable.
        self._critical_drawdown_cooldowns: Dict[str, datetime] = {}

    def start(self):
        """Initialize connections and start monitoring."""
        logger.info("Starting Stop Loss Guardian")

        # Connect to services
        self.repo.connect()
        self.redis.connect()

        # Restore cooldowns that survived the previous process lifetime.
        # Worst case if Redis is unavailable: one extra alert per symbol,
        # which is acceptable — losing an alert is worse than a duplicate.
        persisted = self.redis.get_drawdown_cooldowns()
        if persisted:
            self._critical_drawdown_cooldowns.update(persisted)
            logger.info(
                f"Restored {len(persisted)} drawdown cooldown(s) from Redis: "
                f"{list(persisted.keys())}"
            )

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
        """Stop monitoring and cleanup all resources.

        Safe to call multiple times (idempotent).  The signal handler and the
        ``finally`` block in ``main()`` may both invoke this method.
        """
        if not self._running and self.repo.conn is None and self.redis.client is None:
            return  # already stopped
        logger.info("Stopping Stop Loss Guardian...")
        self._running = False
        self.repo.close()
        self.redis.close()
        logger.info("Stop Loss Guardian shutdown complete")

    # Number of consecutive monitoring-loop errors before sending a degraded alert.
    _ERROR_ALERT_THRESHOLD = 5

    def _run_monitoring_loop(self):
        """Main monitoring loop."""
        logger.info(f"Starting monitoring loop (interval: {settings.check_interval_seconds}s)")

        consecutive_errors = 0

        while self._running:
            try:
                # Ensure database connection is healthy
                self.repo.ensure_connected()
                self._check_all_positions()

                if consecutive_errors > 0:
                    logger.info(
                        f"Monitoring loop recovered after {consecutive_errors} consecutive error(s)"
                    )
                consecutive_errors = 0

            except Exception as e:
                consecutive_errors += 1
                logger.error(
                    f"Error in monitoring loop (consecutive: {consecutive_errors}): {e}",
                    exc_info=True,
                )

                if consecutive_errors == self._ERROR_ALERT_THRESHOLD:
                    # Escalate: positions are not being monitored.
                    logger.critical(
                        f"Stop Loss Guardian: {consecutive_errors} consecutive monitoring failures. "
                        f"Positions may be UNPROTECTED. Last error: {e}"
                    )
                    try:
                        self.telegram.send_alert(
                            f"🚨 STOP LOSS GUARDIAN DEGRADED\n\n"
                            f"{consecutive_errors} consecutive monitoring failures.\n"
                            f"Positions may be UNPROTECTED.\n\n"
                            f"Last error: {e}\n\n"
                            f"Check Pi logs immediately."
                        )
                    except Exception as alert_exc:
                        logger.error(f"Failed to send degraded-service alert: {alert_exc}")

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
            try:
                self._check_position(position)
            except Exception as e:
                logger.error(f"Error checking position {position.symbol}: {e}", exc_info=True)
                # continue to next position

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
                except ValueError as exc:
                    logger.warning(
                        f"{pos.symbol}: could not parse price_updated_at "
                        f"'{redis_data['updated_at']}' from Redis: {exc}"
                    )

            # Get stop loss info - first check Redis (from Robinhood), then tracking table
            stop_order = self.redis.get_stop_order(pos.symbol)
            if stop_order:
                # Stop loss exists in Robinhood - use that
                pos.stop_loss_price = Decimal(stop_order["stop_price"])
                pos.stop_loss_type = "robinhood"
                # Calculate stop loss percentage from entry price
                if pos.entry_price and pos.entry_price > 0:
                    pos.stop_loss_pct = ((pos.entry_price - pos.stop_loss_price) / pos.entry_price) * 100
                logger.debug(f"{pos.symbol}: Found Robinhood stop @ ${pos.stop_loss_price}")
            else:
                # Fall back to tracking table (manual entries)
                tracking = self.repo.get_stop_loss_tracking(pos.symbol)
                if tracking and tracking.stop_loss_price:
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

        # Determine price freshness once — used in both branches below.
        price_is_stale = self._is_price_stale(position)

        # Check 1: Missing stop loss — always alert regardless of price freshness.
        # When price data is stale we still fire the alert (the position is still
        # unprotected) but we suppress the current price / drawdown fields so the
        # trader isn't shown hours-old figures as if they were live.
        if not position.has_stop_loss:
            if price_is_stale:
                logger.warning(
                    f"{position.symbol}: price data is stale — missing stop loss alert "
                    f"will omit current price/drawdown to avoid misleading figures"
                )
            self._handle_missing_stop_loss(position, tracking, price_is_stale)
            return

        # Checks 2 and 3 depend on a current price. Skip them when price data is
        # stale so we never act on hours-old drawdown figures.
        if price_is_stale:
            logger.warning(
                f"{position.symbol}: price data is stale "
                f"(updated_at={position.price_updated_at}, "
                f"threshold={settings.price_staleness_minutes}min) "
                f"— skipping drawdown and stop-trigger checks"
            )
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

    def _handle_missing_stop_loss(
        self,
        position: Position,
        tracking,
        price_is_stale: bool = False,
    ):
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

        # When price data is stale, omit live-price fields so the alert
        # doesn't show hours-old figures as if they were current.
        current_price = (
            float(position.current_price)
            if position.current_price and not price_is_stale
            else None
        )
        drawdown_pct = (
            float(position.current_drawdown_pct)
            if position.current_drawdown_pct and not price_is_stale
            else None
        )

        # Send alert
        self.dispatcher.send_missing_stop_loss_alert(
            symbol=position.symbol,
            entry_price=float(position.entry_price),
            current_price=current_price,
            drawdown_pct=drawdown_pct,
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
        if not self._should_send_drawdown_alert(position.symbol):
            return

        self.dispatcher.send_drawdown_alert(
            symbol=position.symbol,
            entry_price=float(position.entry_price),
            current_price=float(position.current_price),
            drawdown_pct=float(position.current_drawdown_pct),
            stop_loss_price=float(position.stop_loss_price) if position.stop_loss_price else None,
            stop_loss_tracking_id=tracking.id,
        )

        self._set_drawdown_cooldown(position.symbol)

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

    def _should_send_drawdown_alert(self, symbol: str) -> bool:
        """Return True if enough time has passed to send another critical drawdown alert."""
        last = self._critical_drawdown_cooldowns.get(symbol)
        if last is None:
            return True
        return (datetime.now(timezone.utc) - last) >= timedelta(
            minutes=settings.escalation_interval_minutes
        )

    def _set_drawdown_cooldown(self, symbol: str) -> None:
        """Record that a critical drawdown alert was just sent for this symbol.

        The timestamp is stored both in the in-memory dict (for fast reads
        within the same process lifetime) and in Redis (so cooldowns survive
        a service restart).
        """
        now = datetime.now(timezone.utc)
        try:
            self.redis.set_drawdown_cooldown(symbol, now)
        except Exception as e:
            logger.error(f"Failed to persist drawdown cooldown for {symbol} to Redis: {e}")
        self._critical_drawdown_cooldowns[symbol] = now

    def _is_price_stale(self, position: Position) -> bool:
        """Return True if the position's price data is too old to trust.

        A None price_updated_at (Redis had no data) is always treated as stale.
        Handles both timezone-aware and timezone-naive datetimes from Redis.
        """
        if position.price_updated_at is None:
            return True
        updated_at = position.price_updated_at
        if updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=timezone.utc)
        age = datetime.now(timezone.utc) - updated_at
        return age > timedelta(minutes=settings.price_staleness_minutes)

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
