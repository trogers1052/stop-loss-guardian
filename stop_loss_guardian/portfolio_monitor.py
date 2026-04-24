"""Portfolio-level risk monitor.

Runs alongside per-position monitoring in the guardian's main loop.
Calculates aggregate portfolio risk metrics and writes them to Redis
so the decision-engine can gate new entries based on real-time state.

Key metrics tracked:
- Actual portfolio heat (using real stop distances, not configured %)
- Daily P&L across all positions
- Stops hit today (position disappeared since last scan)
- Gap risk detection (price gapped through stop level)

Writes to Redis key ``risk:daily_state`` every check cycle.
"""

import json
import logging
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Dict, List, Optional

from .config import settings
from .models import Position

logger = logging.getLogger(__name__)

DAILY_STATE_KEY = "risk:daily_state"
DAILY_STATE_TTL = 86400  # 24 hours
DAILY_EQUITY_OPEN_KEY = "trading:daily_equity_open"
BUYING_POWER_KEY = "robinhood:buying_power"


class PortfolioDailyState:
    """Snapshot of portfolio-level risk for a single trading day."""

    def __init__(
        self,
        *,
        trade_date: str,
        stops_hit_today: int = 0,
        stops_hit_symbols: Optional[List[str]] = None,
        daily_pnl_pct: float = 0.0,
        daily_pnl_dollars: float = 0.0,
        actual_portfolio_heat: float = 0.0,
        position_risks: Optional[Dict[str, dict]] = None,
        sector_heat: Optional[Dict[str, float]] = None,
        halted: bool = False,
        halt_reason: Optional[str] = None,
        gap_alerts: Optional[List[dict]] = None,
        open_position_count: int = 0,
        total_equity: float = 0.0,
        updated_at: Optional[str] = None,
    ):
        self.trade_date = trade_date
        self.stops_hit_today = stops_hit_today
        self.stops_hit_symbols = stops_hit_symbols or []
        self.daily_pnl_pct = daily_pnl_pct
        self.daily_pnl_dollars = daily_pnl_dollars
        self.actual_portfolio_heat = actual_portfolio_heat
        self.position_risks = position_risks or {}
        self.sector_heat = sector_heat or {}
        self.halted = halted
        self.halt_reason = halt_reason
        self.gap_alerts = gap_alerts or []
        self.open_position_count = open_position_count
        self.total_equity = total_equity
        self.updated_at = updated_at or datetime.now(timezone.utc).isoformat()

    def to_dict(self) -> dict:
        return {
            "date": self.trade_date,
            "stops_hit_today": self.stops_hit_today,
            "stops_hit_symbols": self.stops_hit_symbols,
            "daily_pnl_pct": round(self.daily_pnl_pct, 6),
            "daily_pnl_dollars": round(self.daily_pnl_dollars, 2),
            "actual_portfolio_heat": round(self.actual_portfolio_heat, 6),
            "position_risks": self.position_risks,
            "sector_heat": self.sector_heat,
            "halted": self.halted,
            "halt_reason": self.halt_reason,
            "gap_alerts": self.gap_alerts,
            "open_position_count": self.open_position_count,
            "total_equity": round(self.total_equity, 2),
            "updated_at": self.updated_at,
        }


# Sector groups — mirrors risk_config.yaml in decision-engine.
# Kept in sync manually; symbols not listed here have no sector constraint.
SECTOR_GROUPS: Dict[str, List[str]] = {
    "uranium": ["CCJ", "URNM", "UUUU"],
    "precious_metals": ["WPM", "SLV", "IAUM", "PPLT"],
    "industrial": ["CAT", "ETN"],
    "rare_earth": ["MP"],
    "defense": ["RTX", "ITA", "AVAV"],
    "semiconductor": ["APH"],
    "energy": ["XLE"],
}

# Invert for O(1) lookup
_SYMBOL_TO_SECTOR: Dict[str, str] = {}
for _sector, _symbols in SECTOR_GROUPS.items():
    for _sym in _symbols:
        _SYMBOL_TO_SECTOR[_sym] = _sector


class PortfolioMonitor:
    """Monitors aggregate portfolio risk across all open positions.

    Called from the guardian's main loop after per-position checks.
    """

    def __init__(self, redis_client, telegram_client):
        """
        Args:
            redis_client: Guardian's RedisClient instance (already connected).
            telegram_client: Guardian's TelegramClient instance.
        """
        self.redis = redis_client
        self.telegram = telegram_client
        self._previous_symbols: set = set()
        self._stops_hit_today: List[str] = []
        self._last_date: str = ""
        self._halt_alert_sent_today: bool = False
        self._stops_alerted_count: int = 0

    def check(self, positions: List[Position]) -> Optional[PortfolioDailyState]:
        """Run all portfolio-level checks.

        Args:
            positions: Enriched positions from the guardian's current cycle
                       (already have current_price, stop_loss_price, etc.).

        Returns:
            PortfolioDailyState written to Redis, or None on error.
        """
        today = date.today().isoformat()

        # Reset counters on new trading day
        if today != self._last_date:
            self._stops_hit_today = []
            self._halt_alert_sent_today = False
            self._stops_alerted_count = 0
            self._last_date = today

        current_symbols = {p.symbol for p in positions}

        # Detect stops hit (position disappeared since last scan)
        self._detect_hit_stops(current_symbols)

        # Build stop orders map for heat + gap calculations
        stop_orders = self._load_all_stop_orders()

        # Calculate actual portfolio heat from real stop distances
        position_risks, sector_heat, actual_heat = self._calculate_actual_heat(
            positions, stop_orders
        )

        # Calculate daily P&L
        daily_pnl_pct, daily_pnl_dollars, total_equity = self._calculate_daily_pnl()

        # Detect gap risk (price below stop)
        gap_alerts = self._detect_gap_risk(positions, stop_orders)

        # Determine halt state
        halted, halt_reason = self._evaluate_halt(
            daily_pnl_pct, actual_heat, len(self._stops_hit_today)
        )

        state = PortfolioDailyState(
            trade_date=today,
            stops_hit_today=len(self._stops_hit_today),
            stops_hit_symbols=list(self._stops_hit_today),
            daily_pnl_pct=daily_pnl_pct,
            daily_pnl_dollars=daily_pnl_dollars,
            actual_portfolio_heat=actual_heat,
            position_risks=position_risks,
            sector_heat=sector_heat,
            halted=halted,
            halt_reason=halt_reason,
            gap_alerts=gap_alerts,
            open_position_count=len(positions),
            total_equity=total_equity,
        )

        # Write to Redis
        self._write_state(state)

        # Send alerts if thresholds breached
        self._check_thresholds_and_alert(state)

        # Update previous symbols for next scan
        self._previous_symbols = current_symbols

        return state

    # ------------------------------------------------------------------
    # Stop detection
    # ------------------------------------------------------------------

    def _detect_hit_stops(self, current_symbols: set) -> None:
        """Detect positions that disappeared since last scan.

        Only counts as a stop hit if the symbol had an active stop order
        in the previous cycle.  Manual sells (no stop order) are logged
        but not counted toward the daily stops-hit total.
        """
        if not self._previous_symbols:
            # First scan — no comparison possible
            return

        disappeared = self._previous_symbols - current_symbols
        if not disappeared:
            return

        # Check which disappeared symbols actually had stop orders
        stop_orders = self._load_all_stop_orders()

        for symbol in disappeared:
            if symbol in self._stops_hit_today:
                continue

            if symbol in stop_orders:
                self._stops_hit_today.append(symbol)
                logger.warning(
                    f"Portfolio monitor: {symbol} stop hit "
                    f"(stop #{len(self._stops_hit_today)} today)"
                )
            else:
                logger.info(
                    f"Portfolio monitor: {symbol} position closed "
                    f"(no stop order — likely manual sell, not counted)"
                )

    # ------------------------------------------------------------------
    # Heat calculation
    # ------------------------------------------------------------------

    def _calculate_actual_heat(
        self,
        positions: List[Position],
        stop_orders: Dict[str, dict],
    ) -> tuple:
        """Calculate actual portfolio heat from real stop distances.

        Returns:
            (position_risks, sector_heat, total_heat)
        """
        position_risks: Dict[str, dict] = {}
        sector_heat: Dict[str, float] = {}
        total_heat = 0.0

        for pos in positions:
            entry = float(pos.entry_price) if pos.entry_price else 0.0
            current = float(pos.current_price) if pos.current_price else entry
            shares = float(pos.quantity) if pos.quantity else 0.0

            if entry <= 0 or current <= 0:
                continue

            # Get stop price — prefer Robinhood order, fall back to tracking
            stop_price = None
            if pos.symbol in stop_orders:
                try:
                    stop_price = float(stop_orders[pos.symbol]["stop_price"])
                except (KeyError, ValueError, TypeError):
                    pass
            if stop_price is None and pos.stop_loss_price is not None:
                stop_price = float(pos.stop_loss_price)

            # Calculate risk for this position
            if stop_price and stop_price > 0:
                # Actual risk = (entry - stop) / entry as fraction
                stop_distance_pct = (entry - stop_price) / entry
                # Risk in dollar terms = shares * (entry - stop)
                dollar_risk = shares * (entry - stop_price)
            else:
                # No stop = worst case: use configured default (10%)
                stop_distance_pct = float(settings.default_stop_loss_pct) / 100.0
                dollar_risk = shares * entry * stop_distance_pct

            # Get account equity for risk % calculation
            account_state = self.redis.get_account_state()
            total_equity = float(account_state.total_equity) if account_state else 0.0

            risk_pct = dollar_risk / total_equity if total_equity > 0 else 0.0
            total_heat += risk_pct

            sector = _SYMBOL_TO_SECTOR.get(pos.symbol)

            position_risks[pos.symbol] = {
                "risk_pct": round(risk_pct, 6),
                "stop_distance_pct": round(stop_distance_pct, 4),
                "stop_price": stop_price,
                "dollar_risk": round(dollar_risk, 2),
                "sector": sector,
                "has_stop": stop_price is not None,
            }

            if sector:
                sector_heat[sector] = sector_heat.get(sector, 0.0) + risk_pct

        return position_risks, sector_heat, total_heat

    # ------------------------------------------------------------------
    # Daily P&L
    # ------------------------------------------------------------------

    def _calculate_daily_pnl(self) -> tuple:
        """Calculate daily P&L from Redis equity snapshots.

        Returns:
            (daily_pnl_pct, daily_pnl_dollars, current_equity)
        """
        if not self.redis.client:
            return 0.0, 0.0, 0.0

        try:
            raw_open = self.redis.client.get(DAILY_EQUITY_OPEN_KEY)
            raw_current = self.redis.client.get(BUYING_POWER_KEY)
        except Exception as e:
            logger.warning(f"Portfolio monitor: Redis read failed for P&L: {e}")
            return 0.0, 0.0, 0.0

        if not raw_open or not raw_current:
            return 0.0, 0.0, 0.0

        try:
            open_data = json.loads(raw_open)
            opening_equity = float(open_data["equity"])

            current_data = json.loads(raw_current)
            current_equity = float(current_data["total_equity"])
        except (json.JSONDecodeError, KeyError, ValueError, TypeError) as e:
            logger.warning(f"Portfolio monitor: P&L parse error: {e}")
            return 0.0, 0.0, 0.0

        if opening_equity <= 0:
            return 0.0, 0.0, current_equity

        pnl_pct = (current_equity - opening_equity) / opening_equity
        pnl_dollars = current_equity - opening_equity

        return pnl_pct, pnl_dollars, current_equity

    # ------------------------------------------------------------------
    # Gap risk detection
    # ------------------------------------------------------------------

    def _detect_gap_risk(
        self,
        positions: List[Position],
        stop_orders: Dict[str, dict],
    ) -> List[dict]:
        """Detect positions where current price is below stop level."""
        gap_alerts = []

        for pos in positions:
            current = float(pos.current_price) if pos.current_price else None
            entry = float(pos.entry_price) if pos.entry_price else None
            if current is None or entry is None or entry <= 0:
                continue

            # Get stop price
            stop_price = None
            if pos.symbol in stop_orders:
                try:
                    stop_price = float(stop_orders[pos.symbol]["stop_price"])
                except (KeyError, ValueError, TypeError):
                    pass
            if stop_price is None and pos.stop_loss_price is not None:
                stop_price = float(pos.stop_loss_price)

            if stop_price and current < stop_price:
                planned_loss_pct = (stop_price - entry) / entry
                actual_loss_pct = (current - entry) / entry
                gap_alerts.append({
                    "symbol": pos.symbol,
                    "stop_price": stop_price,
                    "current_price": current,
                    "entry_price": entry,
                    "planned_loss_pct": round(planned_loss_pct, 4),
                    "actual_loss_pct": round(actual_loss_pct, 4),
                    "excess_loss_pct": round(actual_loss_pct - planned_loss_pct, 4),
                })

        return gap_alerts

    # ------------------------------------------------------------------
    # Halt evaluation
    # ------------------------------------------------------------------

    def _evaluate_halt(
        self,
        daily_pnl_pct: float,
        actual_heat: float,
        stops_hit: int,
    ) -> tuple:
        """Determine if new entries should be halted.

        Returns:
            (halted: bool, reason: str | None)
        """
        max_stops = settings.portfolio_max_stops_per_day
        daily_halt = settings.portfolio_daily_loss_halt_pct
        heat_halt = settings.portfolio_heat_halt_pct

        reasons = []

        if stops_hit >= max_stops:
            reasons.append(
                f"{stops_hit} stops hit today (limit: {max_stops})"
            )

        if daily_pnl_pct <= -daily_halt:
            reasons.append(
                f"daily loss {daily_pnl_pct:.1%} exceeds {daily_halt:.0%} limit"
            )

        if actual_heat > heat_halt:
            reasons.append(
                f"actual heat {actual_heat:.1%} exceeds {heat_halt:.0%} limit"
            )

        if reasons:
            return True, "; ".join(reasons)
        return False, None

    # ------------------------------------------------------------------
    # Redis state persistence
    # ------------------------------------------------------------------

    def _write_state(self, state: PortfolioDailyState) -> None:
        """Write portfolio state to Redis for decision-engine consumption."""
        if not self.redis.client:
            return
        try:
            self.redis.client.setex(
                DAILY_STATE_KEY,
                DAILY_STATE_TTL,
                json.dumps(state.to_dict()),
            )
        except Exception as e:
            logger.warning(f"Portfolio monitor: failed to write state to Redis: {e}")

    # ------------------------------------------------------------------
    # Alert thresholds
    # ------------------------------------------------------------------

    def _check_thresholds_and_alert(self, state: PortfolioDailyState) -> None:
        """Send Telegram alerts when portfolio thresholds are breached."""
        stops = state.stops_hit_today
        max_stops = settings.portfolio_max_stops_per_day

        # Individual stop hit notification (only alert on new stops)
        if stops > self._stops_alerted_count and len(state.stops_hit_symbols) > 0:
            latest_symbol = state.stops_hit_symbols[-1]

            if stops == 1:
                self._send_alert(
                    f"Stop hit: {latest_symbol}. "
                    f"Daily P&L: {state.daily_pnl_pct:.1%}. "
                    f"Portfolio heat: {state.actual_portfolio_heat:.1%}.",
                    severity="info",
                )
            elif stops == 2:
                self._send_alert(
                    f"2 stops hit today ({', '.join(state.stops_hit_symbols)}). "
                    f"Daily P&L: {state.daily_pnl_pct:.1%}. "
                    f"Review remaining positions.",
                    severity="warning",
                )

            self._stops_alerted_count = stops

        # Portfolio halt alert (send once per day)
        if state.halted and not self._halt_alert_sent_today:
            self._send_alert(
                f"PORTFOLIO CIRCUIT BREAKER\n\n"
                f"Reason: {state.halt_reason}\n"
                f"Daily P&L: {state.daily_pnl_pct:.1%} (${state.daily_pnl_dollars:+.2f})\n"
                f"Stops hit: {stops}\n"
                f"Actual heat: {state.actual_portfolio_heat:.1%}\n"
                f"Positions: {state.open_position_count}\n\n"
                f"All new BUY entries halted.",
                severity="critical",
            )
            self._halt_alert_sent_today = True

        # Excessive stops alert (even if not halted yet)
        if stops >= max_stops and not state.halted:
            self._send_alert(
                f"{stops} stops hit today "
                f"({', '.join(state.stops_hit_symbols)}). "
                f"Daily P&L: {state.daily_pnl_pct:.1%}. "
                f"Consider closing remaining positions.",
                severity="urgent",
            )

        # High heat warning (actual heat, not configured)
        heat_warn = settings.portfolio_heat_warn_pct
        if state.actual_portfolio_heat > heat_warn and not state.halted:
            self._send_alert(
                f"Portfolio heat {state.actual_portfolio_heat:.1%} "
                f"exceeds warning threshold {heat_warn:.0%}. "
                f"Actual stop distances exceed configured risk.",
                severity="warning",
            )

        # Gap risk alerts
        for gap in state.gap_alerts:
            self._send_alert(
                f"GAP RISK: {gap['symbol']} price ${gap['current_price']:.2f} "
                f"is below stop ${gap['stop_price']:.2f}. "
                f"Planned loss: {gap['planned_loss_pct']:.1%}, "
                f"Actual loss: {gap['actual_loss_pct']:.1%} "
                f"(excess: {gap['excess_loss_pct']:.1%}).",
                severity="urgent",
            )

    def _send_alert(self, message: str, severity: str = "info") -> None:
        """Send a portfolio-level alert via Telegram."""
        prefix = {
            "info": "Portfolio",
            "warning": "Portfolio WARNING",
            "urgent": "PORTFOLIO ALERT",
            "critical": "PORTFOLIO CIRCUIT BREAKER",
        }.get(severity, "Portfolio")

        full_message = f"{prefix}: {message}"
        logger.warning(full_message)

        try:
            self.telegram.send_alert(full_message)
        except Exception as e:
            logger.error(f"Failed to send portfolio alert: {e}")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _load_all_stop_orders(self) -> Dict[str, dict]:
        """Load all stop orders from Redis."""
        return self.redis.get_all_stop_orders()
