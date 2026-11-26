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
