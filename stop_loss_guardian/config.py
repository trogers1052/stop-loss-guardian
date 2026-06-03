"""Configuration for Stop Loss Guardian service.

Builds on :class:`trading_commons.config.BaseServiceSettings`, which supplies
the shared Redis / Telegram / Kafka / logging blocks, the ``redis_url``
property, Docker-secrets support (``/run/secrets/<name>`` wins over the
environment for any field listed in ``SECRET_FIELDS``), and the
env > YAML > defaults precedence helper.

This subclass adds the guardian-specific fields (database, Twilio escalation,
risk parameters, alert thresholds, monitoring, portfolio-level risk) and
extends ``SECRET_FIELDS`` with the guardian's sensitive credentials.
"""

from decimal import Decimal
from typing import ClassVar, Optional

from pydantic import Field
from pydantic_settings import SettingsConfigDict

from trading_commons.config import BaseServiceSettings


class Settings(BaseServiceSettings):
    """Stop Loss Guardian configuration."""

    # Env vars are matched case-insensitively to field names (e.g. DB_HOST ->
    # db_host), so the historical UPPERCASE env names keep working without
    # per-field aliases.
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Database - Trading Platform (PostgreSQL)
    db_host: str = Field(default="postgres")
    db_port: int = Field(default=5432)
    db_user: str = Field(default="trader")
    db_password: str = Field(default="")
    db_name: str = Field(default="trading_platform")

    # Redis position-data keys (host/port/db/password come from the base).
    redis_host: str = Field(default="redis")
    redis_positions_key: str = Field(default="robinhood:positions")
    redis_buying_power_key: str = Field(default="robinhood:buying_power")

    # Twilio - Urgent alerts (SMS + Phone)
    twilio_account_sid: Optional[str] = Field(default=None)
    twilio_auth_token: Optional[str] = Field(default=None)
    twilio_phone_number: Optional[str] = Field(default=None)
    alert_phone_number: str = Field(default="")  # Your phone number

    # Risk Parameters
    max_risk_per_trade_pct: Decimal = Field(default=Decimal("2.0"))
    max_position_pct: Decimal = Field(default=Decimal("20.0"))
    default_stop_loss_pct: Decimal = Field(default=Decimal("10.0"))

    # Alert Thresholds
    drawdown_warning_pct: Decimal = Field(default=Decimal("5.0"))
    drawdown_critical_pct: Decimal = Field(default=Decimal("10.0"))
    earnings_warning_days: int = Field(default=5)

    # Alert Escalation
    escalation_interval_minutes: int = Field(default=60)
    max_telegram_alerts: int = Field(default=2)  # After this, escalate to SMS
    max_sms_alerts: int = Field(default=2)  # After this, escalate to phone call

    # Monitoring
    check_interval_seconds: int = Field(default=60)
    market_hours_only: bool = Field(default=True)
    price_staleness_minutes: int = Field(default=15)

    # Portfolio-level risk monitoring
    portfolio_monitor_enabled: bool = Field(default=True)
    portfolio_max_stops_per_day: int = Field(default=3)
    portfolio_daily_loss_halt_pct: float = Field(default=0.06)
    portfolio_heat_warn_pct: float = Field(default=0.08)
    portfolio_heat_halt_pct: float = Field(default=0.12)

    # Sensitive fields that may be supplied via Docker secrets (in addition to
    # the base's redis_password / telegram_bot_token / telegram_chat_id).
    SECRET_FIELDS: ClassVar[tuple[str, ...]] = (
        "redis_password",
        "telegram_bot_token",
        "telegram_chat_id",
        "db_password",
        "twilio_account_sid",
        "twilio_auth_token",
    )

    @property
    def database_url(self) -> str:
        return f"postgresql://{self.db_user}:{self.db_password}@{self.db_host}:{self.db_port}/{self.db_name}"

    @property
    def twilio_enabled(self) -> bool:
        return all([self.twilio_account_sid, self.twilio_auth_token, self.twilio_phone_number, self.alert_phone_number])

    @property
    def telegram_enabled(self) -> bool:
        return all([self.telegram_bot_token, self.telegram_chat_id])


settings = Settings()
