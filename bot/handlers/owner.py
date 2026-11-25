"""
Команды владельца:
    /stats                         — краткая сводка прямо в чате
    /link_client <tg_id> <Клиент>  — привязать Telegram-аккаунт к клиенту
Основная работа — в Mini App (кнопки "📊 Статистика", "🤝 Клиенты").
"""
from __future__ import annotations

import html

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message
from sqlalchemy import select

from api.database import session_scope
from api.models import Client
from api.pdf_generator import fmt_money
from api.services import compute_stats
from bot.translations import t
from bot.utils import effective_role, get_or_create_user

router = Router(name="owner")


@router.message(Command("stats"))
async def cmd_stats(message: Message) -> None:
    async with session_scope() as session:
        user = await get_or_create_user(session, message.from_user)
        if effective_role(user) != "owner":
            await message.answer(t(user.language, "role_denied"))
            return
        s = await compute_stats(session)
    await message.answer(
        f"📊 <b>Kuxnya NVGR</b>\n"
        f"💰 {fmt_money(s['revenue'])}\n"
        f"🧾 {fmt_money(s['cost'])}\n"
        f"📈 {fmt_money(s['profit'])} ({s['margin']}%)\n"
        f"🍲 {s['portions']}"
    )


@router.message(Command("link_client"))
async def cmd_link_client(message: Message, command: CommandObject) -> None:
    """Связывает клиента с Telegram ID, чтобы клиент видел свой кабинет."""
    async with session_scope() as session:
        user = await get_or_create_user(session, message.from_user)
        lang = user.language
        if effective_role(user) != "owner":
            await message.answer(t(lang, "role_denied"))
            return

        parts = (command.args or "").split(maxsplit=1)
        if len(parts) != 2 or not parts[0].isdigit():
            await message.answer(t(lang, "link_usage"))
            return
        tg_id, name = int(parts[0]), parts[1].strip()

        # Снимаем привязку этого ID с другого клиента (telegram_id уникален)
        other = (await session.execute(select(Client).where(Client.telegram_id == tg_id))).scalar_one_or_none()
        if other and other.name != name:
            other.telegram_id = None
            await session.flush()

        client = (await session.execute(select(Client).where(Client.name == name))).scalar_one_or_none()
        if client is None:
            client = Client(name=name)
            session.add(client)
        client.telegram_id = tg_id
    await message.answer(t(lang, "link_done", client=html.escape(name), tg_id=tg_id))
