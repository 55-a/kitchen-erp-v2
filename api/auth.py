"""
Авторизация Mini App через Telegram initData.

Схема:
1. Фронтенд передаёт window.Telegram.WebApp.initData в заголовке
   `X-Telegram-Init-Data` (или `Authorization: tma <initData>`).
2. Middleware `TelegramAuthMiddleware` проверяет подпись HMAC-SHA256 для КАЖДОГО
   запроса к /api/* и при ошибке сразу отвечает 403.
3. Зависимость `require_roles(...)` достаёт роль пользователя из БД и проверяет доступ.

Алгоритм проверки (https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app):
    secret_key = HMAC_SHA256(key="WebAppData", msg=bot_token)
    hash       = hex(HMAC_SHA256(key=secret_key, msg=data_check_string))
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from typing import Callable
from urllib.parse import parse_qsl

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from api.config import Settings, get_settings
from api.database import get_session
from api.models import User

# Пути API, которые не требуют initData
PUBLIC_API_PATHS = {"/api/health"}
# Скачивание PDF по одноразовой подписанной ссылке (Telegram не может передать заголовки
# при скачивании файла), токен проверяется в самом обработчике.
TOKEN_API_PATHS = {"/api/client/act-pdf"}

ROLES = ("owner", "cook", "purchaser", "client")


class InitDataError(Exception):
    """Подпись initData неверна, данные устарели или повреждены."""


def validate_init_data(init_data: str, bot_token: str, ttl: int = 86400) -> dict:
    """
    Проверяет подпись initData и возвращает разобранные поля (user — уже dict).
    Бросает InitDataError, если проверка не прошла.
    """
    if not init_data or not bot_token:
        raise InitDataError("empty init data or bot token")

    pairs = dict(parse_qsl(init_data, keep_blank_values=True, strict_parsing=False))
    received_hash = pairs.pop("hash", None)
    if not received_hash:
        raise InitDataError("hash is missing")

    # data_check_string: все поля, кроме hash, отсортированы по ключу, через \n
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    calculated = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

    # compare_digest — защита от атаки по времени
    if not hmac.compare_digest(calculated, received_hash):
        raise InitDataError("bad signature")

    try:
        auth_date = int(pairs.get("auth_date", "0"))
    except ValueError as exc:
        raise InitDataError("bad auth_date") from exc
    if ttl > 0 and time.time() - auth_date > ttl:
        raise InitDataError("init data expired")

    if "user" in pairs:
        try:
            pairs["user"] = json.loads(pairs["user"])
        except json.JSONDecodeError as exc:
            raise InitDataError("bad user json") from exc
    if not isinstance(pairs.get("user"), dict) or "id" not in pairs["user"]:
        raise InitDataError("user is missing")
    return pairs


def extract_init_data(request: Request) -> str:
    """Достаёт initData из заголовков запроса."""
    header = request.headers.get("X-Telegram-Init-Data")
    if header:
        return header
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("tma "):
        return auth[4:].strip()
    return ""


class TelegramAuthMiddleware(BaseHTTPMiddleware):
    """Проверяет initData для всех запросов /api/* (кроме публичных). Ошибка → 403."""

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if not path.startswith("/api/") or path in PUBLIC_API_PATHS or request.method == "OPTIONS":
            return await call_next(request)
        if path in TOKEN_API_PATHS and request.query_params.get("token"):
            return await call_next(request)

        settings = get_settings()
        try:
            data = validate_init_data(extract_init_data(request), settings.bot_token, settings.init_data_ttl)
        except InitDataError:
            return JSONResponse({"detail": "Forbidden: invalid Telegram initData"}, status_code=403)

        request.state.tg_user = data["user"]
        return await call_next(request)


@dataclass
class AuthUser:
    """Текущий пользователь Mini App."""

    telegram_id: int
    first_name: str
    role: str | None
    language: str

    @property
    def is_owner(self) -> bool:
        return self.role == "owner"


def resolve_role(telegram_id: int, stored_role: str | None, settings: Settings) -> str | None:
    """
    Итоговая роль с учётом белых списков:
    - владелец определяется ТОЛЬКО по OWNER_IDS (нельзя "выбрать" себе роль владельца);
    - повар/закупщик — если STAFF_IDS задан, только из него;
    - владелец может переключиться на любую роль (проверить работу повара, закупщика,
      клиента) и вернуться обратно через «Сменить роль». Пока роль не выбрана — он владелец.
    """
    if telegram_id in settings.owner_ids:
        return stored_role if stored_role in {"owner", "cook", "purchaser", "client"} else "owner"
    if stored_role == "owner":
        return None
    if stored_role in {"cook", "purchaser"} and settings.staff_ids and telegram_id not in settings.staff_ids:
        return None
    return stored_role


async def get_current_user(
    request: Request, session: AsyncSession = Depends(get_session)
) -> AuthUser:
    """FastAPI-зависимость: пользователь, проверенный middleware, + роль из БД."""
    tg_user = getattr(request.state, "tg_user", None)
    if not tg_user:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Forbidden: invalid Telegram initData")

    settings = get_settings()
    telegram_id = int(tg_user["id"])
    row = (await session.execute(select(User).where(User.telegram_id == telegram_id))).scalar_one_or_none()
    stored_role = row.role if row else None
    language = row.language if row else (tg_user.get("language_code") or settings.default_language)
    return AuthUser(
        telegram_id=telegram_id,
        first_name=tg_user.get("first_name", ""),
        role=resolve_role(telegram_id, stored_role, settings),
        language=language if language in {"ru", "en", "uz", "zh"} else settings.default_language,
    )


def require_roles(*roles: str) -> Callable:
    """Фабрика зависимостей: пропускает только указанные роли (владелец — всегда)."""

    async def checker(user: AuthUser = Depends(get_current_user)) -> AuthUser:
        if user.is_owner or user.role in roles:
            return user
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Forbidden: role is not allowed")

    return checker


# ---------- Подписанные ссылки для скачивания PDF ----------

def sign_download_token(
    telegram_id: int, client_name: str, lang: str, ttl: int = 300, extra: dict | None = None
) -> str:
    """Короткоживущий токен (5 минут) для скачивания акта без заголовков авторизации."""
    settings = get_settings()
    expires = int(time.time()) + ttl
    data = {"c": client_name, "u": telegram_id, "l": lang, "e": expires, **(extra or {})}
    payload = json.dumps(data, ensure_ascii=False)
    body = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
    key = hashlib.sha256(("download:" + settings.bot_token).encode()).digest()
    sig = hmac.new(key, body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{sig}"


def verify_download_token(token: str) -> dict:
    """Проверяет токен скачивания. Ошибка → 403."""
    settings = get_settings()
    try:
        body, sig = token.rsplit(".", 1)
    except ValueError:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Forbidden: bad token")
    key = hashlib.sha256(("download:" + settings.bot_token).encode()).digest()
    expected = hmac.new(key, body.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Forbidden: bad token")
    padded = body + "=" * (-len(body) % 4)
    data = json.loads(base64.urlsafe_b64decode(padded.encode()).decode())
    if data.get("e", 0) < time.time():
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Forbidden: token expired")
    return data
