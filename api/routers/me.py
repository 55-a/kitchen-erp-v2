"""
Профиль текущего пользователя Mini App:
    GET  /api/me           — роль, язык, контакт поддержки
    POST /api/me/language  — сменить язык (сохраняется в БД: бот, приложение и PDF-акт на этом языке)
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth import AuthUser, get_current_user
from api.config import get_settings
from api.database import get_session
from api.models import User

router = APIRouter(prefix="/api", tags=["me"])

LANGUAGES = ("uz", "oz", "ru", "en", "zh")   # oz — узбекский на кириллице


@router.get("/me")
async def get_me(
    request: Request,
    user: AuthUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Кто я. Если человек открыл приложение впервые (даже без /start) — запоминаем его
    и сообщаем владельцу, что новому человеку нужно назначить роль."""
    row = (await session.execute(select(User).where(User.telegram_id == user.telegram_id))).scalar_one_or_none()
    if row is None:
        tg_user = getattr(request.state, "tg_user", None) or {}
        full_name = " ".join(x for x in (tg_user.get("first_name"), tg_user.get("last_name")) if x) or user.first_name
        session.add(User(telegram_id=user.telegram_id, full_name=full_name, language=user.language))
        try:
            await session.commit()
        except IntegrityError:   # приложение одновременно спросило /api/me дважды — запись уже есть
            await session.rollback()
        else:
            from api.routers.team import announce_newcomer

            await announce_newcomer(user.telegram_id, full_name, tg_user.get("username"))
    support = get_settings().support_telegram
    return {
        "telegram_id": user.telegram_id,
        "first_name": user.first_name,
        "role": user.role,
        "can_switch": user.is_owner_account,   # менять себе роль может только владелец (проверка кабинетов)
        "language": user.language,
        "support_telegram": support or None,
        "support_url": f"https://t.me/{support}" if support else None,
    }


class LanguageIn(BaseModel):
    language: str = Field(pattern="^(uz|oz|ru|en|zh)$")


@router.post("/me/language")
async def post_language(
    body: LanguageIn,
    user: AuthUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    row = (await session.execute(select(User).where(User.telegram_id == user.telegram_id))).scalar_one_or_none()
    if row is None:  # открыл приложение, ни разу не нажав /start в боте
        row = User(telegram_id=user.telegram_id, full_name=user.first_name, language=body.language)
        session.add(row)
    else:
        row.language = body.language
    await session.commit()
    return {"language": body.language}


class RoleIn(BaseModel):
    role: str = Field(pattern="^(owner|cook|purchaser|client)$")


@router.post("/me/role")
async def post_role(
    body: RoleIn,
    user: AuthUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """
    Смена роли в приложении — только для владельца (проверить кабинеты повара, закупщика, клиента).
    Остальным роль назначает владелец в «👥 Команда» (PUT /api/team/{id}).
    """
    from bot.utils import can_take_role

    if not can_take_role(user.telegram_id, body.role):
        raise HTTPException(403, "Forbidden: role is not allowed")
    row = (await session.execute(select(User).where(User.telegram_id == user.telegram_id))).scalar_one_or_none()
    if row is None:
        row = User(telegram_id=user.telegram_id, full_name=user.first_name, language=user.language, role=body.role)
        session.add(row)
    else:
        row.role = body.role
    await session.commit()
    return {"role": body.role}
