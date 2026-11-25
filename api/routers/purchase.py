"""POST /api/purchase — новая закупка (увеличивает остаток на складе)."""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth import AuthUser, require_roles
from api.database import get_session
from api.models import Purchase
from api.routers.stock import stock_to_dict
from api.services import add_purchase, money, qty

router = APIRouter(prefix="/api", tags=["purchase"])


class PurchaseIn(BaseModel):
    product_id: int | None = None
    product_name: str | None = Field(None, max_length=255)
    quantity: Decimal = Field(gt=0)
    unit: str | None = Field(None, max_length=20)
    total_price: Decimal = Field(ge=0)
    payment_method: str = Field("cash", pattern="^(cash|card|transfer)$")
    date: dt.date = Field(default_factory=dt.date.today)

    @model_validator(mode="after")
    def _need_product(self):
        if not self.product_id and not (self.product_name or "").strip():
            raise ValueError("product_id or product_name is required")
        return self


@router.post("/purchase", status_code=201)
async def post_purchase(
    body: PurchaseIn,
    user: AuthUser = Depends(require_roles("purchaser")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """
    Сервер сам проверяет, что товар есть на складе: если нет — 400 product_not_found.
    Фронтенд показывает успех ТОЛЬКО после ответа 201.
    """
    purchase, product = await add_purchase(
        session,
        product_id=body.product_id,
        product_name=body.product_name,
        quantity=body.quantity,
        unit=body.unit,
        total_price=body.total_price,
        payment_method=body.payment_method,
        date=body.date,
        created_by=user.telegram_id,
    )
    await session.commit()
    return {
        "id": purchase.id,
        "product_name": purchase.product_name,
        "quantity": qty(purchase.quantity),
        "unit": purchase.unit,
        "total_price": money(purchase.total_price),
        "date": purchase.date.isoformat(),
        "stock": stock_to_dict(product),
    }


@router.get("/purchase")
async def list_purchases(
    limit: int = 20,
    user: AuthUser = Depends(require_roles("purchaser")),
    session: AsyncSession = Depends(get_session),
) -> list[dict]:
    """Последние закупки."""
    rows = (
        await session.execute(
            select(Purchase).order_by(Purchase.date.desc(), Purchase.id.desc()).limit(min(limit, 100))
        )
    ).scalars().all()
    return [
        {
            "id": p.id,
            "product_name": p.product_name,
            "quantity": qty(p.quantity),
            "unit": p.unit,
            "total_price": money(p.total_price),
            "payment_method": p.payment_method,
            "date": p.date.isoformat(),
        }
        for p in rows
    ]
