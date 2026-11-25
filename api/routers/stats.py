"""GET /api/stats — статистика для владельца."""
from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth import AuthUser, require_roles
from api.database import get_session
from api.services import compute_stats

router = APIRouter(prefix="/api", tags=["stats"])


@router.get("/stats")
async def get_stats(
    date_from: dt.date | None = Query(None, description="Начало периода, YYYY-MM-DD"),
    date_to: dt.date | None = Query(None, description="Конец периода, YYYY-MM-DD"),
    user: AuthUser = Depends(require_roles("owner")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Выручка, себестоимость, прибыль, маржа (%), разбивка по месяцам и клиентам."""
    if date_from and date_to and date_from > date_to:
        raise HTTPException(400, "date_from must be before date_to")
    return await compute_stats(session, date_from, date_to)
