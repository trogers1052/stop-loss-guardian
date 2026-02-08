"""Database repository for Stop Loss Guardian."""

import logging
from datetime import datetime
from decimal import Decimal
from typing import List, Optional, Dict, Any

import psycopg2
from psycopg2.extras import RealDictCursor

from ..config import settings
from ..models import Position, StopLossRecord, Alert, AlertType, Severity, AlertChannel

logger = logging.getLogger(__name__)


class Repository:
    """Database access for stop loss tracking and alerts."""

    def __init__(self):
        self.conn = None

    def connect(self):
        """Establish database connection."""
        try:
            self.conn = psycopg2.connect(
                host=settings.db_host,
                port=settings.db_port,
                user=settings.db_user,
                password=settings.db_password,
                dbname=settings.db_name,
            )
            self.conn.autocommit = False
            logger.info("Connected to database")
        except Exception as e:
            logger.error(f"Failed to connect to database: {e}")
            raise

    def close(self):
        """Close database connection."""
        if self.conn:
            self.conn.close()
            self.conn = None

    def get_open_positions(self) -> List[Position]:
        """Get all open positions from journal_positions table."""
        query = """
            SELECT
                jp.id as position_id,
                jp.symbol,
                jp.entry_price,
                jp.quantity,
                jp.entry_date,
                jp.status
            FROM journal_positions jp
            WHERE jp.status = 'open'
            ORDER BY jp.entry_date DESC
        """
        try:
            with self.conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(query)
                rows = cur.fetchall()

            positions = []
            for row in rows:
                positions.append(Position(
                    symbol=row["symbol"],
                    quantity=Decimal(str(row["quantity"])),
                    entry_price=Decimal(str(row["entry_price"])),
                    entry_date=row["entry_date"],
                    position_id=row["position_id"],
                ))
            return positions
        except Exception as e:
            logger.error(f"Failed to get open positions: {e}")
            return []

    def get_stop_loss_tracking(self, symbol: str) -> Optional[StopLossRecord]:
        """Get stop loss tracking record for a symbol."""
        query = """
            SELECT * FROM stop_loss_tracking
            WHERE symbol = %s
            ORDER BY created_at DESC
            LIMIT 1
        """
        try:
            with self.conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(query, (symbol,))
                row = cur.fetchone()

            if not row:
                return None

            return StopLossRecord(
                id=row["id"],
                symbol=row["symbol"],
                position_id=row["position_id"],
                entry_price=Decimal(str(row["entry_price"])),
                quantity=Decimal(str(row["quantity"])),
                stop_loss_price=Decimal(str(row["stop_loss_price"])) if row["stop_loss_price"] else None,
                stop_loss_type=row["stop_loss_type"],
                stop_loss_pct=Decimal(str(row["stop_loss_pct"])) if row["stop_loss_pct"] else None,
                current_price=Decimal(str(row["current_price"])) if row["current_price"] else None,
                current_drawdown_pct=Decimal(str(row["current_drawdown_pct"])) if row["current_drawdown_pct"] else None,
                missing_stop_alert_sent=row["missing_stop_alert_sent"],
                alert_count=row["alert_count"],
                alert_escalation_level=row["alert_escalation_level"],
                acknowledged=row["acknowledged"],
                next_earnings_date=row["next_earnings_date"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )
        except Exception as e:
            logger.error(f"Failed to get stop loss tracking for {symbol}: {e}")
            return None

    def upsert_stop_loss_tracking(
        self,
        symbol: str,
        position_id: Optional[int],
        entry_price: Decimal,
        quantity: Decimal,
        stop_loss_price: Optional[Decimal] = None,
        stop_loss_type: Optional[str] = None,
        stop_loss_pct: Optional[Decimal] = None,
        current_price: Optional[Decimal] = None,
        current_drawdown_pct: Optional[Decimal] = None,
    ) -> int:
        """Insert or update stop loss tracking record."""
        query = """
            INSERT INTO stop_loss_tracking (
                symbol, position_id, entry_price, quantity,
                stop_loss_price, stop_loss_type, stop_loss_pct,
                current_price, current_drawdown_pct, price_updated_at
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW()
            )
            ON CONFLICT (symbol, position_id)
            DO UPDATE SET
                entry_price = EXCLUDED.entry_price,
                quantity = EXCLUDED.quantity,
                stop_loss_price = COALESCE(EXCLUDED.stop_loss_price, stop_loss_tracking.stop_loss_price),
                stop_loss_type = COALESCE(EXCLUDED.stop_loss_type, stop_loss_tracking.stop_loss_type),
                stop_loss_pct = COALESCE(EXCLUDED.stop_loss_pct, stop_loss_tracking.stop_loss_pct),
                current_price = EXCLUDED.current_price,
                current_drawdown_pct = EXCLUDED.current_drawdown_pct,
                price_updated_at = NOW(),
                updated_at = NOW()
            RETURNING id
        """
        try:
            with self.conn.cursor() as cur:
                cur.execute(query, (
                    symbol, position_id, str(entry_price), str(quantity),
                    str(stop_loss_price) if stop_loss_price else None,
                    stop_loss_type,
                    str(stop_loss_pct) if stop_loss_pct else None,
                    str(current_price) if current_price else None,
                    str(current_drawdown_pct) if current_drawdown_pct else None,
                ))
                result = cur.fetchone()
                self.conn.commit()
                return result[0] if result else 0
        except Exception as e:
            self.conn.rollback()
            logger.error(f"Failed to upsert stop loss tracking: {e}")
            raise

    def update_stop_loss(
        self,
        symbol: str,
        stop_loss_price: Decimal,
        stop_loss_type: str,
        stop_loss_pct: Optional[Decimal] = None,
    ):
        """Set or update stop loss for a position."""
        query = """
            UPDATE stop_loss_tracking
            SET
                stop_loss_price = %s,
                stop_loss_type = %s,
                stop_loss_pct = %s,
                stop_loss_set_at = NOW(),
                missing_stop_alert_sent = false,
                acknowledged = true,
                acknowledged_at = NOW(),
                updated_at = NOW()
            WHERE symbol = %s
        """
        try:
            with self.conn.cursor() as cur:
                cur.execute(query, (
                    str(stop_loss_price),
                    stop_loss_type,
                    str(stop_loss_pct) if stop_loss_pct else None,
                    symbol,
                ))
                self.conn.commit()
                logger.info(f"Updated stop loss for {symbol}: ${stop_loss_price}")
        except Exception as e:
            self.conn.rollback()
            logger.error(f"Failed to update stop loss for {symbol}: {e}")
            raise

    def mark_alert_sent(
        self,
        symbol: str,
        escalation_level: str,
    ):
        """Mark that an alert was sent and update escalation level."""
        query = """
            UPDATE stop_loss_tracking
            SET
                missing_stop_alert_sent = true,
                last_alert_sent = NOW(),
                alert_count = alert_count + 1,
                alert_escalation_level = %s,
                updated_at = NOW()
            WHERE symbol = %s
        """
        try:
            with self.conn.cursor() as cur:
                cur.execute(query, (escalation_level, symbol))
                self.conn.commit()
        except Exception as e:
            self.conn.rollback()
            logger.error(f"Failed to mark alert sent for {symbol}: {e}")
            raise

    def acknowledge_alert(self, symbol: str, reason: Optional[str] = None):
        """Acknowledge alert for a position."""
        query = """
            UPDATE stop_loss_tracking
            SET
                acknowledged = true,
                acknowledged_at = NOW(),
                acknowledged_reason = %s,
                updated_at = NOW()
            WHERE symbol = %s
        """
        try:
            with self.conn.cursor() as cur:
                cur.execute(query, (reason, symbol))
                self.conn.commit()
                logger.info(f"Acknowledged alert for {symbol}")
        except Exception as e:
            self.conn.rollback()
            logger.error(f"Failed to acknowledge alert for {symbol}: {e}")
            raise

    def log_urgent_alert(
        self,
        alert: Alert,
        channel: AlertChannel,
        escalation_level: int,
        stop_loss_tracking_id: Optional[int] = None,
        twilio_sid: Optional[str] = None,
    ) -> int:
        """Log an urgent alert to the database."""
        query = """
            INSERT INTO urgent_alerts (
                alert_type, symbol, position_id, stop_loss_tracking_id,
                severity, escalation_level, message, details,
                channel, twilio_sid
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
            )
            RETURNING id
        """
        try:
            import json
            with self.conn.cursor() as cur:
                cur.execute(query, (
                    alert.alert_type.value,
                    alert.symbol,
                    alert.position_id,
                    stop_loss_tracking_id,
                    alert.severity.value,
                    escalation_level,
                    alert.message,
                    json.dumps(alert.details),
                    channel.value,
                    twilio_sid,
                ))
                result = cur.fetchone()
                self.conn.commit()
                return result[0] if result else 0
        except Exception as e:
            self.conn.rollback()
            logger.error(f"Failed to log urgent alert: {e}")
            raise

    def get_positions_without_stop_loss(self) -> List[Dict[str, Any]]:
        """Get positions that don't have stop losses configured."""
        query = """
            SELECT
                slt.id,
                slt.symbol,
                slt.position_id,
                slt.entry_price,
                slt.quantity,
                slt.current_price,
                slt.current_drawdown_pct,
                slt.alert_count,
                slt.alert_escalation_level,
                slt.last_alert_sent,
                slt.acknowledged
            FROM stop_loss_tracking slt
            WHERE slt.stop_loss_price IS NULL
              AND slt.acknowledged = false
            ORDER BY slt.created_at ASC
        """
        try:
            with self.conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(query)
                return cur.fetchall()
        except Exception as e:
            logger.error(f"Failed to get positions without stop loss: {e}")
            return []

    def get_positions_with_critical_drawdown(self, threshold_pct: Decimal) -> List[Dict[str, Any]]:
        """Get positions with drawdown exceeding threshold."""
        query = """
            SELECT
                slt.*
            FROM stop_loss_tracking slt
            WHERE slt.current_drawdown_pct IS NOT NULL
              AND slt.current_drawdown_pct <= %s
              AND slt.acknowledged = false
            ORDER BY slt.current_drawdown_pct ASC
        """
        try:
            with self.conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(query, (str(-threshold_pct),))  # Negative because drawdown is negative
                return cur.fetchall()
        except Exception as e:
            logger.error(f"Failed to get positions with critical drawdown: {e}")
            return []

    def cleanup_closed_positions(self):
        """Remove tracking for positions that are no longer open."""
        query = """
            DELETE FROM stop_loss_tracking slt
            WHERE NOT EXISTS (
                SELECT 1 FROM journal_positions jp
                WHERE jp.id = slt.position_id
                  AND jp.status = 'open'
            )
            AND slt.position_id IS NOT NULL
        """
        try:
            with self.conn.cursor() as cur:
                cur.execute(query)
                deleted = cur.rowcount
                self.conn.commit()
                if deleted > 0:
                    logger.info(f"Cleaned up {deleted} closed positions from tracking")
        except Exception as e:
            self.conn.rollback()
            logger.error(f"Failed to cleanup closed positions: {e}")
