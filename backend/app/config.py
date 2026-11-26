from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    mock_llm: bool = True
    llm_base_url: str = "https://api.openai.com/v1"
    llm_api_key: str = ""
    llm_proxy_url: str = ""
    llm_timeout_s: float = 60.0

    model_smart: str = "gpt-4.1"
    model_fast: str = "gpt-4.1-mini"
    model_smart_price_in_usd: float = 2.0
    model_smart_price_out_usd: float = 8.0
    model_fast_price_in_usd: float = 0.4
    model_fast_price_out_usd: float = 1.6
    usd_rub_rate: float = 82.1664

    daily_budget_rub: float = 500.0
    budget_tz: str = "Europe/Moscow"
    rate_limit_ip_per_min: int = 60
    rate_limit_session_msg_per_min: int = 10
    max_turns_per_session: int = 30
    max_message_chars: int = 4000

    operator_user: str = "manager"
    operator_password: str = ""
    manager_session_hours: int = 12
    customer_session_hours: int = 720

    cors_origins: str = "*"
    data_dir: Path = Path("./data")
    default_mode: Literal["autopilot", "pilot"] = "autopilot"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "app.db"

    @property
    def quotes_dir(self) -> Path:
        return self.data_dir / "quotes"

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
