"""
Профиль текущего пользователя Mini App:
    GET  /api/me           — роль, язык, контакт поддержки
    POST /api/me/language  — сменить язык (сохраняется в БД: бот, приложение и PDF-акт на этом языке)
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth import AuthUser, get_current_user
from api.config import get_settings
from api.database import get_session
from api.models import User

router = APIRouter(prefix="/api", tags=["me"])

LANGUAGES = ("uz", "oz", "ru", "en", "zh")   # oz — узбекский на кириллице


@router.get("/me")
async def get_me(user: AuthUser = Depends(get_current_user)) -> dict:
    support = get_settings().support_telegram
    return {
        "telegram_id": user.telegram_id,
        "first_name": user.first_name,
        "role": user.role,
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
