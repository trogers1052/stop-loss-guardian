"""Prometheus metrics for stop-loss-guardian.

Defines application-level metrics for monitoring the guardian's
health, performance, and business-logic behavior.  All metrics
use the ``guardian_`` prefix so they are easy to filter in Grafana.
"""

import logging
import os

logger = logging.getLogger(__name__)

_DEFAULT_PORT = 9098

# ---------------------------------------------------------------------------
# Metric definitions
# ---------------------------------------------------------------------------
# Lazy-initialised: the objects are None until start_metrics_server()
# succeeds.  All instrumentation call-sites must go through the helper
# functions below which silently no-op when prometheus_client is absent.
# ---------------------------------------------------------------------------

CHECK_CYCLES: "Counter | None" = None
CHECK_CYCLE_DURATION: "Histogram | None" = None
POSITIONS_CHECKED: "Counter | None" = None
MISSING_STOPS: "Gauge | None" = None
DRAWDOWN_WARNINGS: "Counter | None" = None
EARNINGS_ALERTS: "Counter | None" = None
ALERTS_SENT: "Counter | None" = None
PORTFOLIO_HEAT: "Gauge | None" = None
DAILY_PNL: "Gauge | None" = None
PORTFOLIO_HALTED: "Gauge | None" = None
CONSECUTIVE_ERRORS: "Gauge | None" = None
DB_ERRORS: "Counter | None" = None
REDIS_ERRORS: "Counter | None" = None


def _init_metrics() -> bool:
    """Create all Prometheus metric objects.  Returns True on success."""
    global CHECK_CYCLES, CHECK_CYCLE_DURATION, POSITIONS_CHECKED
    global MISSING_STOPS, DRAWDOWN_WARNINGS, EARNINGS_ALERTS
    global ALERTS_SENT, PORTFOLIO_HEAT, DAILY_PNL, PORTFOLIO_HALTED
    global CONSECUTIVE_ERRORS, DB_ERRORS, REDIS_ERRORS

    try:
        from prometheus_client import Counter, Gauge, Histogram
    except ImportError:
        return False

    CHECK_CYCLES = Counter(
        "guardian_check_cycles_total",
        "Total number of monitoring cycles completed",
    )
    CHECK_CYCLE_DURATION = Histogram(
        "guardian_check_cycle_duration_seconds",
        "Duration of each monitoring cycle in seconds",
        buckets=(0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0),
    )
    POSITIONS_CHECKED = Counter(
        "guardian_positions_checked_total",
        "Total number of positions checked across all cycles",
    )
    MISSING_STOPS = Gauge(
        "guardian_missing_stops",
        "Current number of positions without stop losses",
    )
    DRAWDOWN_WARNINGS = Counter(
        "guardian_drawdown_warnings_total",
        "Total drawdown alerts sent, by severity",
        ["severity"],
    )
    EARNINGS_ALERTS = Counter(
        "guardian_earnings_alerts_total",
        "Total earnings proximity alerts sent",
    )
    ALERTS_SENT = Counter(
        "guardian_alerts_sent_total",
        "Total alerts dispatched, by channel and status",
        ["channel", "status"],
    )
    PORTFOLIO_HEAT = Gauge(
        "guardian_portfolio_heat_pct",
        "Current portfolio risk as a percentage of account equity",
    )
    DAILY_PNL = Gauge(
        "guardian_daily_pnl_pct",
        "Current daily P&L as a percentage",
    )
    PORTFOLIO_HALTED = Gauge(
        "guardian_portfolio_halted",
        "1 if portfolio circuit breaker is active, 0 otherwise",
    )
    CONSECUTIVE_ERRORS = Gauge(
        "guardian_consecutive_errors",
        "Number of consecutive monitoring loop errors",
    )
    DB_ERRORS = Counter(
        "guardian_db_errors_total",
        "Total database operation failures",
    )
    REDIS_ERRORS = Counter(
        "guardian_redis_errors_total",
        "Total Redis operation failures",
    )
    return True


def start_metrics_server() -> None:
    """Start Prometheus metrics HTTP server on METRICS_PORT (default 9098)."""
    try:
        from prometheus_client import start_http_server
    except ImportError:
        logger.warning("prometheus_client not installed — metrics endpoint disabled")
        return

    if not _init_metrics():
        logger.warning("Failed to initialise Prometheus metrics")
        return

    port = int(os.environ.get("METRICS_PORT", str(_DEFAULT_PORT)))
    start_http_server(port)
    logger.info(f"Metrics server listening on :{port}/metrics")
