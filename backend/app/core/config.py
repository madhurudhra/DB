from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=None,
        extra="ignore",
        case_sensitive=False,
    )

    app_env: Literal["development", "testing", "production"] = "development"
    trading_mode: Literal["live"] = "live"
    live_trading_enabled: bool = False

    database_path: str = "/var/lib/super-intelligence/super-intelligence.db"

    crypto_bot_enabled: bool = False
    equity_bot_enabled: bool = False
    zerodha_bot_enabled: bool = False

    us_market_timezone: str = "America/New_York"
    india_market_timezone: str = "Asia/Kolkata"
    crypto_market_timezone: str = "UTC"

    dashboard_host: str = "127.0.0.1"
    dashboard_port: int = Field(default=8000, ge=1024, le=65535)


def get_settings() -> Settings:
    return Settings()
