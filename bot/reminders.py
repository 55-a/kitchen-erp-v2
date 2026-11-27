"""
Вечернее напоминание повару об остатках готовой еды.

Каждый день в REMINDER_HOUR (по умолчанию 17:00 по Ташкенту, UTC+5) бот смотрит партии
с нераспределёнными порциями и пишет поварам: «Остались порции: … — что с ними?»
с кнопкой «📦 Остатки», которая открывает нужный экран приложения.
Если поваров в системе нет — пишет владельцам. Без остатков — ничего не шлёт.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import html
import logging

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from sqlalchemy import select

from api.auth import resolve_role
from api.config import get_settings
from api.database import session_scope
from api.models import Batch, User
from bot.translations import t

log = logging.getLogger(__name__)

TASHKENT = dt.timezone(dt.timedelta(hours=5))   # в Узбекистане нет перехода на летнее время
CHECK_EVERY = 300                                # секунд между проверками


async def collect_leftovers() -> list[dict]:
    """Открытые остатки за последние 3 дня (что ещё не раздали/не списали)."""
    from api.routers.kitchen import batch_states

    since = dt.date.today() - dt.timedelta(days=3)
    async with session_scope() as session:
        batches = (await session.execute(select(Batch).where(Batch.date >= since))).scalars().all()
        states = await batch_states(session, batches)
    return [s for s in states.values() if s["status"] != "closed" and s["remaining"] > 0]


async def recipients() -> list[User]:
    """Повара; если их нет — владельцы (у кого есть запись в users)."""
    settings = get_settings()
    async with session_scope() as session:
        users = (await session.execute(select(User))).scalars().all()
    cooks = [u for u in users if resolve_role(u.telegram_id, u.role, settings) == "cook"]
    if cooks:
        return cooks
    return [u for u in users if u.telegram_id in settings.owner_ids]


def reminder_text(lang: str, leftovers: list[dict]) -> str:
    lines = []
    for s in leftovers:
        until = "—"
        if s["expires_at"]:
            until = dt.datetime.fromisoformat(s["expires_at"]).astimezone(TASHKENT).strftime("%H:%M %d.%m")
        lines.append(t(lang, "reminder_line", dish=html.escape(s["recipe"] or "—"),
                       portions=f"{s['remaining']:g}", until=until))
    return t(lang, "reminder_leftovers", lines="\n".join(lines))


async def send_leftover_reminders(bot: Bot) -> int:
    """Разослать напоминание. Возвращает, скольким людям ушло сообщение."""
    leftovers = await collect_leftovers()
    if not leftovers:
        return 0
    base = get_settings().webapp_url
    sent = 0
    for u in await recipients():
        lang = u.language or "ru"
        kb = None
        if base:
            kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
                text=t(lang, "btn_leftovers"), web_app=WebAppInfo(url=f"{base}/app/cook?lang={lang}"))]])
        try:
            await bot.send_message(u.telegram_id, reminder_text(lang, leftovers), reply_markup=kb)
            sent += 1
        except Exception as exc:  # пользователь заблокировал бота и т.п. — остальным всё равно шлём
            log.warning("Reminder to %s failed: %s", u.telegram_id, exc)
    return sent


async def leftovers_reminder_loop(bot: Bot) -> None:
    """Фоновая задача: раз в день после REMINDER_HOUR по Ташкенту."""
    hour = get_settings().reminder_hour
    sent_on: dt.date | None = None
    while True:
        try:
            now = dt.datetime.now(TASHKENT)
            if now.hour >= hour and sent_on != now.date():
                sent_on = now.date()
                n = await send_leftover_reminders(bot)
                log.info("Leftovers reminder sent to %s users", n)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # напоминание — не критично, бот должен работать дальше
            log.exception("Leftovers reminder failed: %s", exc)
        await asyncio.sleep(CHECK_EVERY)
