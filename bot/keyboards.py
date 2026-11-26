"""Inline-клавиатуры бота и фабрики callback-данных."""
from __future__ import annotations

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from aiogram.utils.keyboard import InlineKeyboardBuilder

from api.config import get_settings
from bot.translations import LANGUAGES, t


class RoleCB(CallbackData, prefix="role"):
    role: str


class LangCB(CallbackData, prefix="lang"):
    code: str


class ReceiptCB(CallbackData, prefix="rcpt"):
    """Кнопки подтверждения позиции чека: action = add | skip."""

    action: str
    draft_id: int
    idx: int


def webapp_url(role: str, lang: str, tab: str | None = None) -> str | None:
    """Ссылка на Mini App. Без WEBAPP_URL (HTTPS) кнопки не показываем."""
    base = get_settings().webapp_url
    if not base:
        return None
    url = f"{base}/app/{role}?lang={lang}"
    if tab:
        url += f"&tab={tab}"
    return url


def _webapp_button(text: str, role: str, lang: str, tab: str | None = None) -> InlineKeyboardButton | None:
    url = webapp_url(role, lang, tab)
    if not url:
        return None
    return InlineKeyboardButton(text=text, web_app=WebAppInfo(url=url))


def entry_url(lang: str) -> str | None:
    """Экран «Войти» Mini App: сам ведёт на экран роли или предлагает выбрать роль."""
    base = get_settings().webapp_url
    return f"{base}/app?lang={lang}" if base else None


def role_keyboard(lang: str) -> InlineKeyboardMarkup:
    """Выбор роли (только для владельца — проверить кабинеты)."""
    kb = InlineKeyboardBuilder()
    for role in ("owner", "cook", "purchaser", "client"):
        kb.button(text=t(lang, f"role_{role}"), callback_data=RoleCB(role=role))
    kb.adjust(2)
    return kb.as_markup()


def lang_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for code, title in LANGUAGES.items():
        kb.button(text=title, callback_data=LangCB(code=code))
    kb.adjust(2)
    return kb.as_markup()


def role_menu(role: str, lang: str) -> InlineKeyboardMarkup | None:
    """Меню роли в боте — одна кнопка «Открыть кабинет»: всё остальное уже внутри приложения."""
    btn = _webapp_button(t(lang, "btn_open_cabinet"), role, lang)
    return InlineKeyboardMarkup(inline_keyboard=[[btn]]) if btn else None


def receipt_item_keyboard(lang: str, draft_id: int, idx: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=t(lang, "btn_add"), callback_data=ReceiptCB(action="add", draft_id=draft_id, idx=idx).pack()
                ),
                InlineKeyboardButton(
                    text=t(lang, "btn_skip"), callback_data=ReceiptCB(action="skip", draft_id=draft_id, idx=idx).pack()
                ),
            ]
        ]
    )
