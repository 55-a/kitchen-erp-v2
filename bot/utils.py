"""Общие функции бота: работа с пользователем, ролью и языком."""
from __future__ import annotations

from aiogram.types import User as TgUser
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth import resolve_role
from api.config import get_settings
from api.models import Client, User
from bot.translations import normalize_lang


async def get_or_create_user(session: AsyncSession, tg_user: TgUser) -> User:
    """Находит пользователя по Telegram ID или создаёт (язык — из настроек Telegram)."""
    user = (await session.execute(select(User).where(User.telegram_id == tg_user.id))).scalar_one_or_none()
    if user is None:
        user = User(
            telegram_id=tg_user.id,
            full_name=tg_user.full_name,
            language=normalize_lang(tg_user.language_code),
        )
        session.add(user)
        await session.flush()
    elif user.full_name != tg_user.full_name:
        user.full_name = tg_user.full_name
    return user


def effective_role(user: User | None) -> str | None:
    """Роль с учётом белых списков OWNER_IDS / STAFF_IDS (та же логика, что в API)."""
    if user is None:
        return None
    return resolve_role(user.telegram_id, user.role, get_settings())


def can_take_role(telegram_id: int, role: str) -> bool:
    """Сам выбирает себе роль только владелец (проверить кабинеты). Остальным роль назначает
    владелец в приложении: «👥 Команда»."""
    return telegram_id in get_settings().owner_ids and role in {"owner", "cook", "purchaser", "client"}


async def linked_client(session: AsyncSession, telegram_id: int) -> Client | None:
    return (await session.execute(select(Client).where(Client.telegram_id == telegram_id))).scalar_one_or_none()
