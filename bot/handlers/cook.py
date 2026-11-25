"""
Команды повара. Партии и рецепты оформляются в Mini App
(кнопки "🍲 Новая партия", "📖 Рецепты"); здесь — быстрый просмотр склада.
"""
from __future__ import annotations

import html

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy import select

from api.database import session_scope
from api.models import Stock
from api.services import qty
from bot.translations import t
from bot.utils import effective_role, get_or_create_user

router = Router(name="cook")


@router.message(Command("stock"))
async def cmd_stock(message: Message) -> None:
    """Остатки склада списком (для повара, закупщика и владельца)."""
    async with session_scope() as session:
        user = await get_or_create_user(session, message.from_user)
        if effective_role(user) not in {"owner", "cook", "purchaser"}:
            await message.answer(t(user.language, "role_denied"))
            return
        rows = (await session.execute(select(Stock).order_by(Stock.name))).scalars().all()

    lines = [f"{'⚠️' if p.quantity <= 0 else '•'} {html.escape(p.name)}: {qty(p.quantity)} {p.unit}" for p in rows]
    text = "📦\n" + "\n".join(lines) if lines else "📦 —"
    # Telegram ограничивает сообщение 4096 символами
    for i in range(0, len(text), 4000):
        await message.answer(text[i : i + 4000])
