"""GET /api/recipes — рецепты с ингредиентами и текущими остатками."""
from __future__ import annotations

from decimal import Decimal

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth import AuthUser, require_roles
from api.database import get_session
from api.models import Recipe
from api.services import money, qty
from api.units import UnitMismatch, convert

router = APIRouter(prefix="/api", tags=["recipes"])


@router.get("/recipes")
async def get_recipes(
    user: AuthUser = Depends(require_roles("cook")),
    session: AsyncSession = Depends(get_session),
) -> list[dict]:
    """
    Для каждого ингредиента отдаём количество на базовый выход, остаток на складе
    и стоимость — фронтенд пересчитывает потребность под плановый выход.
    """
    recipes = (await session.execute(select(Recipe).order_by(Recipe.name))).scalars().all()
    result = []
    for r in recipes:
        ingredients = []
        cost_per_base = Decimal("0")
        for ing in r.ingredients:
            p = ing.product
            try:
                in_stock_unit = convert(Decimal(ing.quantity), ing.unit, p.unit)
                cost = in_stock_unit * Decimal(p.price_per_unit or 0)
                cost_per_base += cost
            except UnitMismatch:
                in_stock_unit, cost = None, None
            ingredients.append(
                {
                    "product_id": p.id,
                    "product": p.name,
                    "quantity": qty(ing.quantity),
                    "unit": ing.unit,
                    "quantity_in_stock_unit": qty(in_stock_unit) if in_stock_unit is not None else None,
                    "stock_unit": p.unit,
                    "stock_quantity": qty(p.quantity),
                    "cost": money(cost) if cost is not None else None,
                }
            )
        base_yield = Decimal(r.base_yield or 1)
        result.append(
            {
                "id": r.id,
                "name": r.name,
                "base_yield": qty(base_yield),
                "cost_per_base": money(cost_per_base),
                "cost_per_portion": money(cost_per_base / base_yield) if base_yield else 0.0,
                "ingredients": ingredients,
            }
        )
    return result
