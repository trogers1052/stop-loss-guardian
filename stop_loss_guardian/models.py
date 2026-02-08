"""Data models for Stop Loss Guardian."""

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Optional, Dict, Any, List


class AlertType(str, Enum):
    MISSING_STOP_LOSS = "missing_stop_loss"
    DRAWDOWN_WARNING = "drawdown_warning"
    DRAWDOWN_CRITICAL = "drawdown_critical"
    EARNINGS_WARNING = "earnings_warning"
    POSITION_SIZE_WARNING = "position_size_warning"


class Severity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    URGENT = "urgent"
    CRITICAL = "critical"


class AlertChannel(str, Enum):
    TELEGRAM = "telegram"
    SMS = "sms"
    PHONE_CALL = "phone_call"


class StopLossType(str, Enum):
    ATR_BASED = "atr_based"
    SUPPORT = "support"
    PERCENTAGE = "percentage"
    MANUAL = "manual"


@dataclass
class Position:
    """Open position from trading journal + Robinhood data."""
    symbol: str
    quantity: Decimal
    entry_price: Decimal
    entry_date: Optional[datetime] = None
    position_id: Optional[int] = None

    # Current state (from Robinhood)
    current_price: Optional[Decimal] = None
    current_equity: Optional[Decimal] = None
    percent_change: Optional[Decimal] = None
    equity_change: Optional[Decimal] = None
    price_updated_at: Optional[datetime] = None

    # Stop loss info (from stop_loss_tracking table)
    stop_loss_price: Optional[Decimal] = None
    stop_loss_type: Optional[StopLossType] = None
    stop_loss_pct: Optional[Decimal] = None

    @property
    def has_stop_loss(self) -> bool:
        return self.stop_loss_price is not None

    @property
    def current_drawdown_pct(self) -> Optional[Decimal]:
        """Calculate current drawdown from entry price."""
        if self.current_price is None or self.entry_price is None:
            return None
        return ((self.current_price - self.entry_price) / self.entry_price) * 100

    @property
    def stop_loss_triggered(self) -> bool:
        """Check if current price is below stop loss."""
        if not self.has_stop_loss or self.current_price is None:
            return False
        return self.current_price <= self.stop_loss_price

    @property
    def position_value(self) -> Optional[Decimal]:
        """Current market value of position."""
        if self.current_price is None:
            return None
        return self.quantity * self.current_price

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "quantity": str(self.quantity),
            "entry_price": str(self.entry_price),
            "entry_date": self.entry_date.isoformat() if self.entry_date else None,
            "position_id": self.position_id,
            "current_price": str(self.current_price) if self.current_price else None,
            "current_equity": str(self.current_equity) if self.current_equity else None,
            "percent_change": str(self.percent_change) if self.percent_change else None,
            "stop_loss_price": str(self.stop_loss_price) if self.stop_loss_price else None,
            "stop_loss_type": self.stop_loss_type.value if self.stop_loss_type else None,
            "has_stop_loss": self.has_stop_loss,
            "current_drawdown_pct": str(self.current_drawdown_pct) if self.current_drawdown_pct else None,
        }


@dataclass
class StopLossRecord:
    """Stop loss tracking record from database."""
    id: int
    symbol: str
    position_id: Optional[int]
    entry_price: Decimal
    quantity: Decimal
    stop_loss_price: Optional[Decimal]
    stop_loss_type: Optional[str]
    stop_loss_pct: Optional[Decimal]
    current_price: Optional[Decimal]
    current_drawdown_pct: Optional[Decimal]
    missing_stop_alert_sent: bool
    alert_count: int
    alert_escalation_level: str
    acknowledged: bool
    next_earnings_date: Optional[datetime]
    created_at: datetime
    updated_at: datetime


@dataclass
class Alert:
    """Urgent alert to be sent."""
    alert_type: AlertType
    severity: Severity
    symbol: str
    message: str
    position_id: Optional[int] = None
    details: Dict[str, Any] = field(default_factory=dict)

    # Suggested action
    suggested_stop_price: Optional[Decimal] = None
    suggested_action: Optional[str] = None

    def format_message(self) -> str:
        """Format alert message for delivery."""
        emoji = {
            Severity.INFO: "ℹ️",
            Severity.WARNING: "⚠️",
            Severity.URGENT: "🚨",
            Severity.CRITICAL: "🔴",
        }.get(self.severity, "")

        lines = [
            f"{emoji} {self.severity.value.upper()}: {self.alert_type.value.replace('_', ' ').title()}",
            f"Symbol: {self.symbol}",
            self.message,
        ]

        if self.details.get("entry_price"):
            lines.append(f"Entry: ${self.details['entry_price']}")
        if self.details.get("current_price"):
            lines.append(f"Current: ${self.details['current_price']}")
        if self.details.get("drawdown_pct"):
            lines.append(f"Drawdown: {self.details['drawdown_pct']:.1f}%")
        if self.suggested_stop_price:
            lines.append(f"Suggested Stop: ${self.suggested_stop_price}")
        if self.suggested_action:
            lines.append(f"Action: {self.suggested_action}")

        return "\n".join(lines)


@dataclass
class PositionSizeResult:
    """Result of position size calculation."""
    symbol: str
    entry_price: Decimal
    stop_price: Decimal
    account_balance: Decimal

    # Calculated values
    risk_per_share: Decimal
    max_shares: int
    dollar_risk: Decimal
    risk_pct: Decimal
    position_value: Decimal
    position_pct: Decimal

    # Validation
    is_valid: bool
    warnings: List[str] = field(default_factory=list)
    blocked_reason: Optional[str] = None

    @property
    def rr_ratio(self) -> Optional[Decimal]:
        """Calculate R:R ratio if target is provided."""
        # This would need target price to calculate
        return None

    def format_message(self) -> str:
        """Format position sizing result for display."""
        status = "✅ VALID" if self.is_valid else f"❌ BLOCKED: {self.blocked_reason}"

        lines = [
            f"Position Size Calculator - {self.symbol}",
            f"Status: {status}",
            f"",
            f"Entry: ${self.entry_price}",
            f"Stop: ${self.stop_price}",
            f"Risk/Share: ${self.risk_per_share}",
            f"",
            f"Max Shares: {self.max_shares}",
            f"Position Value: ${self.position_value:.2f}",
            f"Dollar Risk: ${self.dollar_risk:.2f}",
            f"Risk %: {self.risk_pct:.2f}%",
            f"Position %: {self.position_pct:.2f}%",
        ]

        if self.warnings:
            lines.append("")
            lines.append("Warnings:")
            for w in self.warnings:
                lines.append(f"  ⚠️ {w}")

        return "\n".join(lines)


@dataclass
class AccountState:
    """Current account state from Robinhood."""
    buying_power: Decimal
    cash: Decimal
    total_equity: Decimal
    updated_at: datetime

    @property
    def available_for_new_position(self) -> Decimal:
        """Amount available for a new position (use buying power)."""
        return self.buying_power
