"""Tests for the main() entry point and signal handling.

All side-effecting calls (health server, metrics server, guardian) are
mocked so main() runs without touching real resources.
"""

import unittest.mock

import pytest

import stop_loss_guardian.main as main_mod


class TestSignalHandler:
    def test_calls_guardian_stop_when_set(self):
        fake = unittest.mock.MagicMock()
        with unittest.mock.patch.object(main_mod, "guardian", fake):
            main_mod.signal_handler(15, None)
            fake.stop.assert_called_once()

    def test_exits_when_no_guardian(self):
        with unittest.mock.patch.object(main_mod, "guardian", None):
            with pytest.raises(SystemExit):
                main_mod.signal_handler(15, None)


class TestHealthServer:
    def test_starts_daemon_thread(self):
        fake_server = unittest.mock.MagicMock()
        with unittest.mock.patch.object(
            main_mod, "HTTPServer", return_value=fake_server
        ), unittest.mock.patch.object(main_mod, "threading") as fake_threading:
            main_mod._start_health_server()
            fake_threading.Thread.assert_called_once()
            fake_threading.Thread.return_value.start.assert_called_once()


class TestMain:
    def _patch_common(self):
        return [
            unittest.mock.patch.object(main_mod, "_start_health_server"),
            unittest.mock.patch.object(main_mod, "start_metrics_server"),
            unittest.mock.patch.object(main_mod, "signal"),
        ]

    def test_main_happy_path(self):
        fake_guardian = unittest.mock.MagicMock()
        patches = self._patch_common()
        patches.append(unittest.mock.patch.object(
            main_mod, "StopLossGuardian", return_value=fake_guardian
        ))
        with patches[0], patches[1], patches[2], patches[3]:
            main_mod.main()
        fake_guardian.start.assert_called_once()
        fake_guardian.stop.assert_called_once()

    def test_main_keyboard_interrupt(self):
        fake_guardian = unittest.mock.MagicMock()
        fake_guardian.start.side_effect = KeyboardInterrupt()
        patches = self._patch_common()
        patches.append(unittest.mock.patch.object(
            main_mod, "StopLossGuardian", return_value=fake_guardian
        ))
        with patches[0], patches[1], patches[2], patches[3]:
            main_mod.main()  # should not raise
        fake_guardian.stop.assert_called_once()

    def test_main_fatal_error_exits(self):
        fake_guardian = unittest.mock.MagicMock()
        fake_guardian.start.side_effect = RuntimeError("boom")
        patches = self._patch_common()
        patches.append(unittest.mock.patch.object(
            main_mod, "StopLossGuardian", return_value=fake_guardian
        ))
        with patches[0], patches[1], patches[2], patches[3]:
            with pytest.raises(SystemExit):
                main_mod.main()
        fake_guardian.stop.assert_called_once()
