"""Tests for the database Repository.

All psycopg2 I/O is mocked at the connection/cursor level — no real DB.
The cursor is used as a context manager throughout the repository, so the
mock cursor must support the context-manager protocol.
"""

import unittest.mock
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from stop_loss_guardian.db.repository import Repository
from stop_loss_guardian.models import (
    Alert, AlertType, Severity, AlertChannel,
)


def _make_cursor(fetchone=None, fetchall=None, rowcount=0):
    """Build a MagicMock cursor that works as a context manager."""
    cur = unittest.mock.MagicMock()
    cur.fetchone.return_value = fetchone
    cur.fetchall.return_value = fetchall if fetchall is not None else []
    cur.rowcount = rowcount
    cur.__enter__.return_value = cur
    cur.__exit__.return_value = False
    return cur


@pytest.fixture
def repo():
    """Repository with a fully-mocked connection."""
    r = Repository()
    r.conn = unittest.mock.MagicMock()
    r.conn.closed = False
    r._pool = unittest.mock.MagicMock()
    return r


def _attach_cursor(repo, cur):
    """Make repo.conn.cursor(...) return the given cursor regardless of kwargs."""
    repo.conn.cursor.return_value = cur


# ---------------------------------------------------------------------------
# connect / ensure_connected / close
# ---------------------------------------------------------------------------

class TestConnectionLifecycle:
    def test_connect_acquires_pool_connection(self):
        import psycopg2.pool as pg_pool
        r = Repository()
        fake_conn = unittest.mock.MagicMock()
        fake_pool = unittest.mock.MagicMock()
        fake_pool.getconn.return_value = fake_conn
        with unittest.mock.patch.object(
            pg_pool, "SimpleConnectionPool", return_value=fake_pool, create=True
        ):
            r.connect()
        assert r.conn is fake_conn
        assert r.conn.autocommit is False
        fake_pool.getconn.assert_called_once()

    def test_connect_raises_on_failure(self):
        import psycopg2.pool as pg_pool
        r = Repository()
        with unittest.mock.patch.object(
            pg_pool, "SimpleConnectionPool",
            side_effect=RuntimeError("boom"), create=True,
        ):
            with pytest.raises(RuntimeError):
                r.connect()

    def test_ensure_connected_replaces_closed_connection(self, repo):
        repo.conn.closed = True
        new_conn = unittest.mock.MagicMock()
        repo._pool.getconn.return_value = new_conn
        repo.ensure_connected()
        assert repo.conn is new_conn
        assert repo.conn.autocommit is False

    def test_ensure_connected_reconnects_when_none(self, repo):
        repo.conn = None
        new_conn = unittest.mock.MagicMock()
        repo._pool.getconn.return_value = new_conn
        repo.ensure_connected()
        assert repo.conn is new_conn

    def test_ensure_connected_pings_live_connection(self, repo):
        cur = _make_cursor()
        _attach_cursor(repo, cur)
        repo.ensure_connected()
        cur.execute.assert_called_once_with("SELECT 1")

    def test_ensure_connected_recovers_from_ping_failure(self, repo):
        # cursor() raises -> triggers reconnect branch
        repo.conn.cursor.side_effect = RuntimeError("dead connection")
        new_conn = unittest.mock.MagicMock()
        repo._pool.getconn.return_value = new_conn
        repo.ensure_connected()
        assert repo.conn is new_conn

    def test_ensure_connected_no_pool_calls_connect(self, repo):
        repo.conn = None
        repo._pool = None
        with unittest.mock.patch.object(repo, "connect") as mock_connect:
            repo.ensure_connected()
            mock_connect.assert_called_once()

    def test_close_returns_connection_and_closes_pool(self, repo):
        pool = repo._pool
        conn = repo.conn
        repo.close()
        pool.putconn.assert_called_once_with(conn)
        pool.closeall.assert_called_once()
        assert repo.conn is None
        assert repo._pool is None

    def test_close_is_safe_without_pool(self):
        r = Repository()
        r.close()  # no exception
        assert r._pool is None


# ---------------------------------------------------------------------------
# get_open_positions
# ---------------------------------------------------------------------------

class TestGetOpenPositions:
    def test_returns_positions(self, repo):
        rows = [
            {
                "position_id": 7,
                "symbol": "AAPL",
                "entry_price": "100.5",
                "quantity": "10",
                "entry_date": datetime(2026, 1, 1, tzinfo=timezone.utc),
                "status": "open",
            }
        ]
        cur = _make_cursor(fetchall=rows)
        _attach_cursor(repo, cur)
        result = repo.get_open_positions()
        assert len(result) == 1
        assert result[0].symbol == "AAPL"
        assert result[0].entry_price == Decimal("100.5")
        assert result[0].position_id == 7

    def test_empty(self, repo):
        cur = _make_cursor(fetchall=[])
        _attach_cursor(repo, cur)
        assert repo.get_open_positions() == []

    def test_error_rolls_back_and_raises(self, repo):
        cur = _make_cursor()
        cur.execute.side_effect = RuntimeError("query failed")
        _attach_cursor(repo, cur)
        with pytest.raises(RuntimeError):
            repo.get_open_positions()
        repo.conn.rollback.assert_called_once()


# ---------------------------------------------------------------------------
# get_stop_loss_tracking
# ---------------------------------------------------------------------------

class TestGetStopLossTracking:
    def _row(self, **overrides):
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        base = {
            "id": 1,
            "symbol": "AAPL",
            "position_id": 5,
            "entry_price": "100",
            "quantity": "10",
            "stop_loss_price": "90",
            "stop_loss_type": "stop_limit",
            "stop_loss_pct": "10",
            "current_price": "95",
            "current_drawdown_pct": "-5",
            "missing_stop_alert_sent": False,
            "alert_count": 0,
            "alert_escalation_level": "none",
            "acknowledged": False,
            "next_earnings_date": None,
            "created_at": now,
            "updated_at": now,
        }
        base.update(overrides)
        return base

    def test_found_full_row(self, repo):
        cur = _make_cursor(fetchone=self._row())
        _attach_cursor(repo, cur)
        rec = repo.get_stop_loss_tracking("AAPL")
        assert rec.symbol == "AAPL"
        assert rec.stop_loss_price == Decimal("90")
        assert rec.current_drawdown_pct == Decimal("-5")

    def test_found_with_null_optional_fields(self, repo):
        cur = _make_cursor(fetchone=self._row(
            stop_loss_price=None, stop_loss_pct=None,
            current_price=None, current_drawdown_pct=None,
        ))
        _attach_cursor(repo, cur)
        rec = repo.get_stop_loss_tracking("AAPL")
        assert rec.stop_loss_price is None
        assert rec.current_price is None

    def test_not_found(self, repo):
        cur = _make_cursor(fetchone=None)
        _attach_cursor(repo, cur)
        assert repo.get_stop_loss_tracking("AAPL") is None

    def test_error_raises(self, repo):
        cur = _make_cursor()
        cur.execute.side_effect = RuntimeError("fail")
        _attach_cursor(repo, cur)
        with pytest.raises(RuntimeError):
            repo.get_stop_loss_tracking("AAPL")
        repo.conn.rollback.assert_called_once()


# ---------------------------------------------------------------------------
# upsert / update / mark / acknowledge
# ---------------------------------------------------------------------------

class TestUpsertStopLossTracking:
    def test_returns_id(self, repo):
        cur = _make_cursor(fetchone=(42,))
        _attach_cursor(repo, cur)
        result = repo.upsert_stop_loss_tracking(
            symbol="AAPL",
            position_id=1,
            entry_price=Decimal("100"),
            quantity=Decimal("10"),
            stop_loss_price=Decimal("90"),
            stop_loss_type="stop",
            stop_loss_pct=Decimal("10"),
            current_price=Decimal("95"),
            current_drawdown_pct=Decimal("-5"),
        )
        assert result == 42
        repo.conn.commit.assert_called_once()

    def test_returns_zero_when_no_row(self, repo):
        cur = _make_cursor(fetchone=None)
        _attach_cursor(repo, cur)
        result = repo.upsert_stop_loss_tracking(
            symbol="AAPL", position_id=None,
            entry_price=Decimal("100"), quantity=Decimal("10"),
        )
        assert result == 0

    def test_error_raises(self, repo):
        cur = _make_cursor()
        cur.execute.side_effect = RuntimeError("fail")
        _attach_cursor(repo, cur)
        with pytest.raises(RuntimeError):
            repo.upsert_stop_loss_tracking(
                symbol="AAPL", position_id=1,
                entry_price=Decimal("100"), quantity=Decimal("10"),
            )
        repo.conn.rollback.assert_called_once()


class TestUpdateStopLoss:
    def test_commits(self, repo):
        cur = _make_cursor()
        _attach_cursor(repo, cur)
        repo.update_stop_loss("AAPL", Decimal("90"), "stop_limit", Decimal("10"))
        repo.conn.commit.assert_called_once()
        cur.execute.assert_called_once()

    def test_none_pct(self, repo):
        cur = _make_cursor()
        _attach_cursor(repo, cur)
        repo.update_stop_loss("AAPL", Decimal("90"), "stop")
        repo.conn.commit.assert_called_once()

    def test_error_raises(self, repo):
        cur = _make_cursor()
        cur.execute.side_effect = RuntimeError("fail")
        _attach_cursor(repo, cur)
        with pytest.raises(RuntimeError):
            repo.update_stop_loss("AAPL", Decimal("90"), "stop")
        repo.conn.rollback.assert_called_once()


class TestMarkAlertSent:
    def test_commits(self, repo):
        cur = _make_cursor()
        _attach_cursor(repo, cur)
        repo.mark_alert_sent("AAPL", "telegram")
        repo.conn.commit.assert_called_once()

    def test_error_raises(self, repo):
        cur = _make_cursor()
        cur.execute.side_effect = RuntimeError("fail")
        _attach_cursor(repo, cur)
        with pytest.raises(RuntimeError):
            repo.mark_alert_sent("AAPL", "telegram")
        repo.conn.rollback.assert_called_once()


class TestAcknowledgeAlert:
    def test_commits(self, repo):
        cur = _make_cursor()
        _attach_cursor(repo, cur)
        repo.acknowledge_alert("AAPL", reason="manual")
        repo.conn.commit.assert_called_once()

    def test_no_reason(self, repo):
        cur = _make_cursor()
        _attach_cursor(repo, cur)
        repo.acknowledge_alert("AAPL")
        repo.conn.commit.assert_called_once()

    def test_error_raises(self, repo):
        cur = _make_cursor()
        cur.execute.side_effect = RuntimeError("fail")
        _attach_cursor(repo, cur)
        with pytest.raises(RuntimeError):
            repo.acknowledge_alert("AAPL")
        repo.conn.rollback.assert_called_once()


# ---------------------------------------------------------------------------
# log_urgent_alert
# ---------------------------------------------------------------------------

class TestLogUrgentAlert:
    def _alert(self):
        return Alert(
            alert_type=AlertType.MISSING_STOP_LOSS,
            severity=Severity.CRITICAL,
            symbol="AAPL",
            message="No stop loss!",
            position_id=3,
            details={"foo": "bar"},
        )

    def test_returns_id(self, repo):
        cur = _make_cursor(fetchone=(99,))
        _attach_cursor(repo, cur)
        result = repo.log_urgent_alert(
            self._alert(), AlertChannel.TELEGRAM, escalation_level=1,
            stop_loss_tracking_id=5, twilio_sid="SM123",
        )
        assert result == 99
        repo.conn.commit.assert_called_once()

    def test_returns_zero_when_no_row(self, repo):
        cur = _make_cursor(fetchone=None)
        _attach_cursor(repo, cur)
        result = repo.log_urgent_alert(
            self._alert(), AlertChannel.SMS, escalation_level=2,
        )
        assert result == 0

    def test_error_raises(self, repo):
        cur = _make_cursor()
        cur.execute.side_effect = RuntimeError("fail")
        _attach_cursor(repo, cur)
        with pytest.raises(RuntimeError):
            repo.log_urgent_alert(self._alert(), AlertChannel.TELEGRAM, 1)
        repo.conn.rollback.assert_called_once()


# ---------------------------------------------------------------------------
# queries returning raw dict rows
# ---------------------------------------------------------------------------

class TestQueriesReturningRows:
    def test_positions_without_stop_loss(self, repo):
        rows = [{"symbol": "AAPL"}, {"symbol": "TSLA"}]
        cur = _make_cursor(fetchall=rows)
        _attach_cursor(repo, cur)
        assert repo.get_positions_without_stop_loss() == rows

    def test_positions_without_stop_loss_error(self, repo):
        cur = _make_cursor()
        cur.execute.side_effect = RuntimeError("fail")
        _attach_cursor(repo, cur)
        with pytest.raises(RuntimeError):
            repo.get_positions_without_stop_loss()
        repo.conn.rollback.assert_called_once()

    def test_positions_with_critical_drawdown(self, repo):
        rows = [{"symbol": "AAPL", "current_drawdown_pct": "-12"}]
        cur = _make_cursor(fetchall=rows)
        _attach_cursor(repo, cur)
        result = repo.get_positions_with_critical_drawdown(Decimal("10"))
        assert result == rows

    def test_positions_with_critical_drawdown_error(self, repo):
        cur = _make_cursor()
        cur.execute.side_effect = RuntimeError("fail")
        _attach_cursor(repo, cur)
        with pytest.raises(RuntimeError):
            repo.get_positions_with_critical_drawdown(Decimal("10"))
        repo.conn.rollback.assert_called_once()


# ---------------------------------------------------------------------------
# cleanup_closed_positions
# ---------------------------------------------------------------------------

class TestCleanupClosedPositions:
    def test_logs_and_commits(self, repo):
        cur = _make_cursor()
        # rowcount differs per execute call (alerts then tracking)
        type(cur).rowcount = unittest.mock.PropertyMock(side_effect=[2, 3])
        _attach_cursor(repo, cur)
        repo.cleanup_closed_positions()
        assert cur.execute.call_count == 2
        repo.conn.commit.assert_called_once()

    def test_zero_rows_still_commits(self, repo):
        cur = _make_cursor(rowcount=0)
        _attach_cursor(repo, cur)
        repo.cleanup_closed_positions()
        repo.conn.commit.assert_called_once()

    def test_error_is_swallowed_after_rollback(self, repo):
        # cleanup_closed_positions logs the error but does NOT re-raise.
        cur = _make_cursor()
        cur.execute.side_effect = RuntimeError("fail")
        _attach_cursor(repo, cur)
        repo.cleanup_closed_positions()  # no exception propagates
        repo.conn.rollback.assert_called_once()
