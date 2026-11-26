"""
Справочник сырья и остатки склада.

    GET  /api/stock            — сырьё с остатками и ценами (повар, закупщик, владелец)
    POST /api/stock            — «+ Новый товар»: добавить сырьё в справочник прямо из закупки или техкарты
    POST /api/stock/inventory  — инвентаризация: записать фактические остатки (было/стало сохраняется)
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth import AuthUser, require_roles
from api.database import get_session
from api.models import Stock, StockAdjustment
from api.services import ServiceError, money, qty
from api.units import normalize_unit

router = APIRouter(prefix="/api", tags=["stock"])

# Категории сырья (подписи — в static/js/i18n.js, ключи cat_*)
CATEGORIES = ("meat", "veg", "grain", "dairy", "oil", "spice", "bakery", "pack", "other")


def stock_to_dict(p: Stock) -> dict:
    return {
        "id": p.id,
        "name": p.name,
        "unit": p.unit,
        "category": p.category or "other",
        "quantity": qty(p.quantity),
        "price_per_unit": money(p.price_per_unit),
        "value": money((p.quantity or 0) * (p.price_per_unit or 0)),
        "updated_at": p.updated_at.isoformat() if p.updated_at else None,
    }


@router.get("/stock")
async def get_stock(
    user: AuthUser = Depends(require_roles("cook", "purchaser")),
    session: AsyncSession = Depends(get_session),
) -> list[dict]:
    """Всё сырьё по алфавиту."""
    rows = (await session.execute(select(Stock).order_by(Stock.name))).scalars().all()
    return [stock_to_dict(p) for p in rows]


class ProductIn(BaseModel):
    name: str = Field(min_length=2, max_length=255)
    unit: str = Field(min_length=1, max_length=20)
    category: str = Field("other", pattern="^(" + "|".join(CATEGORIES) + ")$")


@router.post("/stock", status_code=201)
async def create_product(
    body: ProductIn,
    user: AuthUser = Depends(require_roles("cook", "purchaser")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """
    «+ Новый товар». Остаток 0 и цена 0 — их заполнит первая закупка или инвентаризация.
    Дубликаты (без учёта регистра и пробелов) не создаём — возвращаем 409 с существующим товаром.
    """
    name = " ".join(body.name.split())
    # Сравниваем в Python через casefold: lower() в SQLite не понимает кириллицу, а дубликаты ловить надо везде
    key = name.casefold()
    existing = next((p for p in (await session.execute(select(Stock))).scalars().all()
                     if " ".join(p.name.split()).casefold() == key), None)
    if existing:
        raise ServiceError(409, "product_exists", "Такой товар уже есть в справочнике", {"product": stock_to_dict(existing)})
    product = Stock(name=name, unit=normalize_unit(body.unit), category=body.category,
                    quantity=Decimal("0"), price_per_unit=Decimal("0"))
    session.add(product)
    await session.commit()
    await session.refresh(product)
    return stock_to_dict(product)


class InventoryItem(BaseModel):
    product_id: int
    quantity: Decimal = Field(ge=0)
    price_per_unit: Decimal | None = Field(None, ge=0, description="Цена, если у товара её ещё нет")


class InventoryIn(BaseModel):
    items: list[InventoryItem] = Field(min_length=1, max_length=500)
    date: dt.date = Field(default_factory=dt.date.today)


@router.post("/stock/inventory")
async def post_inventory(
    body: InventoryIn,
    user: AuthUser = Depends(require_roles("cook", "purchaser")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """
    Инвентаризация: фактический остаток заменяет учётный. Для каждого изменения
    сохраняется запись «было → стало» (stock_adjustments). Цену можно задать
    только товару, у которого её ещё нет (новый товар или начальные остатки).
    """
    ids = sorted({i.product_id for i in body.items})
    products = {
        p.id: p
        for p in (
            await session.execute(select(Stock).where(Stock.id.in_(ids)).order_by(Stock.id).with_for_update())
        ).scalars().all()
    }
    missing = [i for i in ids if i not in products]
    if missing:
        raise ServiceError(400, "product_not_found", "Товар не найден в справочнике", {"ids": missing})

    changed = []
    for item in body.items:
        p = products[item.product_id]
        old = Decimal(p.quantity or 0)
        new = Decimal(item.quantity)
        if item.price_per_unit is not None and not Decimal(p.price_per_unit or 0):
            p.price_per_unit = Decimal(item.price_per_unit)
        if new == old:
            continue
        p.quantity = new
        session.add(StockAdjustment(
            product_id=p.id, product_name=p.name, old_quantity=old, new_quantity=new, delta=new - old,
            unit=p.unit, price_per_unit=Decimal(p.price_per_unit or 0), reason="inventory",
            date=body.date, created_by=user.telegram_id,
        ))
        changed.append({"product": p.name, "old": qty(old), "new": qty(new), "unit": p.unit})
    await session.commit()
    return {"changed": changed, "count": len(changed)}


class ProductPatch(BaseModel):
    name: str | None = Field(None, min_length=2, max_length=255)
    category: str | None = Field(None, pattern="^(" + "|".join(CATEGORIES) + ")$")


@router.patch("/stock/{product_id}")
async def patch_product(
    product_id: int,
    body: ProductPatch,
    user: AuthUser = Depends(require_roles("cook", "purchaser")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Переименовать товар или сменить категорию. Единицу не меняем — от неё зависят остатки и техкарты."""
    p = await session.get(Stock, product_id)
    if p is None:
        raise ServiceError(404, "product_not_found", "Товар не найден в справочнике")
    if body.name:
        name = " ".join(body.name.split())
        clash = next((x for x in (await session.execute(select(Stock))).scalars().all()
                      if x.id != p.id and " ".join(x.name.split()).casefold() == name.casefold()), None)
        if clash:
            raise ServiceError(409, "product_exists", "Такой товар уже есть в справочнике", {"product": stock_to_dict(clash)})
        p.name = name
    if body.category:
        p.category = body.category
    await session.commit()
    await session.refresh(p)
    return stock_to_dict(p)
