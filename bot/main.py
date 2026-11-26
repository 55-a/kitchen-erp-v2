"""
Telegram-бот Kuxnya NVGR на aiogram 3.

Запуск отдельно:      python -m bot.main
Запуск внутри API:    RUN_BOT_IN_API=true (по умолчанию) — см. api/main.py
ВАЖНО: не запускайте оба варианта одновременно с одним токеном —
Telegram вернёт ошибку Conflict (одновременно возможен только один getUpdates).
"""
from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand, ErrorEvent, MenuButtonWebApp, WebAppInfo

from api.config import get_settings
from api.database import init_db
from bot.handlers import client, cook, owner, purchaser, start
from bot.reminders import leftovers_reminder_loop
from bot.translations import t

log = logging.getLogger("kitchen-bot")


def create_bot() -> Bot:
    settings = get_settings()
    if not settings.bot_token:
        raise RuntimeError("BOT_TOKEN is not set")
    return Bot(token=settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))


def create_dispatcher() -> Dispatcher:
    """Собирает диспетчер. Порядок важен: start первым (команды /start, /lang)."""
    dp = Dispatcher()
    dp.include_routers(start.router, owner.router, cook.router, purchaser.router, client.router)

    @dp.errors()
    async def on_error(event: ErrorEvent) -> bool:
        """Любая необработанная ошибка: пишем в лог и вежливо отвечаем пользователю."""
        log.exception("Unhandled bot error: %s", event.exception)
        update = event.update
        target = update.message or (update.callback_query.message if update.callback_query else None)
        user = update.message.from_user if update.message else (
            update.callback_query.from_user if update.callback_query else None
        )
        if target is not None:
            try:
                await target.answer(t(user.language_code if user else None, "error_generic"))
            except Exception:  # noqa: BLE001 — не даём ошибке в обработчике ошибок уронить бота
                pass
        return True

    return dp


async def set_commands(bot: Bot) -> None:
    """Меню команд в Telegram на всех поддерживаемых языках."""
    descriptions = {
        "ru": ("Выбор роли", "Язык", "Меню", "Помощь"),
        "en": ("Choose role", "Language", "Menu", "Help"),
        "uz": ("Rol tanlash", "Til", "Menyu", "Yordam"),
        "zh": ("选择角色", "语言", "菜单", "帮助"),
    }
    for lang, (d_start, d_lang, d_menu, d_help) in descriptions.items():
        commands = [
            BotCommand(command="start", description=d_start),
            BotCommand(command="lang", description=d_lang),
            BotCommand(command="menu", description=d_menu),
            BotCommand(command="help", description=d_help),
        ]
        await bot.set_my_commands(commands, language_code=None if lang == "ru" else lang)

    # Кнопка меню по умолчанию — «Войти» в Mini App (у каждого чата после /start — на его языке)
    base = get_settings().webapp_url
    if base:
        await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="Войти", web_app=WebAppInfo(url=f"{base}/app")))


async def run_polling() -> None:
    """Запускает long polling. Используется и из api/main.py, и при запуске модуля."""
    await init_db()
    bot = create_bot()
    dp = create_dispatcher()
    # Вечернее напоминание повару об остатках (фоновая задача в том же процессе)
    reminder = asyncio.create_task(leftovers_reminder_loop(bot))
    try:
        await set_commands(bot)
        # Сбрасываем накопившиеся апдейты, чтобы после простоя бот не "отвечал на старое"
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot, handle_signals=False)
    finally:
        reminder.cancel()
        await bot.session.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    asyncio.run(run_polling())
