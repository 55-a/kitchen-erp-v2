"""Отправка файлов и сообщений ботом из API (PDF акта, отчёт, рецепты, меню)."""
from __future__ import annotations

import logging

from fastapi import HTTPException

from api.config import get_settings

log = logging.getLogger(__name__)


async def send_document(chat_id: int, data: bytes, filename: str, caption: str) -> None:
    """Бот присылает файл в личный чат. Ошибка Telegram → 502 (приложение покажет «Сервер недоступен»)."""
    from aiogram import Bot
    from aiogram.types import BufferedInputFile

    bot = Bot(token=get_settings().bot_token)
    try:
        await bot.send_document(chat_id=chat_id, document=BufferedInputFile(data, filename=filename), caption=caption)
    except Exception as exc:
        log.exception("Failed to send %s: %s", filename, exc)
        raise HTTPException(502, "Could not send the document via Telegram")
    finally:
        await bot.session.close()


# В тестах (NOTIFY_DISABLED=1) сообщения не уходят в Telegram, а складываются сюда
SENT: list[dict] = []


async def send_message(chat_id: int, text: str, *, button_text: str | None = None, button_url: str | None = None) -> bool:
    """Бот пишет человеку (HTML) с необязательной кнопкой, открывающей приложение.
    Ошибка не ломает запрос: человек мог не нажать /start или заблокировать бота."""
    import os

    if os.getenv("NOTIFY_DISABLED"):
        SENT.append({"chat_id": chat_id, "text": text, "button_text": button_text, "button_url": button_url})
        return True
    from aiogram import Bot
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo

    markup = None
    if button_text and button_url:
        markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=button_text, web_app=WebAppInfo(url=button_url))]])
    bot = Bot(token=get_settings().bot_token)
    try:
        await bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML", reply_markup=markup)
        return True
    except Exception as exc:
        log.warning("Could not message %s: %s", chat_id, exc)
        return False
    finally:
        await bot.session.close()
