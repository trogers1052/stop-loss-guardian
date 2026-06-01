"""Tests for StopLossGuardian lifecycle: __init__, start, stop, monitoring loop."""

import contextlib
import threading
import unittest.mock
from datetime import datetime, timezone

import pytest

import stop_loss_guardian.config as _cfg_module
from stop_loss_guardian.guardian import StopLossGuardian


_SENTINEL = object()


@contextlib.contextmanager
def _settings_overrides(**overrides):
    """Temporarily set attributes on the settings object guardian.py uses.

    guardian.py binds ``settings`` by value at import time, so we must
    target that exact object rather than the (possibly re-bound) one on
    the config module.
    """
    import stop_loss_guardian.guardian as _g
    s = _g.settings
    old = {k: getattr(s, k, _SENTINEL) for k in overrides}
    for k, v in overrides.items():
        setattr(s, k, v)
    try:
        yield s
    finally:
        for k, v in old.items():
            if v is _SENTINEL:
                try:
                    delattr(s, k)
                except AttributeError:
                    pass
            else:
                setattr(s, k, v)


class TestInit:
    def test_constructs_dependencies(self):
        g = StopLossGuardian()
        assert g.repo is not None
        assert g.redis is not None
        assert g.twilio is not None
        assert g.telegram is not None
        assert g.position_sizer is not None
        assert g.dispatcher is None
        assert g.portfolio_monitor is None
        assert g._running is False
        assert g._critical_drawdown_cooldowns == {}
        assert g._earnings_alert_cooldowns == {}


class TestStart:
    def _bare_guardian(self):
        g = StopLossGuardian.__new__(StopLossGuardian)
        g.repo = unittest.mock.MagicMock()
        g.redis = unittest.mock.MagicMock()
        g.twilio = unittest.mock.MagicMock()
        g.telegram = unittest.mock.MagicMock()
        g.position_sizer = unittest.mock.MagicMock()
        g.dispatcher = None
        g.portfolio_monitor = None
        g._running = False
        g._stop_event = threading.Event()
        g._critical_drawdown_cooldowns = {}
        g._earnings_alert_cooldowns = {}
        return g

    def test_start_connects_and_restores_cooldowns(self):
        g = self._bare_guardian()
        ts = datetime(2026, 1, 1, tzinfo=timezone.utc)
        g.redis.get_drawdown_cooldowns.return_value = {"AAPL": ts}
        with _settings_overrides(twilio_enabled=False, portfolio_monitor_enabled=False):
            with unittest.mock.patch.object(g, "_run_monitoring_loop") as loop:
                g.start()
            g.repo.connect.assert_called_once()
            g.redis.connect.assert_called_once()
            assert g._critical_drawdown_cooldowns["AAPL"] == ts
            assert g.dispatcher is not None
            assert g.portfolio_monitor is None
            loop.assert_called_once()
            assert g._running is True

    def test_start_enables_twilio_and_portfolio_monitor(self):
        g = self._bare_guardian()
        g.redis.get_drawdown_cooldowns.return_value = {}
        with _settings_overrides(twilio_enabled=True, portfolio_monitor_enabled=True):
            with unittest.mock.patch.object(g, "_run_monitoring_loop"):
                with unittest.mock.patch(
                    "stop_loss_guardian.guardian.PortfolioMonitor"
                ) as PM:
                    g.start()
            g.twilio.connect.assert_called_once()
            PM.assert_called_once()
            assert g.portfolio_monitor is not None


class TestStop:
    def _running_guardian(self):
        g = StopLossGuardian.__new__(StopLossGuardian)
        g.repo = unittest.mock.MagicMock()
        g.repo.conn = unittest.mock.MagicMock()
        g.redis = unittest.mock.MagicMock()
        g.redis.client = unittest.mock.MagicMock()
        g._running = True
        g._stop_event = threading.Event()
        return g

    def test_stop_closes_resources(self):
        g = self._running_guardian()
        g.stop()
        assert g._running is False
        assert g._stop_event.is_set()
        g.repo.close.assert_called_once()
        g.redis.close.assert_called_once()

    def test_stop_idempotent_when_already_stopped(self):
        g = StopLossGuardian.__new__(StopLossGuardian)
        g.repo = unittest.mock.MagicMock()
        g.repo.conn = None
        g.redis = unittest.mock.MagicMock()
        g.redis.client = None
        g._running = False
        g._stop_event = threading.Event()
        g.stop()  # early-return branch
        g.repo.close.assert_not_called()


class TestMonitoringLoop:
    def _loop_guardian(self):
        g = StopLossGuardian.__new__(StopLossGuardian)
        g.repo = unittest.mock.MagicMock()
        g.redis = unittest.mock.MagicMock()
        g.telegram = unittest.mock.MagicMock()
        g._running = True
        g._stop_event = threading.Event()
        return g

    def test_loop_runs_one_cycle_then_stops(self):
        g = self._loop_guardian()

        def stop_after(*a, **kw):
            g._running = False
            return True  # _stop_event.wait returns True -> break

        with unittest.mock.patch.object(g, "_check_all_positions") as check, \
             unittest.mock.patch.object(g._stop_event, "wait", side_effect=stop_after):
            g._run_monitoring_loop()
        check.assert_called_once()
        g.repo.ensure_connected.assert_called_once()

    def test_loop_handles_error_and_escalates(self):
        g = self._loop_guardian()
        # Raise enough errors to hit the alert threshold, then stop.
        call_count = {"n": 0}

        def failing_check():
            call_count["n"] += 1
            raise RuntimeError("cycle fail")

        wait_calls = {"n": 0}

        def wait(*a, **kw):
            wait_calls["n"] += 1
            # stop once we've reached the escalation threshold
            if call_count["n"] >= StopLossGuardian._ERROR_ALERT_THRESHOLD:
                g._running = False
                return True
            return False

        with unittest.mock.patch.object(g, "_check_all_positions", side_effect=failing_check), \
             unittest.mock.patch.object(g._stop_event, "wait", side_effect=wait):
            g._run_monitoring_loop()
        # Degraded alert sent once at threshold
        g.telegram.send_alert.assert_called_once()

    def test_loop_alert_send_failure_is_caught(self):
        g = self._loop_guardian()
        g.telegram.send_alert.side_effect = RuntimeError("telegram down")

        def failing_check():
            raise RuntimeError("cycle fail")

        n = {"i": 0}

        def wait(*a, **kw):
            n["i"] += 1
            if n["i"] >= StopLossGuardian._ERROR_ALERT_THRESHOLD:
                g._running = False
                return True
            return False

        with unittest.mock.patch.object(g, "_check_all_positions", side_effect=failing_check), \
             unittest.mock.patch.object(g._stop_event, "wait", side_effect=wait):
            g._run_monitoring_loop()  # must not raise
