"""Tests for Position Size Calculator."""

import pytest
from decimal import Decimal

from stop_loss_guardian.position_sizer import PositionSizer


class TestPositionSizer:
    """Test cases for position sizing logic."""

    def setup_method(self):
        """Set up test fixtures."""
        self.sizer = PositionSizer(
            max_risk_pct=Decimal("2.0"),
            max_position_pct=Decimal("20.0"),
        )

    def test_basic_calculation(self):
        """Test basic position size calculation."""
        result = self.sizer.calculate(
            symbol="AAPL",
            entry_price=Decimal("150.00"),
            stop_price=Decimal("140.00"),
            account_balance=Decimal("10000.00"),
        )

        # Max risk = $10,000 * 2% = $200
        # Risk per share = $150 - $140 = $10
        # Max shares by risk = $200 / $10 = 20
        # Position value = 20 * $150 = $3,000
        # Position % = $3,000 / $10,000 = 30% (exceeds 20% limit)
        # So max shares by position = $2,000 / $150 = 13.3 -> 13

        assert result.max_shares == 13
        assert result.is_valid == True
        assert result.risk_per_share == Decimal("10.00")

    def test_small_account_expensive_stock(self):
        """Test MOH scenario - expensive stock on small account."""
        # The MOH lesson: $180.97 stock on $888 account
        result = self.sizer.calculate(
            symbol="MOH",
            entry_price=Decimal("180.97"),
            stop_price=Decimal("162.87"),  # 10% stop
            account_balance=Decimal("888.00"),
        )

        # Max risk = $888 * 2% = $17.76
        # Risk per share = $180.97 - $162.87 = $18.10
        # Max shares by risk = $17.76 / $18.10 < 1
        # This should be blocked!

        assert result.max_shares == 0
        assert result.is_valid == False
        assert "too expensive" in result.blocked_reason.lower()

    def test_affordable_stock_small_account(self):
        """Test affordable stock for small account."""
        result = self.sizer.calculate(
            symbol="SOFI",
            entry_price=Decimal("12.50"),
            stop_price=Decimal("11.25"),  # 10% stop
            account_balance=Decimal("888.00"),
        )

        # Max risk = $888 * 2% = $17.76
        # Risk per share = $12.50 - $11.25 = $1.25
        # Max shares by risk = $17.76 / $1.25 = 14.2 -> 14

        # Max position = $888 * 20% = $177.60
        # Max shares by position = $177.60 / $12.50 = 14.2 -> 14

        assert result.max_shares == 14
        assert result.is_valid == True
        assert result.risk_pct <= Decimal("2.0")
        assert result.position_pct <= Decimal("20.0")

    def test_invalid_stop_above_entry(self):
        """Test that stop above entry is rejected."""
        result = self.sizer.calculate(
            symbol="TEST",
            entry_price=Decimal("100.00"),
            stop_price=Decimal("110.00"),  # Invalid: stop above entry
            account_balance=Decimal("10000.00"),
        )

        assert result.is_valid == False
        assert "below entry" in result.blocked_reason.lower()

    def test_very_tight_stop_warning(self):
        """Test warning for very tight stop."""
        result = self.sizer.calculate(
            symbol="TEST",
            entry_price=Decimal("100.00"),
            stop_price=Decimal("99.00"),  # 1% stop - very tight
            account_balance=Decimal("10000.00"),
        )

        assert result.is_valid == True
        assert any("tight" in w.lower() for w in result.warnings)

    def test_wide_stop_warning(self):
        """Test warning for wide stop."""
        result = self.sizer.calculate(
            symbol="TEST",
            entry_price=Decimal("100.00"),
            stop_price=Decimal("80.00"),  # 20% stop - wide
            account_balance=Decimal("10000.00"),
        )

        assert result.is_valid == True
        assert any("wide" in w.lower() for w in result.warnings)

    def test_suggested_stop_percentage(self):
        """Test suggested stop loss by percentage."""
        suggested = self.sizer.suggest_stop_loss(
            entry_price=Decimal("100.00"),
            method="percentage",
        )

        # Default is 10% stop loss
        assert suggested == Decimal("90.00")

    def test_suggested_stop_atr(self):
        """Test suggested stop loss by ATR."""
        suggested = self.sizer.suggest_stop_loss(
            entry_price=Decimal("100.00"),
            method="atr",
            atr=Decimal("2.50"),
            atr_multiplier=Decimal("2.0"),
        )

        # Stop = entry - (ATR * multiplier) = 100 - (2.50 * 2) = 95
        assert suggested == Decimal("95.00")

    def test_format_recommendation(self):
        """Test formatted recommendation output."""
        recommendation = self.sizer.format_recommendation(
            symbol="AAPL",
            entry_price=Decimal("150.00"),
            stop_price=Decimal("140.00"),
            account_balance=Decimal("10000.00"),
        )

        assert "AAPL" in recommendation
        assert "$150" in recommendation
        assert "$140" in recommendation
        assert "Max Shares:" in recommendation


class TestPositionSizerEdgeCases:
    """Edge case tests."""

    def test_zero_account_balance(self):
        """Test with zero account balance."""
        sizer = PositionSizer()
        result = sizer.calculate(
            symbol="TEST",
            entry_price=Decimal("100.00"),
            stop_price=Decimal("90.00"),
            account_balance=Decimal("0.00"),
        )

        assert result.max_shares == 0
        assert result.is_valid == False

    def test_negative_entry_price(self):
        """Test with negative entry price."""
        sizer = PositionSizer()
        result = sizer.calculate(
            symbol="TEST",
            entry_price=Decimal("-100.00"),
            stop_price=Decimal("-110.00"),
            account_balance=Decimal("10000.00"),
        )

        assert result.is_valid == False
        assert "positive" in result.blocked_reason.lower()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
