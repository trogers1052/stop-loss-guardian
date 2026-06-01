# Stop Loss Guardian

> The platform's #1 job is keeping losses small.

A capital-protection service for a personal AI-assisted stock-trading platform. It
continuously watches every open position and raises escalating alerts the moment a
position is unprotected, drifting into a dangerous drawdown, or approaching an
earnings event. It also enforces portfolio-level circuit breakers so a single bad
day can't quietly compound into a deep hole.

The guiding lesson behind this service: a 34.5% loss requires a 52.7% gain just to
break even, while a 10% loss only needs 11.1%. Small losses are survivable; large
ones are not. This service exists to make sure no position is ever left without a
stop loss.

## Role / Architecture

Stop Loss Guardian runs as a long-lived monitoring daemon on the trading platform's
Raspberry Pi "spine." On each check cycle it:

1. Reads open positions from the trading platform's **PostgreSQL** database
   (the trading journal) and the `stop_loss_tracking` table.
2. Enriches each position with live price, equity, and stop-order data from
   **Redis** (populated by the Robinhood sync service).
3. Runs per-position checks (missing stop, drawdown, stop triggered, earnings
   proximity) and portfolio-level checks (heat, daily P&L, stops-hit count).
4. Dispatches alerts with **escalation**: Telegram → SMS → phone call (Twilio),
   advancing only when lower tiers go unacknowledged.
5. Writes a `risk:daily_state` snapshot back to Redis so the decision engine can
   gate new entries on real-time portfolio risk.

```
PostgreSQL (positions, stop_loss_tracking)
        │
        ▼
  StopLossGuardian  ──reads live prices/stops──  Redis (robinhood:*, risk:daily_state)
        │
        ├─ per-position checks ─┐
        └─ portfolio monitor ───┤
                                ▼
                    AlertDispatcher (escalation)
                                │
              Telegram ──▶ SMS ──▶ Phone call (Twilio)
```

The process also exposes an HTTP `/health` endpoint and a Prometheus metrics
endpoint for observability.

## Features

- **Missing-stop detection** — any open position without a stop loss triggers an
  alert that escalates until acknowledged.
- **Escalating delivery** — Telegram (routine) → SMS (urgent) → phone call,
  advancing only after the configured number of un-acknowledged alerts.
- **Drawdown monitoring** — warning and critical drawdown thresholds, with
  per-symbol cooldowns to avoid alert spam.
- **Stop-trigger awareness** — informational alert when price falls through a
  configured stop, prompting a check that the broker order executed.
- **Earnings proximity** — warns when a held position has earnings within the
  configured window (date from the tracking table, with a Redis fallback).
- **Position sizing** — calculates max shares for a trade under a 2% risk cap and a
  20% position-concentration cap, and flags stocks too expensive for the account.
- **Portfolio-level risk monitor** — actual portfolio heat from real stop
  distances, daily P&L, stops-hit-today counting, gap-risk detection, and a
  circuit breaker that halts new entries when limits are breached.
- **Restart-safe state** — drawdown-alert cooldowns persist in Redis so a restart
  doesn't replay alerts.
- **Stale-price safety** — when price data is older than the staleness threshold,
  live-price/drawdown fields are suppressed (so hours-old figures aren't shown as
  current) but missing-stop alerts still fire.
- **Degraded-service alerting** — if the monitoring loop fails repeatedly, it sends
  a "positions may be UNPROTECTED" alert.
- **Observability** — `/health` HTTP endpoint plus Prometheus metrics (check
  cycles, missing stops, alerts sent, portfolio heat, daily P&L, halt state, etc.).

## Configuration

Configuration is loaded via `pydantic-settings` from environment variables (or an
optional `.env` file). All values have defaults; secrets must be supplied via env.

| Variable | Default | Description |
|----------|---------|-------------|
| `DB_HOST` | `postgres` | PostgreSQL host (trading journal) |
| `DB_PORT` | `5432` | PostgreSQL port |
| `DB_USER` | `trader` | PostgreSQL user |
| `DB_PASSWORD` | _(empty)_ | PostgreSQL password — **set this** |
| `DB_NAME` | `trading_platform` | PostgreSQL database name |
| `REDIS_HOST` | `redis` | Redis host |
| `REDIS_PORT` | `6379` | Redis port |
| `REDIS_DB` | `0` | Redis database number |
| `REDIS_PASSWORD` | _(none)_ | Redis password, if any |
| `REDIS_POSITIONS_KEY` | `robinhood:positions` | Redis hash of positions |
| `REDIS_BUYING_POWER_KEY` | `robinhood:buying_power` | Redis key for buying power |
| `TWILIO_ACCOUNT_SID` | _(none)_ | Twilio SID (enables SMS/phone) — `placeholder` |
| `TWILIO_AUTH_TOKEN` | _(none)_ | Twilio auth token — `placeholder` |
| `TWILIO_PHONE_NUMBER` | _(none)_ | Twilio "from" number |
| `ALERT_PHONE_NUMBER` | _(empty)_ | Your phone number for SMS/calls |
| `TELEGRAM_BOT_TOKEN` | _(none)_ | Telegram bot token — `placeholder` |
| `TELEGRAM_CHAT_ID` | _(none)_ | Telegram chat ID |
| `MAX_RISK_PER_TRADE_PCT` | `2.0` | Max % of account at risk per trade |
| `MAX_POSITION_PCT` | `20.0` | Max % of account in a single position |
| `DEFAULT_STOP_LOSS_PCT` | `10.0` | Default stop distance for suggestions |
| `DRAWDOWN_WARNING_PCT` | `5.0` | Drawdown warning threshold |
| `DRAWDOWN_CRITICAL_PCT` | `10.0` | Drawdown critical threshold |
| `EARNINGS_WARNING_DAYS` | `5` | Days-out window for earnings alerts |
| `ESCALATION_INTERVAL_MINUTES` | `60` | Min minutes between escalation alerts |
| `MAX_TELEGRAM_ALERTS` | `2` | Telegram alerts before escalating to SMS |
| `MAX_SMS_ALERTS` | `2` | SMS alerts before escalating to phone call |
| `CHECK_INTERVAL_SECONDS` | `60` | Monitoring loop interval |
| `MARKET_HOURS_ONLY` | `true` | Restrict checks to market hours |
| `PRICE_STALENESS_MINUTES` | `15` | Age beyond which price data is "stale" |
| `PORTFOLIO_MONITOR_ENABLED` | `true` | Enable portfolio-level risk monitor |
| `PORTFOLIO_MAX_STOPS_PER_DAY` | `3` | Stops-hit count that triggers a halt |
| `PORTFOLIO_DAILY_LOSS_HALT_PCT` | `0.06` | Daily-loss fraction that halts entries |
| `PORTFOLIO_HEAT_WARN_PCT` | `0.08` | Portfolio-heat warning fraction |
| `PORTFOLIO_HEAT_HALT_PCT` | `0.12` | Portfolio-heat halt fraction |
| `LOG_LEVEL` | `INFO` | Logging level |
| `HEALTH_PORT` | `8080` | Port for the `/health` HTTP server |
| `METRICS_PORT` | `9098` | Port for the Prometheus metrics server |

Twilio is enabled only when SID, auth token, "from" number, and alert number are
all set. Telegram is enabled when both the bot token and chat ID are set.

## Running

### Local

```bash
pip install -r requirements.txt

# Provide configuration via environment or a .env file
export DB_PASSWORD=...           # placeholder
export TELEGRAM_BOT_TOKEN=...    # placeholder
export TELEGRAM_CHAT_ID=...      # placeholder

python -m stop_loss_guardian.main
```

The service requires reachable PostgreSQL and Redis instances (the rest of the
trading platform supplies these).

### Docker

```bash
docker build -t stop-loss-guardian .

docker run --rm \
  -e DB_HOST=... -e DB_PASSWORD=... \
  -e REDIS_HOST=... \
  -e TELEGRAM_BOT_TOKEN=... -e TELEGRAM_CHAT_ID=... \
  -p 8080:8080 -p 9098:9098 \
  stop-loss-guardian
```

The image runs as a non-root user and includes a `HEALTHCHECK` that probes
`/health`. CI publishes a multi-arch image (`linux/amd64,linux/arm64`) to GHCR; the
Raspberry Pi (arm64) pulls from there.

## Testing

```bash
pip install -r requirements.txt pytest pytest-cov
pytest tests/ -v --cov=stop_loss_guardian --cov-report=term-missing
```

The suite covers the guardian loop, position sizer, portfolio monitor, alert
dispatcher, Redis client, and data models. CI runs the same command on every push
and pull request and uploads coverage to Codecov.

## Project layout

```
stop_loss_guardian/
├── main.py               # Entry point: health server, metrics, signal handling
├── guardian.py           # StopLossGuardian — monitoring loop + per-position checks
├── portfolio_monitor.py  # Portfolio-level heat, daily P&L, gap risk, circuit breaker
├── position_sizer.py     # 2%-risk / 20%-concentration position sizing
├── config.py             # pydantic-settings configuration
├── models.py             # Position, StopLossRecord, Alert, PositionSizeResult, ...
├── metrics.py            # Prometheus metrics (optional, degrades gracefully)
├── redis_client.py       # Reads Robinhood data, persists cooldowns/state in Redis
├── db/
│   └── repository.py     # PostgreSQL access (positions, stop_loss_tracking)
└── alerting/
    ├── dispatcher.py     # Escalation logic (Telegram → SMS → phone call)
    ├── telegram_client.py
    └── twilio_client.py
tests/                    # pytest suite
Dockerfile                # Multi-stage-friendly, non-root, healthcheck
.github/workflows/ci.yml  # Test + coverage + multi-arch image build/push
```

---

## Built with Claude Code

A large portion of this project — implementation, tests, and documentation — was written in pair-programming sessions with [Claude Code](https://claude.com/claude-code), Anthropic's agentic command-line tool.
