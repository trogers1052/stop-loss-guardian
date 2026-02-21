"""Redis client for accessing Robinhood position data."""

import json
import logging
from datetime import datetime
from decimal import Decimal
from typing import Dict, Optional

import redis

from .config import settings
from .models import AccountState

logger = logging.getLogger(__name__)


class RedisClient:
    """Client for accessing Robinhood position and account data from Redis."""

    def __init__(self):
        self.client: Optional[redis.Redis] = None

    def connect(self):
        """Establish Redis connection."""
        try:
            self.client = redis.Redis(
                host=settings.redis_host,
                port=settings.redis_port,
                db=settings.redis_db,
                decode_responses=True,
            )
            # Test connection
            self.client.ping()
            logger.info("Connected to Redis")
        except Exception as e:
            logger.error(f"Failed to connect to Redis: {e}")
            raise

    def close(self):
        """Close Redis connection."""
        if self.client:
            self.client.close()
            self.client = None

    def get_positions(self) -> Dict[str, Dict]:
        """Get all current positions from Robinhood sync.

        Returns:
            Dict mapping symbol -> position data
            Position data includes: symbol, quantity, average_buy_price, equity,
            percent_change, equity_change, updated_at
        """
        try:
            # HGETALL returns all field-value pairs in the hash
            raw_positions = self.client.hgetall(settings.redis_positions_key)

            positions = {}
            for symbol, data_str in raw_positions.items():
                try:
                    position_data = json.loads(data_str)
                    positions[symbol] = position_data
                except json.JSONDecodeError as e:
                    logger.warning(f"Failed to parse position data for {symbol}: {e}")

            return positions
        except Exception as e:
            logger.error(f"Failed to get positions from Redis: {e}")
            return {}

    def get_position(self, symbol: str) -> Optional[Dict]:
        """Get position data for a specific symbol.

        Returns:
            Position data dict or None if not found
        """
        try:
            data_str = self.client.hget(settings.redis_positions_key, symbol)
            if not data_str:
                return None
            return json.loads(data_str)
        except Exception as e:
            logger.error(f"Failed to get position for {symbol}: {e}")
            return None

    def get_current_price(self, symbol: str) -> Optional[Decimal]:
        """Get current price for a symbol from position data.

        Calculates current price from equity / quantity.
        """
        position = self.get_position(symbol)
        if not position:
            return None

        try:
            equity = Decimal(position.get("equity", "0"))
            quantity = Decimal(position.get("quantity", "0"))

            if quantity <= 0:
                return None

            return equity / quantity
        except Exception as e:
            logger.warning(f"Failed to calculate current price for {symbol}: {e}")
            return None

    def get_account_state(self) -> Optional[AccountState]:
        """Get current account balance and buying power."""
        try:
            data_str = self.client.get(settings.redis_buying_power_key)
            if not data_str:
                return None

            data = json.loads(data_str)
            return AccountState(
                buying_power=Decimal(data.get("buying_power", "0")),
                cash=Decimal(data.get("cash", "0")),
                total_equity=Decimal(data.get("total_equity", "0")),
                updated_at=datetime.fromisoformat(data.get("updated_at", datetime.now().isoformat())),
            )
        except Exception as e:
            logger.error(f"Failed to get account state: {e}")
            return None

    def enrich_position(self, symbol: str, entry_price: Decimal, quantity: Decimal) -> Dict:
        """Enrich a position with current market data from Redis.

        Args:
            symbol: Stock symbol
            entry_price: Entry price from journal
            quantity: Number of shares

        Returns:
            Dict with current_price, current_equity, percent_change, equity_change
        """
        position_data = self.get_position(symbol)

        if not position_data:
            return {
                "current_price": None,
                "current_equity": None,
                "percent_change": None,
                "equity_change": None,
                "updated_at": None,
            }

        try:
            equity = Decimal(position_data.get("equity", "0"))
            qty = Decimal(position_data.get("quantity", "0"))

            current_price = equity / qty if qty > 0 else None
            percent_change = Decimal(position_data.get("percent_change", "0"))
            equity_change = Decimal(position_data.get("equity_change", "0"))

            return {
                "current_price": current_price,
                "current_equity": equity,
                "percent_change": percent_change,
                "equity_change": equity_change,
                "updated_at": position_data.get("updated_at"),
            }
        except Exception as e:
            logger.warning(f"Failed to enrich position for {symbol}: {e}")
            return {
                "current_price": None,
                "current_equity": None,
                "percent_change": None,
                "equity_change": None,
                "updated_at": None,
            }

    def get_stop_order(self, symbol: str) -> Optional[Dict]:
        """Get stop order for a specific symbol from Robinhood sync.

        Args:
            symbol: Stock symbol

        Returns:
            Stop order data dict or None if no stop order exists
            Contains: order_id, symbol, stop_price, quantity, side, order_type, state, created_at
        """
        try:
            data_str = self.client.hget("robinhood:stop_orders", symbol)
            if not data_str:
                return None
            return json.loads(data_str)
        except Exception as e:
            logger.error(f"Failed to get stop order for {symbol}: {e}")
            return None

    # ------------------------------------------------------------------
    # Drawdown cooldown persistence
    # ------------------------------------------------------------------
    _COOLDOWN_KEY = "guardian:drawdown_cooldowns"

    def get_drawdown_cooldowns(self) -> Dict[str, "datetime"]:
        """Load all critical-drawdown cooldown timestamps from Redis.

        Returns a dict mapping symbol -> aware UTC datetime.
        Symbols whose stored value cannot be parsed are silently skipped.
        """
        from datetime import datetime, timezone
        try:
            raw = self.client.hgetall(self._COOLDOWN_KEY)
        except Exception as e:
            logger.warning(f"Failed to read drawdown cooldowns from Redis: {e}")
            return {}

        result: Dict[str, datetime] = {}
        for symbol, ts_str in raw.items():
            try:
                dt = datetime.fromisoformat(ts_str)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                result[symbol] = dt
            except ValueError:
                logger.warning(f"Ignoring unparseable cooldown timestamp for {symbol}: {ts_str!r}")
        return result

    def set_drawdown_cooldown(self, symbol: str, timestamp: "datetime") -> None:
        """Persist a single cooldown timestamp to Redis."""
        try:
            self.client.hset(self._COOLDOWN_KEY, symbol, timestamp.isoformat())
        except Exception as e:
            logger.warning(f"Failed to persist drawdown cooldown for {symbol}: {e}")

    def get_all_stop_orders(self) -> Dict[str, Dict]:
        """Get all stop orders from Robinhood sync.

        Returns:
            Dict mapping symbol -> stop order data
        """
        try:
            raw_orders = self.client.hgetall("robinhood:stop_orders")

            orders = {}
            for symbol, data_str in raw_orders.items():
                try:
                    order_data = json.loads(data_str)
                    orders[symbol] = order_data
                except json.JSONDecodeError as e:
                    logger.warning(f"Failed to parse stop order data for {symbol}: {e}")

            return orders
        except Exception as e:
            logger.error(f"Failed to get stop orders from Redis: {e}")
            return {}
