"""Распознавание чеков (POST /api/parse-receipt) и ИИ-помощник закупщика (POST /api/ai/chat) через Gemini."""
from __future__ import annotations

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth import AuthUser, require_roles
from api.database import get_session
from api.gemini import (
    CHAT_MAX_CHARS,
    MAX_IMAGE_BYTES,
    GeminiRateLimited,
    GeminiUnavailable,
    ReceiptParseError,
    chat,
    parse_receipt,
)
from api.models import Purchase, Recipe, Stock
from api.services import match_product

router = APIRouter(prefix="/api", tags=["ai"])

ALLOWED_MIME = {"image/jpeg", "image/png", "image/webp", "image/heic", "image/heif"}


async def recognize_and_match(session: AsyncSession, image: bytes, mime: str) -> list[dict]:
    """
    Общая функция для API и бота: распознаёт чек и подбирает товар со склада.
    Ошибки Gemini переводятся в HTTP-коды: 503 (недоступен), 429 (лимит), 502 (мусор в ответе).
    """
    try:
        items = await parse_receipt(image, mime)
    except GeminiUnavailable:
        raise HTTPException(503, "Receipt recognition service is unavailable")
    except GeminiRateLimited:
        raise HTTPException(429, "Too many receipt requests, try again in a minute")
    except ReceiptParseError as exc:
        raise HTTPException(502, f"Could not parse receipt: {exc}")

    for item in items:
        product = await match_product(session, item["name"])
        item["product_id"] = product.id if product else None
        item["product_name"] = product.name if product else None
        item["stock_unit"] = product.unit if product else None
    return items


@router.post("/parse-receipt")
async def post_parse_receipt(
    file: UploadFile = File(..., description="Фото чека"),
    user: AuthUser = Depends(require_roles("purchaser")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    mime = (file.content_type or "image/jpeg").lower()
    if mime not in ALLOWED_MIME:
        raise HTTPException(400, "Unsupported image type")
    data = await file.read(MAX_IMAGE_BYTES + 1)
    if len(data) > MAX_IMAGE_BYTES:
        raise HTTPException(413, "Image is too large (max 10 MB)")
    if not data:
        raise HTTPException(400, "Empty file")
    return {"items": await recognize_and_match(session, data, mime)}


# ---------------------------------------------------------------- ИИ-помощник (свободный чат)

LANG_NAMES = {"ru": "русском", "uz": "узбекском (латиница)", "en": "английском", "zh": "китайском (упрощённом)"}


class ChatMessage(BaseModel):
    role: str = Field(pattern="^(user|assistant)$")
    text: str = Field(min_length=1, max_length=CHAT_MAX_CHARS)


class ChatIn(BaseModel):
    messages: list[ChatMessage] = Field(min_length=1, max_length=40)


async def build_chat_context(session: AsyncSession, lang: str) -> str:
    """Системная инструкция: роль помощника + актуальные данные кухни (склад, рецепты, закупки)."""
    stock = (await session.execute(select(Stock).order_by(Stock.name))).scalars().all()
    recipes = (await session.execute(select(Recipe).order_by(Recipe.name))).scalars().all()
    purchases = (
        await session.execute(select(Purchase).order_by(Purchase.date.desc(), Purchase.id.desc()).limit(15))
    ).scalars().all()

    stock_lines = "\n".join(
        f"- {s.name}: остаток {float(s.quantity or 0):g} {s.unit}, цена {float(s.price_per_unit or 0):,.0f} сум/{s.unit}".replace(",", " ")
        for s in stock
    ) or "- склад пуст"
    recipe_lines = "\n".join(
        f"- {r.name} (на {float(r.base_yield or 1):g} порц.): "
        + ", ".join(f"{i.product.name} {float(i.quantity):g} {i.unit}" for i in r.ingredients)
        for r in recipes
    ) or "- рецептов нет"
    purchase_lines = "\n".join(
        f"- {p.date:%d.%m.%Y} {p.product_name}: {float(p.quantity):g} {p.unit} за {float(p.total_price):,.0f} сум".replace(",", " ")
        for p in purchases
    ) or "- закупок пока нет"

    return (
        "Ты — ИИ-помощник закупщика кухни Kuxnya NVGR (Ташкент, валюта — сум). "
        f"Отвечай на {LANG_NAMES.get(lang, 'русском')} языке, коротко и по делу, дружелюбно, можно с эмодзи. "
        "Помогай: что и сколько купить под нужное число порций, сравнить цены, проверить остатки, "
        "составить список закупок, объяснить, как пользоваться приложением. "
        "Считай по данным ниже; если данных нет — честно скажи и не выдумывай цены и остатки. "
        "Сам ты ничего не сохраняешь: закупку вносят во вкладке «Ручной ввод» или по фото чека. "
        "Форматируй простым текстом: короткие абзацы и списки с «•».\n\n"
        f"СКЛАД (остатки и последние цены):\n{stock_lines}\n\n"
        f"РЕЦЕПТЫ (закладка на базовый выход):\n{recipe_lines}\n\n"
        f"ПОСЛЕДНИЕ ЗАКУПКИ:\n{purchase_lines}"
    )


@router.post("/ai/chat")
async def post_ai_chat(
    body: ChatIn,
    user: AuthUser = Depends(require_roles("purchaser")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Свободный диалог с ИИ. История хранится в приложении и присылается целиком (последние сообщения)."""
    history = [m.model_dump() for m in body.messages]
    if history[-1]["role"] != "user":
        raise HTTPException(400, "The last message must be from the user")
    system = await build_chat_context(session, user.language)
    try:
        answer = await chat(history, system)
    except GeminiUnavailable:
        raise HTTPException(503, "AI assistant is unavailable")
    except GeminiRateLimited:
        raise HTTPException(429, "Too many requests, try again in a minute")
    except ReceiptParseError:
        raise HTTPException(502, "AI returned an empty answer")
    return {"answer": answer}
