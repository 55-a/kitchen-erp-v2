"""Тесты команд бота /start и /lang (без обращения к Telegram — сообщения подменяются моками)."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.types import User as TgUser
from sqlalchemy import select

from api.models import User
from bot.handlers.start import cb_choose_lang, cb_choose_role, cmd_lang, cmd_start
from bot.keyboards import LangCB, RoleCB
from bot.translations import TRANSLATIONS, t
from tests.conftest import COOK_ID, OWNER_ID

pytestmark = pytest.mark.asyncio(loop_scope="session")


def tg_user(user_id: int, lang: str = "ru") -> TgUser:
    return TgUser(id=user_id, is_bot=False, first_name="Test", language_code=lang)


def fake_message(user_id: int, lang: str = "ru") -> MagicMock:
    msg = MagicMock()
    msg.from_user = tg_user(user_id, lang)
    msg.answer = AsyncMock()
    msg.edit_text = AsyncMock()
    return msg


def fake_callback(user_id: int, lang: str = "ru") -> MagicMock:
    cb = MagicMock()
    cb.from_user = tg_user(user_id, lang)
    cb.message = fake_message(user_id, lang)
    cb.answer = AsyncMock()
    return cb


async def get_user(db, telegram_id: int) -> User | None:
    async with db() as s:
        return (await s.execute(select(User).where(User.telegram_id == telegram_id))).scalar_one_or_none()


def keyboard_texts(markup) -> list[str]:
    return [b.text for row in markup.inline_keyboard for b in row]


# ------------------------------------------------------------------ /start

async def test_start_creates_user_and_shows_roles(db):
    msg = fake_message(500, lang="en")
    await cmd_start(msg)

    text = msg.answer.call_args.args[0]
    markup = msg.answer.call_args.kwargs["reply_markup"]
    assert text == t("en", "choose_role")  # язык взят из Telegram
    assert keyboard_texts(markup) == [t("en", f"role_{r}") for r in ("owner", "cook", "purchaser", "client")]

    user = await get_user(db, 500)
    assert user is not None and user.language == "en" and user.role is None


async def test_owner_role_is_denied_for_regular_user(db):
    cb = fake_callback(501)
    await cb_choose_role(cb, RoleCB(role="owner"))
    cb.answer.assert_awaited_with(t("ru", "role_denied"), show_alert=True)
    assert (await get_user(db, 501)).role is None


async def test_owner_role_allowed_for_owner_ids(db):
    cb = fake_callback(OWNER_ID)
    await cb_choose_role(cb, RoleCB(role="owner"))
    assert (await get_user(db, OWNER_ID)).role == "owner"


async def test_owner_can_switch_to_other_roles_and_back(db):
    from api.auth import resolve_role
    from api.config import get_settings

    settings = get_settings()
    assert resolve_role(OWNER_ID, None, settings) == "owner"
    for role in ("cook", "purchaser", "client", "owner"):
        cb = fake_callback(OWNER_ID)
        await cb_choose_role(cb, RoleCB(role=role))
        stored = (await get_user(db, OWNER_ID)).role
        assert stored == role
        assert resolve_role(OWNER_ID, stored, settings) == role
    markup = cb.message.edit_text.call_args.kwargs["reply_markup"]
    assert t("ru", "btn_stats") in keyboard_texts(markup)


async def test_cook_role_shows_cook_buttons(db):
    cb = fake_callback(COOK_ID)
    await cb_choose_role(cb, RoleCB(role="cook"))
    markup = cb.message.edit_text.call_args.kwargs["reply_markup"]
    texts = keyboard_texts(markup)
    assert t("ru", "btn_new_batch") in texts and t("ru", "btn_recipes") in texts
    # Кнопки открывают Mini App по HTTPS
    urls = [b.web_app.url for row in markup.inline_keyboard for b in row if b.web_app]
    assert urls and all(u.startswith("https://example.test/app/cook") for u in urls)


# ------------------------------------------------------------------ /lang

async def test_lang_shows_all_languages(db):
    msg = fake_message(502)
    await cmd_lang(msg)
    markup = msg.answer.call_args.kwargs["reply_markup"]
    assert len(keyboard_texts(markup)) == 4


async def test_lang_choice_is_saved_to_db(db):
    await cmd_start(fake_message(503))
    cb = fake_callback(503)
    await cb_choose_lang(cb, LangCB(code="zh"))
    assert (await get_user(db, 503)).language == "zh"
    cb.message.edit_text.assert_awaited_with(t("zh", "lang_saved"))


async def test_unknown_lang_falls_back_to_ru(db):
    cb = fake_callback(504)
    await cb_choose_lang(cb, LangCB(code="xx"))
    assert (await get_user(db, 504)).language == "ru"


async def test_all_languages_have_same_keys():
    """Во всех языках одинаковый набор ключей — иначе часть интерфейса останется непереведённой."""
    base = set(TRANSLATIONS["ru"])
    for lang, items in TRANSLATIONS.items():
        assert set(items) == base, f"{lang}: {base ^ set(items)}"
