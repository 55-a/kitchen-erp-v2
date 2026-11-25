"""POST /api/parse-receipt — распознавание фото чека через Gemini."""
from __future__ import annotations

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth import AuthUser, require_roles
from api.database import get_session
from api.gemini import (
    MAX_IMAGE_BYTES,
    GeminiRateLimited,
    GeminiUnavailable,
    ReceiptParseError,
    parse_receipt,
)
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
