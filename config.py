"""Конфигурация бота из переменных окружения (.env)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class Config:
    telegram_bot_token: str
    deepseek_api_key: str
    wp_base_url: str
    wp_username: str
    wp_application_password: str
    admin_usernames: frozenset[str]
    booking_url: str
    site_timezone: str
    log_level: str = "INFO"

    @property
    def timezone(self) -> ZoneInfo:
        return ZoneInfo(self.site_timezone)

    @property
    def wp_auth(self) -> tuple[str, str]:
        return (self.wp_username, self.wp_application_password)


def _require(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Отсутствует обязательная переменная окружения {name}")
    return value


def load_config() -> Config:
    admin_usernames = frozenset(
        u.strip().lower().lstrip("@")
        for u in os.getenv("ADMIN_USERNAMES", "").split(",")
        if u.strip()
    )
    return Config(
        telegram_bot_token=_require("TELEGRAM_BOT_TOKEN"),
        deepseek_api_key=_require("DEEPSEEK_API_KEY"),
        wp_base_url=_require("WP_BASE_URL").rstrip("/"),
        wp_username=_require("WP_USERNAME"),
        wp_application_password=_require("WP_APPLICATION_PASSWORD"),
        admin_usernames=admin_usernames,
        booking_url=_require("BOOKING_URL"),
        site_timezone=os.getenv("SITE_TIMEZONE", "America/Edmonton"),
        log_level=os.getenv("LOG_LEVEL", "INFO"),
    )
