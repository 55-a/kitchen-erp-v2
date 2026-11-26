"""
Общие фикстуры тестов.

Переменные окружения выставляются ДО импорта приложения: тесты работают
на SQLite в памяти и не трогают продакшен-базу и Telegram.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from urllib.parse import urlencode

os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///:memory:"
os.environ["BOT_TOKEN"] = "123456:TEST-TOKEN"
os.environ["OWNER_IDS"] = "1"
os.environ["STAFF_IDS"] = ""
os.environ["RUN_BOT_IN_API"] = "false"
os.environ["WEBAPP_URL"] = "https://example.test"
os.environ["GEMINI_API_KEY"] = ""
os.environ["NOTIFY_DISABLED"] = "1"   # бот не пишет в настоящий Telegram

import pytest_asyncio  # noqa: E402

from api.config import get_settings  # noqa: E402

get_settings.cache_clear()

from api.database import Base, SessionLocal, engine  # noqa: E402
from api import models  # noqa: E402,F401

OWNER_ID = 1
COOK_ID = 2
PURCHASER_ID = 3
CLIENT_ID = 4


def make_init_data(user_id: int, bot_token: str | None = None, auth_date: int | None = None, **user_extra) -> str:
    """Собирает корректно подписанный initData — так же, как это делает Telegram."""
    token = bot_token or os.environ["BOT_TOKEN"]
    user = {"id": user_id, "first_name": "Test", "language_code": "ru", **user_extra}
    fields = {
        "auth_date": str(auth_date or int(time.time())),
        "query_id": "AAHdF6IQAAAAAN0XohDhrOrc",
        "user": json.dumps(user, separators=(",", ":"), ensure_ascii=False),
    }
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, data_check_string.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


def auth_headers(user_id: int) -> dict:
    return {"X-Telegram-Init-Data": make_init_data(user_id)}


@pytest_asyncio.fixture(loop_scope="session")
async def db():
    """Чистая схема для каждого теста."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield SessionLocal
