"""GET /api/stock — остатки на складе."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth import AuthUser, require_roles
from api.database import get_session
from api.models import Stock
from api.services import money, qty

router = APIRouter(prefix="/api", tags=["stock"])


def stock_to_dict(p: Stock) -> dict:
    return {
        "id": p.id,
        "name": p.name,
        "unit": p.unit,
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
    """Все товары склада по алфавиту."""
    rows = (await session.execute(select(Stock).order_by(Stock.name))).scalars().all()
    return [stock_to_dict(p) for p in rows]
