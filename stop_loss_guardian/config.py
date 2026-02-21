"""Configuration for Stop Loss Guardian service."""

from decimal import Decimal
from pydantic_settings import BaseSettings
from pydantic import Field
from typing import Optional


class Settings(BaseSettings):
    """Stop Loss Guardian configuration."""

    # Database - Trading Platform (PostgreSQL)
    db_host: str = Field(default="postgres", alias="DB_HOST")
    db_port: int = Field(default=5432, alias="DB_PORT")
    db_user: str = Field(default="trader", alias="DB_USER")
    db_password: str = Field(default="trader5", alias="DB_PASSWORD")
    db_name: str = Field(default="trading_platform", alias="DB_NAME")

    # Redis - Position data from Robinhood sync
    redis_host: str = Field(default="redis", alias="REDIS_HOST")
    redis_port: int = Field(default=6379, alias="REDIS_PORT")
    redis_db: int = Field(default=0, alias="REDIS_DB")
    redis_positions_key: str = Field(default="robinhood:positions", alias="REDIS_POSITIONS_KEY")
    redis_buying_power_key: str = Field(default="robinhood:buying_power", alias="REDIS_BUYING_POWER_KEY")

    # Twilio - Urgent alerts (SMS + Phone)
    twilio_account_sid: Optional[str] = Field(default=None, alias="TWILIO_ACCOUNT_SID")
    twilio_auth_token: Optional[str] = Field(default=None, alias="TWILIO_AUTH_TOKEN")
    twilio_phone_number: Optional[str] = Field(default=None, alias="TWILIO_PHONE_NUMBER")
    alert_phone_number: str = Field(default="", alias="ALERT_PHONE_NUMBER")  # Your phone number

    # Telegram - Routine alerts (fallback)
    telegram_bot_token: Optional[str] = Field(default=None, alias="TELEGRAM_BOT_TOKEN")
    telegram_chat_id: Optional[str] = Field(default=None, alias="TELEGRAM_CHAT_ID")

    # Risk Parameters
    max_risk_per_trade_pct: Decimal = Field(default=Decimal("2.0"), alias="MAX_RISK_PER_TRADE_PCT")
    max_position_pct: Decimal = Field(default=Decimal("20.0"), alias="MAX_POSITION_PCT")
    default_stop_loss_pct: Decimal = Field(default=Decimal("10.0"), alias="DEFAULT_STOP_LOSS_PCT")

    # Alert Thresholds
    drawdown_warning_pct: Decimal = Field(default=Decimal("5.0"), alias="DRAWDOWN_WARNING_PCT")
    drawdown_critical_pct: Decimal = Field(default=Decimal("10.0"), alias="DRAWDOWN_CRITICAL_PCT")
    earnings_warning_days: int = Field(default=5, alias="EARNINGS_WARNING_DAYS")

    # Alert Escalation
    escalation_interval_minutes: int = Field(default=60, alias="ESCALATION_INTERVAL_MINUTES")
    max_telegram_alerts: int = Field(default=2, alias="MAX_TELEGRAM_ALERTS")  # After this, escalate to SMS
    max_sms_alerts: int = Field(default=2, alias="MAX_SMS_ALERTS")  # After this, escalate to phone call

    # Monitoring
    check_interval_seconds: int = Field(default=60, alias="CHECK_INTERVAL_SECONDS")
    market_hours_only: bool = Field(default=True, alias="MARKET_HOURS_ONLY")
    price_staleness_minutes: int = Field(default=15, alias="PRICE_STALENESS_MINUTES")

    # Logging
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    @property
    def database_url(self) -> str:
        return f"postgresql://{self.db_user}:{self.db_password}@{self.db_host}:{self.db_port}/{self.db_name}"

    @property
    def twilio_enabled(self) -> bool:
        return all([self.twilio_account_sid, self.twilio_auth_token, self.twilio_phone_number, self.alert_phone_number])

    @property
    def telegram_enabled(self) -> bool:
        return all([self.telegram_bot_token, self.telegram_chat_id])

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "extra": "ignore",
    }


settings = Settings()
