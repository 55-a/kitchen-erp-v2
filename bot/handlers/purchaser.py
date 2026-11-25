"""
Закупщик: фото чека прямо в чат.

1. Закупщик присылает фото (или изображение файлом).
2. Бот отправляет его в Gemini, подбирает товары со склада.
3. Распознанные позиции сохраняются в receipt_drafts (не теряются при перезапуске).
4. Для каждой позиции — кнопки "✅ Добавить" / "✕ Пропустить".
   "Добавить" проводит закупку той же функцией, что и POST /api/purchase.
"""
from __future__ import annotations

import datetime as dt
import html
import logging
from decimal import Decimal

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery, Message
from fastapi import HTTPException

from api.database import session_scope
from api.models import ReceiptDraft
from api.pdf_generator import fmt_money
from api.routers.ai import ALLOWED_MIME, recognize_and_match
from api.services import ServiceError, add_purchase
from bot.keyboards import ReceiptCB, receipt_item_keyboard
from bot.translations import t
from bot.utils import effective_role, get_or_create_user

log = logging.getLogger(__name__)
router = Router(name="purchaser")

# HTTP-код ошибки распознавания → ключ перевода
_ERROR_KEYS = {503: "gemini_unavailable", 429: "gemini_rate_limited", 502: "receipt_parse_error"}


def _item_text(lang: str, item: dict) -> str:
    match = html.escape(item["product_name"]) if item.get("product_name") else t(lang, "receipt_no_match")
    return t(
        lang,
        "receipt_item",
        name=html.escape(item["name"]),
        qty=item["quantity"],
        unit=html.escape(item["unit"]),
        price=fmt_money(item["total_price"]),
        match=match,
    )


async def _download_image(bot: Bot, message: Message) -> tuple[bytes, str] | None:
    """Скачивает фото максимального размера или изображение-документ."""
    if message.photo:
        buf = await bot.download(message.photo[-1])
        return buf.read(), "image/jpeg"
    doc = message.document
    if doc and (doc.mime_type or "").lower() in ALLOWED_MIME:
        buf = await bot.download(doc)
        return buf.read(), doc.mime_type.lower()
    return None


@router.message(F.photo | (F.document & F.document.mime_type.startswith("image/")))
async def on_receipt_photo(message: Message, bot: Bot) -> None:
    async with session_scope() as session:
        user = await get_or_create_user(session, message.from_user)
        lang = user.language
        if effective_role(user) not in {"purchaser", "owner"}:
            await message.answer(t(lang, "photo_not_allowed"))
            return

    downloaded = await _download_image(bot, message)
    if downloaded is None:
        await message.answer(t(lang, "receipt_parse_error"))
        return
    image, mime = downloaded

    status_msg = await message.answer(t(lang, "receipt_processing"))
    try:
        async with session_scope() as session:
            items = await recognize_and_match(session, image, mime)
            if not items:
                await status_msg.edit_text(t(lang, "receipt_empty"))
                return
            for item in items:
                item["status"] = "pending"
            draft = ReceiptDraft(telegram_id=message.from_user.id, items=items)
            session.add(draft)
            await session.flush()
            draft_id = draft.id
    except HTTPException as exc:
        await status_msg.edit_text(t(lang, _ERROR_KEYS.get(exc.status_code, "error_generic")))
        return
    except Exception:
        log.exception("Receipt processing failed")
        await status_msg.edit_text(t(lang, "error_generic"))
        return

    await status_msg.edit_text(t(lang, "receipt_header", count=len(items)))
    for idx, item in enumerate(items):
        await message.answer(_item_text(lang, item), reply_markup=receipt_item_keyboard(lang, draft_id, idx))


@router.callback_query(ReceiptCB.filter())
async def on_receipt_decision(callback: CallbackQuery, callback_data: ReceiptCB) -> None:
    """Обработка "✅ Добавить" / "✕ Пропустить" для одной позиции чека."""
    result_line = ""
    async with session_scope() as session:
        user = await get_or_create_user(session, callback.from_user)
        lang = user.language
        if effective_role(user) not in {"purchaser", "owner"}:
            await callback.answer(t(lang, "photo_not_allowed"), show_alert=True)
            return

        # Блокируем черновик: двойное нажатие не проведёт закупку дважды
        draft = await session.get(ReceiptDraft, callback_data.draft_id, with_for_update=True)
        if draft is None or draft.telegram_id != callback.from_user.id or callback_data.idx >= len(draft.items):
            await callback.answer(t(lang, "error_generic"), show_alert=True)
            return
        items = [dict(i) for i in draft.items]  # копия — чтобы SQLAlchemy увидел изменение JSON
        item = items[callback_data.idx]
        if item.get("status") != "pending":
            await callback.answer(t(lang, "item_already"))
            return

        if callback_data.action == "skip":
            item["status"] = "skipped"
            result_line = t(lang, "item_skipped", name=html.escape(item["name"]))
        else:
            if not item.get("product_id"):
                await callback.answer(t(lang, "product_not_matched", name=item["name"]), show_alert=True)
                return
            try:
                purchase, product = await add_purchase(
                    session,
                    product_id=item["product_id"],
                    product_name=None,
                    quantity=Decimal(str(item["quantity"])),
                    unit=item["unit"],
                    total_price=Decimal(str(item["total_price"])),
                    payment_method="cash",
                    date=dt.date.today(),
                    created_by=callback.from_user.id,
                )
            except ServiceError as exc:
                await callback.answer(f"❌ {exc.message}", show_alert=True)
                return
            item["status"] = "added"
            item["purchase_id"] = purchase.id
            result_line = t(
                lang, "item_added", name=html.escape(product.name), qty=item["quantity"], unit=html.escape(item["unit"])
            )
        draft.items = items

    await callback.message.edit_text(_item_text(lang, item) + "\n\n" + result_line, reply_markup=None)
    await callback.answer()
