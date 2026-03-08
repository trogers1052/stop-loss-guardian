"""Tests for portfolio-level risk monitoring."""

import json
import unittest.mock
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from tests.conftest import make_position

# Import after conftest patches
from stop_loss_guardian.portfolio_monitor import (
    PortfolioMonitor,
    PortfolioDailyState,
    DAILY_STATE_KEY,
    _SYMBOL_TO_SECTOR,
)


@pytest.fixture
def mock_redis_client():
    """Mock RedisClient with a mock .client attribute for raw Redis ops."""
    r = unittest.mock.MagicMock()
    r.client = unittest.mock.MagicMock()
    r.get_all_stop_orders.return_value = {}
    r.get_account_state.return_value = unittest.mock.MagicMock(
        total_equity=Decimal("1000.00"),
    )
    return r


@pytest.fixture
def mock_telegram_client():
    tg = unittest.mock.MagicMock()
    tg.send_alert.return_value = True
    return tg


@pytest.fixture
def monitor(mock_redis_client, mock_telegram_client):
    return PortfolioMonitor(
        redis_client=mock_redis_client,
        telegram_client=mock_telegram_client,
    )


# ------------------------------------------------------------------
# PortfolioDailyState
# ------------------------------------------------------------------


class TestPortfolioDailyState:
    def test_to_dict_basic(self):
        state = PortfolioDailyState(trade_date="2026-03-07")
        d = state.to_dict()
        assert d["date"] == "2026-03-07"
        assert d["stops_hit_today"] == 0
        assert d["halted"] is False
        assert d["actual_portfolio_heat"] == 0.0

    def test_to_dict_with_values(self):
        state = PortfolioDailyState(
            trade_date="2026-03-07",
            stops_hit_today=2,
            stops_hit_symbols=["CCJ", "URNM"],
            daily_pnl_pct=-0.038,
            actual_portfolio_heat=0.062,
            halted=True,
            halt_reason="2 stops hit",
        )
        d = state.to_dict()
        assert d["stops_hit_today"] == 2
        assert d["stops_hit_symbols"] == ["CCJ", "URNM"]
        assert d["halted"] is True
        assert d["halt_reason"] == "2 stops hit"


# ------------------------------------------------------------------
# Stop detection
# ------------------------------------------------------------------


class TestStopDetection:
    def test_first_scan_no_detection(self, monitor):
        """First scan has no previous state to compare."""
        positions = [make_position(symbol="AAPL")]
        monitor._detect_hit_stops({p.symbol for p in positions})
        assert monitor._stops_hit_today == []

    def test_detect_disappeared_position(self, monitor):
        """Position disappearing between scans = stop hit."""
        monitor._previous_symbols = {"AAPL", "CCJ"}
        current = {"AAPL"}
        monitor._detect_hit_stops(current)
        assert "CCJ" in monitor._stops_hit_today

    def test_no_duplicate_stops(self, monitor):
        """Same symbol disappearing again doesn't double-count."""
        monitor._previous_symbols = {"AAPL"}
        monitor._stops_hit_today = ["CCJ"]
        monitor._detect_hit_stops({"AAPL"})
        assert monitor._stops_hit_today == ["CCJ"]

    def test_multiple_stops_same_scan(self, monitor):
        """Two positions disappearing in one scan."""
        monitor._previous_symbols = {"AAPL", "CCJ", "URNM"}
        monitor._detect_hit_stops({"AAPL"})
        assert "CCJ" in monitor._stops_hit_today
        assert "URNM" in monitor._stops_hit_today
        assert len(monitor._stops_hit_today) == 2


# ------------------------------------------------------------------
# Heat calculation
# ------------------------------------------------------------------


class TestHeatCalculation:
    def test_heat_with_stop(self, monitor, mock_redis_client):
        """Heat calculated from actual stop distance."""
        pos = make_position(
            symbol="CCJ",
            entry_price=Decimal("50.00"),
            current_price=Decimal("48.00"),
            quantity=Decimal("10"),
            stop_loss_price=Decimal("47.00"),
        )
        mock_redis_client.get_all_stop_orders.return_value = {}

        risks, sector_heat, total = monitor._calculate_actual_heat([pos], {})
        # Risk = (50 - 47) / 50 = 6%, dollar risk = 10 * 3 = 30
        # Account = 1000, risk_pct = 30/1000 = 0.03
        assert "CCJ" in risks
        assert abs(risks["CCJ"]["risk_pct"] - 0.03) < 0.001
        assert abs(risks["CCJ"]["stop_distance_pct"] - 0.06) < 0.001
        assert abs(total - 0.03) < 0.001

    def test_heat_without_stop_uses_default(self, monitor, mock_redis_client):
        """No stop = use default 10% stop distance."""
        pos = make_position(
            symbol="AAPL",
            entry_price=Decimal("100.00"),
            current_price=Decimal("98.00"),
            quantity=Decimal("5"),
        )
        risks, _, total = monitor._calculate_actual_heat([pos], {})
        # Default 10% stop: dollar risk = 5 * 100 * 0.10 = 50
        # risk_pct = 50/1000 = 0.05
        assert abs(risks["AAPL"]["risk_pct"] - 0.05) < 0.001
        assert risks["AAPL"]["has_stop"] is False

    def test_heat_prefers_robinhood_stop(self, monitor, mock_redis_client):
        """Robinhood stop order takes priority over tracking stop."""
        pos = make_position(
            symbol="CCJ",
            entry_price=Decimal("50.00"),
            current_price=Decimal("48.00"),
            quantity=Decimal("10"),
            stop_loss_price=Decimal("45.00"),  # tracking says $45
        )
        stop_orders = {"CCJ": {"stop_price": "47.00"}}  # Robinhood says $47

        risks, _, total = monitor._calculate_actual_heat([pos], stop_orders)
        # Should use $47 (Robinhood), not $45 (tracking)
        assert abs(risks["CCJ"]["stop_distance_pct"] - 0.06) < 0.001

    def test_sector_heat_aggregation(self, monitor, mock_redis_client):
        """Sector heat sums risk from all positions in same sector."""
        ccj = make_position(
            symbol="CCJ", entry_price=Decimal("50.00"),
            current_price=Decimal("48.00"), quantity=Decimal("10"),
            stop_loss_price=Decimal("47.00"),
        )
        urnm = make_position(
            symbol="URNM", entry_price=Decimal("30.00"),
            current_price=Decimal("29.00"), quantity=Decimal("10"),
            stop_loss_price=Decimal("28.00"),
        )

        risks, sector_heat, _ = monitor._calculate_actual_heat([ccj, urnm], {})
        assert "uranium" in sector_heat
        # CCJ: (50-47)/50 * 10shares = 30, 30/1000 = 0.03
        # URNM: (30-28)/30 * 10shares = 20/30*10 ≈ 6.67, 6.67/1000 ≈ 0.02
        ccj_risk = risks["CCJ"]["risk_pct"]
        urnm_risk = risks["URNM"]["risk_pct"]
        assert abs(sector_heat["uranium"] - (ccj_risk + urnm_risk)) < 0.001


# ------------------------------------------------------------------
# Daily P&L
# ------------------------------------------------------------------


class TestDailyPnL:
    def test_pnl_calculated(self, monitor, mock_redis_client):
        """P&L calculated from opening vs current equity."""
        mock_redis_client.client.get.side_effect = [
            json.dumps({"equity": 1000.0, "date": "2026-03-07"}),
            json.dumps({"total_equity": 960.0}),
        ]
        pnl_pct, pnl_dollars, equity = monitor._calculate_daily_pnl()
        assert abs(pnl_pct - (-0.04)) < 0.001
        assert abs(pnl_dollars - (-40.0)) < 0.01
        assert abs(equity - 960.0) < 0.01

    def test_pnl_missing_data(self, monitor, mock_redis_client):
        """Missing Redis data returns zeros."""
        mock_redis_client.client.get.return_value = None
        pnl_pct, pnl_dollars, equity = monitor._calculate_daily_pnl()
        assert pnl_pct == 0.0
        assert pnl_dollars == 0.0

    def test_pnl_no_redis(self, monitor, mock_redis_client):
        """No Redis client returns zeros."""
        mock_redis_client.client = None
        pnl_pct, pnl_dollars, equity = monitor._calculate_daily_pnl()
        assert pnl_pct == 0.0


# ------------------------------------------------------------------
# Gap risk detection
# ------------------------------------------------------------------


class TestGapRisk:
    def test_gap_detected(self, monitor):
        """Price below stop = gap risk."""
        pos = make_position(
            symbol="CCJ",
            entry_price=Decimal("50.00"),
            current_price=Decimal("44.00"),
            stop_loss_price=Decimal("47.00"),
        )
        gaps = monitor._detect_gap_risk([pos], {})
        assert len(gaps) == 1
        assert gaps[0]["symbol"] == "CCJ"
        assert gaps[0]["current_price"] == 44.0
        assert gaps[0]["stop_price"] == 47.0

    def test_no_gap_above_stop(self, monitor):
        """Price above stop = no gap risk."""
        pos = make_position(
            symbol="CCJ",
            entry_price=Decimal("50.00"),
            current_price=Decimal("48.00"),
            stop_loss_price=Decimal("47.00"),
        )
        gaps = monitor._detect_gap_risk([pos], {})
        assert len(gaps) == 0

    def test_gap_uses_robinhood_stop(self, monitor):
        """Gap detection uses Robinhood stop order if available."""
        pos = make_position(
            symbol="CCJ",
            entry_price=Decimal("50.00"),
            current_price=Decimal("46.00"),
        )
        stop_orders = {"CCJ": {"stop_price": "47.00"}}
        gaps = monitor._detect_gap_risk([pos], stop_orders)
        assert len(gaps) == 1


# ------------------------------------------------------------------
# Halt evaluation
# ------------------------------------------------------------------


class TestHaltEvaluation:
    def test_not_halted_normal(self, monitor):
        halted, reason = monitor._evaluate_halt(
            daily_pnl_pct=-0.02, actual_heat=0.04, stops_hit=1
        )
        assert halted is False
        assert reason is None

    def test_halted_too_many_stops(self, monitor):
        halted, reason = monitor._evaluate_halt(
            daily_pnl_pct=-0.02, actual_heat=0.04, stops_hit=3
        )
        assert halted is True
        assert "3 stops hit" in reason

    def test_halted_daily_loss(self, monitor):
        halted, reason = monitor._evaluate_halt(
            daily_pnl_pct=-0.07, actual_heat=0.04, stops_hit=0
        )
        assert halted is True
        assert "daily loss" in reason

    def test_halted_excessive_heat(self, monitor):
        halted, reason = monitor._evaluate_halt(
            daily_pnl_pct=-0.02, actual_heat=0.15, stops_hit=0
        )
        assert halted is True
        assert "actual heat" in reason

    def test_multiple_halt_reasons(self, monitor):
        halted, reason = monitor._evaluate_halt(
            daily_pnl_pct=-0.08, actual_heat=0.15, stops_hit=4
        )
        assert halted is True
        assert "stops hit" in reason
        assert "daily loss" in reason
        assert "actual heat" in reason


# ------------------------------------------------------------------
# Redis state write
# ------------------------------------------------------------------


class TestStateWrite:
    def test_writes_to_redis(self, monitor, mock_redis_client):
        state = PortfolioDailyState(trade_date="2026-03-07")
        monitor._write_state(state)
        mock_redis_client.client.setex.assert_called_once()
        args = mock_redis_client.client.setex.call_args
        assert args[0][0] == DAILY_STATE_KEY
        data = json.loads(args[0][2])
        assert data["date"] == "2026-03-07"

    def test_write_no_redis(self, monitor, mock_redis_client):
        """Gracefully handles no Redis."""
        mock_redis_client.client = None
        state = PortfolioDailyState(trade_date="2026-03-07")
        monitor._write_state(state)  # should not raise


# ------------------------------------------------------------------
# Alert thresholds
# ------------------------------------------------------------------


class TestAlerts:
    def test_halt_alert_sent_once(self, monitor, mock_telegram_client):
        state = PortfolioDailyState(
            trade_date="2026-03-07",
            halted=True,
            halt_reason="test",
            daily_pnl_pct=-0.08,
            stops_hit_today=3,
            stops_hit_symbols=["A", "B", "C"],
            actual_portfolio_heat=0.10,
        )
        monitor._check_thresholds_and_alert(state)
        assert monitor._halt_alert_sent_today is True
        assert mock_telegram_client.send_alert.called

        # Second call should NOT send again
        mock_telegram_client.send_alert.reset_mock()
        monitor._check_thresholds_and_alert(state)
        # Only gap alerts might fire, but halt alert should not re-fire
        halt_calls = [
            c for c in mock_telegram_client.send_alert.call_args_list
            if "CIRCUIT BREAKER" in str(c)
        ]
        assert len(halt_calls) == 0

    def test_gap_alert(self, monitor, mock_telegram_client):
        state = PortfolioDailyState(
            trade_date="2026-03-07",
            gap_alerts=[{
                "symbol": "CCJ",
                "stop_price": 47.0,
                "current_price": 44.0,
                "planned_loss_pct": -0.06,
                "actual_loss_pct": -0.12,
                "excess_loss_pct": -0.06,
            }],
        )
        monitor._check_thresholds_and_alert(state)
        assert mock_telegram_client.send_alert.called
        msg = mock_telegram_client.send_alert.call_args[0][0]
        assert "GAP RISK" in msg
        assert "CCJ" in msg


# ------------------------------------------------------------------
# Full check cycle
# ------------------------------------------------------------------


class TestFullCheck:
    def test_check_writes_state(self, monitor, mock_redis_client):
        """Full check cycle produces state and writes to Redis."""
        mock_redis_client.client.get.return_value = None  # no P&L data
        positions = [
            make_position(
                symbol="CCJ",
                entry_price=Decimal("50.00"),
                current_price=Decimal("48.00"),
                quantity=Decimal("10"),
                stop_loss_price=Decimal("47.00"),
            )
        ]
        state = monitor.check(positions)
        assert state is not None
        assert state.open_position_count == 1
        assert state.actual_portfolio_heat > 0
        mock_redis_client.client.setex.assert_called()

    def test_day_reset(self, monitor, mock_redis_client):
        """New day resets stops counter."""
        monitor._last_date = "2026-03-06"
        monitor._stops_hit_today = ["CCJ"]
        mock_redis_client.client.get.return_value = None
        positions = [make_position(symbol="AAPL")]
        monitor.check(positions)
        assert monitor._stops_hit_today == []
