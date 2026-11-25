"""/start (выбор роли), /lang (выбор языка), /menu, /help."""
from __future__ import annotations

import html

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from api.config import get_settings
from api.database import session_scope
from api.models import User
from bot.keyboards import LangCB, RoleCB, lang_keyboard, role_keyboard, role_menu
from bot.translations import t
from bot.utils import can_take_role, effective_role, get_or_create_user, linked_client

router = Router(name="start")


async def build_menu(session: AsyncSession, user: User) -> tuple[str, object | None]:
    """Текст и клавиатура главного меню текущей роли пользователя."""
    lang = user.language
    role = effective_role(user)
    if role is None:
        return t(lang, "no_role"), role_keyboard(lang)
    if role == "client":
        client = await linked_client(session, user.telegram_id)
        if client is None and user.telegram_id in get_settings().owner_ids:
            # Владелец проверяет кабинет клиента: компанию выбирает в Mini App
            text = t(lang, "welcome_client_owner")
        elif client is None:
            return t(lang, "client_not_linked", tg_id=user.telegram_id), role_menu("none", lang)
        else:
            text = t(lang, "welcome_client", client=html.escape(client.name))
    else:
        text = t(lang, f"welcome_{role}")
    if not get_settings().webapp_url:
        text += "\n\n" + t(lang, "webapp_not_configured")
    return text, role_menu(role, lang)


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    """Приветствие и выбор роли."""
    async with session_scope() as session:
        user = await get_or_create_user(session, message.from_user)
        lang = user.language
    await message.answer(t(lang, "choose_role"), reply_markup=role_keyboard(lang))


@router.callback_query(F.data == "change_role")
async def cb_change_role(callback: CallbackQuery) -> None:
    async with session_scope() as session:
        user = await get_or_create_user(session, callback.from_user)
        lang = user.language
    await callback.message.answer(t(lang, "choose_role"), reply_markup=role_keyboard(lang))
    await callback.answer()


@router.callback_query(RoleCB.filter())
async def cb_choose_role(callback: CallbackQuery, callback_data: RoleCB) -> None:
    """Сохраняем роль, если она разрешена, и показываем меню."""
    async with session_scope() as session:
        user = await get_or_create_user(session, callback.from_user)
        if not can_take_role(user.telegram_id, callback_data.role):
            await callback.answer(t(user.language, "role_denied"), show_alert=True)
            return
        user.role = callback_data.role
        await session.flush()
        text, kb = await build_menu(session, user)
    await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer()


@router.message(Command("lang"))
async def cmd_lang(message: Message) -> None:
    async with session_scope() as session:
        user = await get_or_create_user(session, message.from_user)
        lang = user.language
    await message.answer(t(lang, "choose_lang"), reply_markup=lang_keyboard())


@router.callback_query(LangCB.filter())
async def cb_choose_lang(callback: CallbackQuery, callback_data: LangCB) -> None:
    """Сохраняем язык в БД — он используется в боте, Mini App и PDF."""
    async with session_scope() as session:
        user = await get_or_create_user(session, callback.from_user)
        user.language = callback_data.code if callback_data.code in {"ru", "en", "uz", "oz", "zh"} else "ru"
        await session.flush()
        text, kb = await build_menu(session, user)
        lang = user.language
    await callback.message.edit_text(t(lang, "lang_saved"))
    await callback.message.answer(text, reply_markup=kb)
    await callback.answer()


@router.message(Command("menu"))
async def cmd_menu(message: Message) -> None:
    async with session_scope() as session:
        user = await get_or_create_user(session, message.from_user)
        text, kb = await build_menu(session, user)
    await message.answer(text, reply_markup=kb)


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    async with session_scope() as session:
        user = await get_or_create_user(session, message.from_user)
        lang = user.language
    await message.answer(t(lang, "help"))
