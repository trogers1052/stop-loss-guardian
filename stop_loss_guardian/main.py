"""Stop Loss Guardian - Entry Point.

The platform's #1 job is keeping losses small.

This service monitors all open positions for:
1. Missing stop losses → SCREAMS until you set one
2. Significant drawdowns → Escalating alerts
3. Position sizing violations → Blocks recommendations

Alerts escalate: Telegram → SMS → Phone Call until acknowledged.
"""

import logging
import os
import signal
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from .config import settings
from .guardian import StopLossGuardian

# Configure logging
logging.basicConfig(
    level=getattr(logging, settings.log_level.upper()),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
    ],
)

logger = logging.getLogger(__name__)

# Global guardian instance for signal handling
guardian: StopLossGuardian = None


def signal_handler(signum, frame):
    """Handle shutdown signals gracefully."""
    logger.info(f"Received signal {signum}, shutting down...")
    if guardian:
        guardian.stop()
    sys.exit(0)


def _start_health_server() -> None:
    """Start a minimal HTTP health server on a daemon thread."""
    port = int(os.environ.get("HEALTH_PORT", "8080"))

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/health":
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"ok")
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, *args):
            pass  # suppress HTTP access logs

    server = HTTPServer(("", port), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logger.info(f"Health server listening on :{port}/health")


def main():
    """Main entry point."""
    global guardian

    _start_health_server()

    logger.info("=" * 60)
    logger.info("STOP LOSS GUARDIAN")
    logger.info("The platform's #1 job is keeping losses small.")
    logger.info("=" * 60)

    # Log configuration
    logger.info(f"Database: {settings.db_host}:{settings.db_port}/{settings.db_name}")
    logger.info(f"Redis: {settings.redis_host}:{settings.redis_port}")
    logger.info(f"Twilio enabled: {settings.twilio_enabled}")
    logger.info(f"Telegram enabled: {settings.telegram_enabled}")
    logger.info(f"Check interval: {settings.check_interval_seconds}s")
    logger.info(f"Escalation interval: {settings.escalation_interval_minutes}min")
    logger.info(f"Max risk per trade: {settings.max_risk_per_trade_pct}%")
    logger.info(f"Max position size: {settings.max_position_pct}%")
    logger.info(f"Drawdown warning: {settings.drawdown_warning_pct}%")
    logger.info(f"Drawdown critical: {settings.drawdown_critical_pct}%")

    # Setup signal handlers
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    # Create and start guardian
    guardian = StopLossGuardian()

    try:
        guardian.start()
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)
    finally:
        if guardian:
            guardian.stop()


if __name__ == "__main__":
    main()
