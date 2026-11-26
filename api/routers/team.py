"""
Команда: владелец сам назначает людям роли.

Человек нажимает «ОТКРЫТЬ» (или /start) → владельцу приходит «Новый человек …» →
владелец в «👥 Команда» выбирает роль (повар, закупщик, клиент + его компания) →
бот пишет человеку, и по «ОТКРЫТЬ» у него сразу открывается свой кабинет.

    GET /api/team                  — все, кто открывал бота, и список компаний-клиентов
    PUT /api/team/{telegram_id}    — назначить / снять роль
"""
from __future__ import annotations

import html

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth import AuthUser, get_current_user, resolve_role
from api.config import get_settings
from api.database import get_session, session_scope
from api.models import Client, User
from api.notify import send_message
from api.services import ServiceError

router = APIRouter(prefix="/api/team", tags=["team"])


def owner_account(user: AuthUser = Depends(get_current_user)) -> AuthUser:
    """Командой управляет только владелец (аккаунт из OWNER_IDS)."""
    if not user.is_owner_account:
        raise HTTPException(403, "Forbidden: role is not allowed")
    return user


def _person(u: User, client_name: str | None) -> dict:
    settings = get_settings()
    is_owner = u.telegram_id in settings.owner_ids
    return {
        "telegram_id": u.telegram_id,
        "name": u.full_name or str(u.telegram_id),
        "role": "owner" if is_owner else resolve_role(u.telegram_id, u.role, settings),
        "is_owner": is_owner,
        "client": client_name,
        "since": u.created_at.date().isoformat() if u.created_at else None,
    }


@router.get("")
async def get_team(_: AuthUser = Depends(owner_account), session: AsyncSession = Depends(get_session)) -> dict:
    users = (await session.execute(select(User).order_by(User.id.desc()))).scalars().all()
    clients = (await session.execute(select(Client).order_by(Client.name))).scalars().all()
    by_tg = {c.telegram_id: c.name for c in clients if c.telegram_id}
    people = [_person(u, by_tg.get(u.telegram_id)) for u in users]
    # Сначала те, кто ждёт роль, владельцы — в конце
    people.sort(key=lambda p: (p["is_owner"], p["role"] is not None))
    return {"people": people, "clients": [{"name": c.name, "linked": c.telegram_id is not None} for c in clients]}


class AssignIn(BaseModel):
    role: str | None = Field(default=None, pattern="^(cook|purchaser|client)$")   # None — снять роль
    client: str | None = None                                                      # компания для роли «клиент»


@router.put("/{telegram_id}")
async def assign_role(
    telegram_id: int,
    body: AssignIn,
    _: AuthUser = Depends(owner_account),
    session: AsyncSession = Depends(get_session),
) -> dict:
    if telegram_id in get_settings().owner_ids:
        raise ServiceError(400, "owner_fixed", "Owner role is set in server settings (OWNER_IDS)")
    person = (await session.execute(select(User).where(User.telegram_id == telegram_id))).scalar_one_or_none()
    if person is None:
        raise ServiceError(404, "user_not_found", "User not found")

    company: Client | None = None
    if body.role == "client":
        if not (body.client or "").strip():
            raise ServiceError(400, "client_required", "Choose the client company")
        company = (await session.execute(select(Client).where(Client.name == body.client.strip()))).scalar_one_or_none()
        if company is None:
            raise ServiceError(404, "client_not_found", "Client not found")

    # Человек привязан максимум к одной компании: снимаем старые привязки, потом ставим новую
    for c in (await session.execute(select(Client).where(Client.telegram_id == telegram_id))).scalars():
        c.telegram_id = None
    await session.flush()
    if company is not None:
        company.telegram_id = telegram_id
    person.role = body.role
    await session.commit()

    await tell_about_role(person, body.role, company.name if company else None)
    return _person(person, company.name if company else None)


async def tell_about_role(person: User, role: str | None, client_name: str | None) -> None:
    """Бот сообщает человеку о новой роли — с кнопкой, открывающей его кабинет."""
    from bot.translations import t

    lang = person.language or "ru"
    base = get_settings().webapp_url
    if role is None:
        await send_message(person.telegram_id, t(lang, "role_removed"))
        return
    role_name = t(lang, f"role_{role}")
    if client_name:
        role_name += f" — {html.escape(client_name)}"
    await send_message(
        person.telegram_id,
        t(lang, "role_assigned", role=role_name),
        button_text=t(lang, "btn_open_cabinet") if base else None,
        button_url=f"{base}/app?lang={lang}" if base else None,
    )


async def announce_newcomer(telegram_id: int, name: str, username: str | None = None) -> None:
    """Новый человек открыл бота или приложение — владельцам приходит «назначьте роль»."""
    from bot.translations import t

    settings = get_settings()
    if telegram_id in settings.owner_ids:
        return
    async with session_scope() as session:
        owners = {u.telegram_id: u for u in (await session.execute(
            select(User).where(User.telegram_id.in_(settings.owner_ids)))).scalars()}
    who = html.escape(name or str(telegram_id)) + (f" (@{html.escape(username)})" if username else "")
    for owner_id in settings.owner_ids:
        lang = owners[owner_id].language if owner_id in owners else settings.default_language
        await send_message(
            owner_id,
            t(lang, "newcomer_owner", name=who, tg_id=telegram_id),
            button_text=t(lang, "btn_team") if settings.webapp_url else None,
            button_url=f"{settings.webapp_url}/app/owner?lang={lang}&tab=team" if settings.webapp_url else None,
        )
