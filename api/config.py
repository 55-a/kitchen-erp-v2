"""
Настройки приложения. Все значения читаются из переменных окружения.

Один модуль используется и API, и ботом, чтобы конфигурация не расходилась.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache


def _int_set(raw: str) -> set[int]:
    """Разбирает строку вида "123, 456" в множество целых Telegram ID."""
    result: set[int] = set()
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if part.isdigit():
            result.add(int(part))
    return result


def _bool(raw: str | None, default: bool) -> bool:
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    # Токен бота — нужен и боту, и API (проверка подписи initData)
    bot_token: str = ""
    # Публичный HTTPS-адрес, где развернут API (Telegram открывает Mini App только по HTTPS)
    webapp_url: str = ""
    # Строка подключения к PostgreSQL (Railway отдаёт postgresql://...)
    database_url: str = "sqlite+aiosqlite:///./kitchen.db"
    # Ключ и модель Google Gemini
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"
    # Лимит запросов к Gemini в минуту (бесплатный тариф — 15)
    gemini_rpm: int = 15
    # Telegram ID владельцев (только они получают роль "owner")
    owner_ids: set[int] = field(default_factory=set)
    # Telegram ID сотрудников (повара/закупщики). Пусто — роль может выбрать любой.
    staff_ids: set[int] = field(default_factory=set)
    # Сколько секунд initData считается действительным
    init_data_ttl: int = 86400
    # Запускать ли polling бота внутри процесса API (удобно для одного сервиса на Railway)
    run_bot_in_api: bool = True
    # Язык по умолчанию
    default_language: str = "ru"
    # Каталог со шрифтами для PDF (если шрифты лежат не в системных путях)
    font_dir: str = ""

    @property
    def async_database_url(self) -> str:
        """Приводит DATABASE_URL к асинхронному драйверу SQLAlchemy."""
        url = self.database_url
        if url.startswith("postgres://"):
            url = "postgresql://" + url[len("postgres://"):]
        if url.startswith("postgresql://"):
            url = "postgresql+asyncpg://" + url[len("postgresql://"):]
        return url


@lru_cache
def get_settings() -> Settings:
    """Читает окружение один раз. В тестах кэш сбрасывается через get_settings.cache_clear()."""
    return Settings(
        bot_token=os.getenv("BOT_TOKEN", ""),
        webapp_url=os.getenv("WEBAPP_URL", "").rstrip("/"),
        database_url=os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./kitchen.db"),
        gemini_api_key=os.getenv("GEMINI_API_KEY", ""),
        gemini_model=os.getenv("GEMINI_MODEL", "gemini-2.5-flash"),
        gemini_rpm=int(os.getenv("GEMINI_RPM", "15")),
        owner_ids=_int_set(os.getenv("OWNER_IDS", "")),
        staff_ids=_int_set(os.getenv("STAFF_IDS", "")),
        init_data_ttl=int(os.getenv("INIT_DATA_TTL", "86400")),
        run_bot_in_api=_bool(os.getenv("RUN_BOT_IN_API"), True),
        default_language=os.getenv("DEFAULT_LANGUAGE", "ru"),
        font_dir=os.getenv("FONT_DIR", ""),
    )
