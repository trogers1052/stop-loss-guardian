"""Tests for the Prometheus metrics module.

prometheus_client is installed in the test environment, so _init_metrics
genuinely creates metric objects. To avoid duplicate-registration errors
across test runs, each test uses an isolated CollectorRegistry by patching
the metric classes onto a fresh registry where needed.
"""

import unittest.mock

import stop_loss_guardian.metrics as metrics_mod


class TestStartMetricsServer:
    def test_missing_prometheus_logs_and_returns(self):
        # Simulate prometheus_client being absent: the no-op fallback in
        # trading_commons.metrics sets HAS_PROMETHEUS False.
        with unittest.mock.patch.object(metrics_mod, "HAS_PROMETHEUS", False):
            with unittest.mock.patch.object(metrics_mod, "start_http_server") as srv:
                # Should not raise and must not start a server.
                metrics_mod.start_metrics_server()
                srv.assert_not_called()

    def test_init_failure_returns_without_server(self):
        with unittest.mock.patch.object(
            metrics_mod, "_init_metrics", return_value=False
        ):
            with unittest.mock.patch.object(metrics_mod, "start_http_server") as srv:
                metrics_mod.start_metrics_server()
                srv.assert_not_called()

    def test_starts_server_on_success(self):
        with unittest.mock.patch.object(
            metrics_mod, "_init_metrics", return_value=True
        ):
            with unittest.mock.patch.object(metrics_mod, "start_http_server") as srv:
                metrics_mod.start_metrics_server()
                srv.assert_called_once()

    def test_respects_metrics_port_env(self):
        with unittest.mock.patch.object(
            metrics_mod, "_init_metrics", return_value=True
        ):
            with unittest.mock.patch.object(metrics_mod, "start_http_server") as srv:
                with unittest.mock.patch.dict(
                    "os.environ", {"METRICS_PORT": "12345"}
                ):
                    metrics_mod.start_metrics_server()
                srv.assert_called_once_with(12345)


class TestInitMetrics:
    def test_creates_metric_objects(self):
        # Use an isolated registry to avoid clashing with the default one.
        from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

        reg = CollectorRegistry()

        def _counter(*a, **kw):
            kw.setdefault("registry", reg)
            return Counter(*a, **kw)

        def _gauge(*a, **kw):
            kw.setdefault("registry", reg)
            return Gauge(*a, **kw)

        def _hist(*a, **kw):
            kw.setdefault("registry", reg)
            return Histogram(*a, **kw)

        # _init_metrics builds metrics from the classes imported into the
        # guardian metrics module (sourced from trading_commons.metrics).
        with unittest.mock.patch.object(metrics_mod, "Counter", _counter), \
             unittest.mock.patch.object(metrics_mod, "Gauge", _gauge), \
             unittest.mock.patch.object(metrics_mod, "Histogram", _hist), \
             unittest.mock.patch.object(metrics_mod, "HAS_PROMETHEUS", True):
            assert metrics_mod._init_metrics() is True

        assert metrics_mod.CHECK_CYCLES is not None
        assert metrics_mod.DB_ERRORS is not None
        assert metrics_mod.REDIS_ERRORS is not None
