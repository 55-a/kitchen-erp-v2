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
    kb = InlineKeyboardBuilder()
    url = entry_url(lang)
    if url:  # первой — большая кнопка входа в приложение
        kb.row(InlineKeyboardButton(text=t(lang, "btn_enter_app"), web_app=WebAppInfo(url=url)))
    for role in ("owner", "cook", "purchaser", "client"):
        kb.button(text=t(lang, f"role_{role}"), callback_data=RoleCB(role=role))
    kb.adjust(1, 2, 2) if url else kb.adjust(2)
    return kb.as_markup()


def lang_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for code, title in LANGUAGES.items():
        kb.button(text=title, callback_data=LangCB(code=code))
    kb.adjust(2)
    return kb.as_markup()


# Кнопки каждой роли: (ключ перевода, вкладка Mini App)
ROLE_BUTTONS: dict[str, list[tuple[str, str | None]]] = {
    "owner": [("btn_stats", "stats"), ("btn_clients", "clients")],
    "cook": [("btn_new_batch", "batch"), ("btn_recipes", "recipes")],
    "purchaser": [("btn_new_purchase", "purchase")],
    "client": [("btn_deliveries", "deliveries"), ("btn_invoice", "invoice"), ("btn_act", "act")],
}


def role_menu(role: str, lang: str, can_switch: bool = True) -> InlineKeyboardMarkup:
    """Меню роли: кнопки открывают нужную вкладку Mini App. «Сменить роль» — только владельцу."""
    rows: list[list[InlineKeyboardButton]] = []
    for key, tab in ROLE_BUTTONS.get(role, []):
        btn = _webapp_button(t(lang, key), role, lang, tab)
        if btn:
            rows.append([btn])
    if can_switch:
        rows.append([InlineKeyboardButton(text=t(lang, "btn_change_role"), callback_data="change_role")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def enter_keyboard(lang: str) -> InlineKeyboardMarkup | None:
    """Одна кнопка входа в приложение (для тех, кто ждёт роль)."""
    url = entry_url(lang)
    if not url:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=t(lang, "btn_enter_app"), web_app=WebAppInfo(url=url))]])


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
