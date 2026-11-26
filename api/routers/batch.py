"""POST /api/batch — новая партия (списание продуктов со склада)."""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth import AuthUser, require_roles
from api.database import get_session
from api.models import Batch, BatchItem
from api.services import create_batch, money, qty

router = APIRouter(prefix="/api", tags=["batch"])


class BatchItemIn(BaseModel):
    product_id: int
    quantity: Decimal = Field(ge=0, description="Фактический расход в единицах склада")


class BatchIn(BaseModel):
    recipe_id: int
    items: list[BatchItemIn] | None = Field(None, max_length=80, description="Фактический расход; без него — по техкарте")
    planned_yield: Decimal = Field(gt=0, description="Плановый выход, порций")
    actual_yield: Decimal | None = Field(None, ge=0, description="Фактический выход, порций")
    date: dt.date = Field(default_factory=dt.date.today)


@router.post("/batch", status_code=201)
async def post_batch(
    body: BatchIn,
    user: AuthUser = Depends(require_roles("cook")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Списание атомарное: либо все продукты списаны и партия создана, либо ничего (400)."""
    batch, consumed = await create_batch(
        session,
        recipe_id=body.recipe_id,
        planned_yield=body.planned_yield,
        actual_yield=body.actual_yield,
        date=body.date,
        created_by=user.telegram_id,
        items=[i.model_dump() for i in body.items] if body.items else None,
    )
    await session.commit()
    return {
        "id": batch.id,
        "recipe_id": batch.recipe_id,
        "planned_yield": qty(batch.planned_yield),
        "actual_yield": qty(batch.actual_yield) if batch.actual_yield is not None else None,
        "cost": money(batch.cost),
        "date": batch.date.isoformat(),
        "consumed": consumed,
    }


@router.get("/batch")
async def list_batches(
    limit: int = 20,
    user: AuthUser = Depends(require_roles("cook")),
    session: AsyncSession = Depends(get_session),
) -> list[dict]:
    """Последние партии — для истории на экране повара."""
    rows = (
        await session.execute(select(Batch).order_by(Batch.date.desc(), Batch.id.desc()).limit(min(limit, 100)))
    ).scalars().all()
    # Фактический расход по партиям — одним запросом
    lines = (await session.execute(select(BatchItem).where(BatchItem.batch_id.in_([b.id for b in rows])))).scalars().all()
    by_batch: dict[int, list] = {}
    for ln in lines:
        by_batch.setdefault(ln.batch_id, []).append(
            {"product": ln.product_name, "quantity": qty(ln.quantity), "unit": ln.unit,
             "planned": qty(ln.planned_quantity) if ln.planned_quantity is not None else None, "cost": money(ln.cost)}
        )
    return [
        {
            "id": b.id,
            "recipe_id": b.recipe_id,
            "recipe_version": b.recipe_version,
            "recipe": b.recipe.name if b.recipe else None,
            "cost_per_portion": money(Decimal(b.cost) / Decimal(b.actual_yield or b.planned_yield)) if (b.actual_yield or b.planned_yield) else 0.0,
            "items": by_batch.get(b.id, []),
            "created_at": b.created_at.isoformat() if b.created_at else None,
            "planned_yield": qty(b.planned_yield),
            "actual_yield": qty(b.actual_yield) if b.actual_yield is not None else None,
            "cost": money(b.cost),
            "date": b.date.isoformat(),
        }
        for b in rows
    ]
