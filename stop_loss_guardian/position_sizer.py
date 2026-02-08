"""Position Size Calculator - Enforces 2% max risk per trade."""

import logging
from decimal import Decimal, ROUND_DOWN
from typing import Optional, List

from .config import settings
from .models import PositionSizeResult, AccountState

logger = logging.getLogger(__name__)


class PositionSizer:
    """Calculates position sizes based on risk management rules.

    Key rules:
    - Max 2% of account at risk per trade
    - Max 20% of account in a single position
    - Stocks over ~$90 are flagged as "too expensive" for small accounts
    """

    def __init__(
        self,
        max_risk_pct: Decimal = None,
        max_position_pct: Decimal = None,
    ):
        self.max_risk_pct = max_risk_pct or settings.max_risk_per_trade_pct
        self.max_position_pct = max_position_pct or settings.max_position_pct

    def calculate(
        self,
        symbol: str,
        entry_price: Decimal,
        stop_price: Decimal,
        account_balance: Decimal,
        target_price: Optional[Decimal] = None,
    ) -> PositionSizeResult:
        """Calculate optimal position size based on risk parameters.

        Args:
            symbol: Stock symbol
            entry_price: Planned entry price
            stop_price: Stop loss price
            account_balance: Total account equity
            target_price: Optional target price for R:R calculation

        Returns:
            PositionSizeResult with calculated values and validation
        """
        warnings: List[str] = []
        blocked_reason: Optional[str] = None

        # Validate inputs
        if entry_price <= 0:
            return PositionSizeResult(
                symbol=symbol,
                entry_price=entry_price,
                stop_price=stop_price,
                account_balance=account_balance,
                risk_per_share=Decimal("0"),
                max_shares=0,
                dollar_risk=Decimal("0"),
                risk_pct=Decimal("0"),
                position_value=Decimal("0"),
                position_pct=Decimal("0"),
                is_valid=False,
                blocked_reason="Entry price must be positive",
            )

        if stop_price <= 0:
            return PositionSizeResult(
                symbol=symbol,
                entry_price=entry_price,
                stop_price=stop_price,
                account_balance=account_balance,
                risk_per_share=Decimal("0"),
                max_shares=0,
                dollar_risk=Decimal("0"),
                risk_pct=Decimal("0"),
                position_value=Decimal("0"),
                position_pct=Decimal("0"),
                is_valid=False,
                blocked_reason="Stop price must be positive",
            )

        if stop_price >= entry_price:
            return PositionSizeResult(
                symbol=symbol,
                entry_price=entry_price,
                stop_price=stop_price,
                account_balance=account_balance,
                risk_per_share=Decimal("0"),
                max_shares=0,
                dollar_risk=Decimal("0"),
                risk_pct=Decimal("0"),
                position_value=Decimal("0"),
                position_pct=Decimal("0"),
                is_valid=False,
                blocked_reason="Stop price must be below entry price for long positions",
            )

        # Calculate risk per share
        risk_per_share = entry_price - stop_price

        # Calculate max dollar risk (2% of account)
        max_dollar_risk = account_balance * (self.max_risk_pct / 100)

        # Calculate max shares based on risk
        max_shares_by_risk = int((max_dollar_risk / risk_per_share).to_integral_value(rounding=ROUND_DOWN))

        # Calculate max shares based on position size limit (20% of account)
        max_position_value = account_balance * (self.max_position_pct / 100)
        max_shares_by_position = int((max_position_value / entry_price).to_integral_value(rounding=ROUND_DOWN))

        # Take the minimum
        max_shares = min(max_shares_by_risk, max_shares_by_position)

        # Calculate actual values
        if max_shares > 0:
            position_value = max_shares * entry_price
            dollar_risk = max_shares * risk_per_share
            risk_pct = (dollar_risk / account_balance) * 100
            position_pct = (position_value / account_balance) * 100
        else:
            position_value = Decimal("0")
            dollar_risk = Decimal("0")
            risk_pct = Decimal("0")
            position_pct = Decimal("0")

        # Validation checks
        is_valid = True

        # Check if stock is too expensive (can't even buy 1 share within risk limits)
        if max_shares == 0:
            is_valid = False
            blocked_reason = f"Stock too expensive for account size. Entry ${entry_price} requires minimum {int(max_position_value / entry_price) or 1} shares."
        elif max_shares < 2:
            warnings.append(f"Can only buy {max_shares} share(s) - limited diversification")

        # Check if risk is too high
        if risk_pct > self.max_risk_pct:
            is_valid = False
            blocked_reason = f"Risk {risk_pct:.1f}% exceeds max {self.max_risk_pct}%"

        # Check position concentration
        if position_pct > self.max_position_pct:
            is_valid = False
            blocked_reason = f"Position {position_pct:.1f}% exceeds max {self.max_position_pct}%"

        # Warning for tight stops
        stop_pct = ((entry_price - stop_price) / entry_price) * 100
        if stop_pct < 3:
            warnings.append(f"Very tight stop ({stop_pct:.1f}%) - may get stopped out by noise")
        elif stop_pct > 15:
            warnings.append(f"Wide stop ({stop_pct:.1f}%) - consider tighter risk management")

        # Calculate R:R if target provided
        rr_ratio = None
        if target_price and target_price > entry_price:
            reward = target_price - entry_price
            rr_ratio = reward / risk_per_share
            if rr_ratio < 2:
                warnings.append(f"R:R ratio {rr_ratio:.1f}:1 is below recommended 2:1")

        return PositionSizeResult(
            symbol=symbol,
            entry_price=entry_price,
            stop_price=stop_price,
            account_balance=account_balance,
            risk_per_share=risk_per_share,
            max_shares=max_shares,
            dollar_risk=dollar_risk,
            risk_pct=risk_pct,
            position_value=position_value,
            position_pct=position_pct,
            is_valid=is_valid,
            warnings=warnings,
            blocked_reason=blocked_reason,
        )

    def suggest_stop_loss(
        self,
        entry_price: Decimal,
        method: str = "percentage",
        atr: Optional[Decimal] = None,
        atr_multiplier: Decimal = Decimal("2.0"),
    ) -> Decimal:
        """Suggest a stop loss price.

        Args:
            entry_price: Entry price
            method: 'percentage' or 'atr'
            atr: Average True Range (required for ATR method)
            atr_multiplier: Multiplier for ATR method (default 2.0)

        Returns:
            Suggested stop loss price
        """
        if method == "atr" and atr:
            # ATR-based stop: entry - (ATR * multiplier)
            stop_distance = atr * atr_multiplier
            return entry_price - stop_distance
        else:
            # Percentage-based stop (default 10%)
            stop_pct = settings.default_stop_loss_pct / 100
            return entry_price * (1 - stop_pct)

    def format_recommendation(
        self,
        symbol: str,
        entry_price: Decimal,
        stop_price: Decimal,
        account_balance: Decimal,
        target_price: Optional[Decimal] = None,
    ) -> str:
        """Generate a formatted position size recommendation."""
        result = self.calculate(
            symbol=symbol,
            entry_price=entry_price,
            stop_price=stop_price,
            account_balance=account_balance,
            target_price=target_price,
        )

        return result.format_message()


# Singleton instance
position_sizer = PositionSizer()
