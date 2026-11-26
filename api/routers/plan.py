"""
План / факт / прогноз продаж для владельца.

    GET    /api/plan?month=2026-11            — план, факт и прогноз на месяц (итого и по клиентам)
    PUT    /api/plan                          — задать план вручную (итого или по клиенту)
    DELETE /api/plan?month=2026-11&client=X   — убрать ручной план (снова считается автоматически)

Как считается (всё по рабочим дням пн–пт — кухня возит обеды в будни):
- Факт     — подтверждённые поставки клиентам за месяц (те же данные, что в статистике).
- План     — ручной, если владелец его задал; иначе автоматический:
             средние продажи за рабочий день за 3 месяца ДО начала месяца × рабочих дней в месяце.
- Прогноз  — факт + текущий темп месяца × оставшиеся рабочие дни.
             Пока прошло меньше 3 рабочих дней, темп берётся из истории (иначе прогноз «прыгает»).
Существующие данные не меняются: план хранится в отдельной таблице sales_plans.
"""
from __future__ import annotations

import datetime as dt
from collections import defaultdict
from decimal import ROUND_HALF_UP, Decimal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth import AuthUser, require_roles
from api.database import get_session
from api.models import ClientDelivery, SalesPlan
from api.services import money

router = APIRouter(prefix="/api", tags=["plan"])

HISTORY_DAYS = 92          # сколько дней истории берём для среднего (≈ 3 месяца)
MIN_DAYS_FOR_PACE = 3      # с какого рабочего дня доверяем темпу текущего месяца


def parse_month(value: str | None, today: dt.date) -> dt.date:
    if not value:
        return today.replace(day=1)
    try:
        y, m = value.split("-")
        return dt.date(int(y), int(m), 1)
    except (ValueError, TypeError):
        raise HTTPException(400, "month must be YYYY-MM")


def month_end(start: dt.date) -> dt.date:
    nxt = dt.date(start.year + (start.month == 12), start.month % 12 + 1, 1)
    return nxt - dt.timedelta(days=1)


def working_days(start: dt.date, end: dt.date) -> int:
    """Рабочие дни (пн–пт) от start до end включительно."""
    if end < start:
        return 0
    days = (end - start).days + 1
    full, rest = divmod(days, 7)
    count = full * 5
    for i in range(rest):
        if (start + dt.timedelta(days=full * 7 + i)).weekday() < 5:
            count += 1
    return count


async def _sum_by_client(session: AsyncSession, start: dt.date, end: dt.date) -> dict[str, dict]:
    rows = (
        await session.execute(
            select(ClientDelivery.client_name, ClientDelivery.portions, ClientDelivery.amount, ClientDelivery.date)
            .where(ClientDelivery.date >= start, ClientDelivery.date <= end)
        )
    ).all()
    out: dict[str, dict] = defaultdict(lambda: {"portions": 0, "revenue": Decimal("0"), "deliveries": 0, "first": None})
    for name, portions, amount, date in rows:
        c = out[name]
        c["portions"] += int(portions or 0)
        c["revenue"] += Decimal(amount or 0)
        c["deliveries"] += 1
        c["first"] = min(c["first"], date) if c["first"] else date
    return out


def _total(by_client: dict[str, dict]) -> dict:
    return {
        "portions": sum(c["portions"] for c in by_client.values()),
        "revenue": sum((c["revenue"] for c in by_client.values()), Decimal("0")),
        "deliveries": sum(c["deliveries"] for c in by_client.values()),
    }


def _q(v: Decimal) -> Decimal:
    return Decimal(v).quantize(Decimal("1"), rounding=ROUND_HALF_UP)


async def compute_plan(session: AsyncSession, month: dt.date, today: dt.date) -> dict:
    start, end = month, month_end(month)
    days_total = working_days(start, end)
    if today < start:
        days_passed = 0
    elif today > end:
        days_passed = days_total
    else:
        days_passed = working_days(start, today)
    days_left = days_total - days_passed

    # ---- Факт месяца
    fact_by_client = await _sum_by_client(session, start, end)
    fact_total = _total(fact_by_client)

    # ---- История для автоплана: до 3 месяцев перед началом месяца.
    # Считаем с первой поставки в этом окне, чтобы «пустые» недели до старта работы не занижали среднее.
    hist_start, hist_end = start - dt.timedelta(days=HISTORY_DAYS), start - dt.timedelta(days=1)
    hist_by_client = await _sum_by_client(session, hist_start, hist_end)
    hist_total = _total(hist_by_client)
    first_dates = [c["first"] for c in hist_by_client.values() if c["first"]]
    hist_from = min(first_dates) if first_dates else None
    hist_days = working_days(hist_from, hist_end) if hist_from else 0

    def daily(portions: int, revenue: Decimal, days: int) -> tuple[Decimal, Decimal]:
        if days <= 0:
            return Decimal("0"), Decimal("0")
        return Decimal(portions) / days, Decimal(revenue) / days

    # ---- Ручные планы
    manual = {
        p.client_name: p
        for p in (await session.execute(select(SalesPlan).where(SalesPlan.month == start))).scalars().all()
    }

    def plan_for(key: str, hist: dict) -> dict:
        if key in manual:
            p = manual[key]
            return {"portions": int(p.portions), "revenue": money(p.revenue), "source": "manual"}
        if not hist_days or not hist["portions"]:
            return {"portions": 0, "revenue": 0.0, "source": "none"}
        dp, dr = daily(hist["portions"], hist["revenue"], hist_days)
        return {"portions": int(_q(dp * days_total)), "revenue": money(_q(dr * days_total)), "source": "auto"}

    def forecast_for(fact: dict, hist: dict) -> dict:
        if days_left <= 0:
            return {"portions": fact["portions"], "revenue": money(fact["revenue"]), "pace": "final"}
        if days_passed >= MIN_DAYS_FOR_PACE:
            dp, dr = daily(fact["portions"], fact["revenue"], days_passed)
            pace = "month"
        else:
            dp, dr = daily(hist["portions"], hist["revenue"], hist_days)
            pace = "history"
        return {
            "portions": int(_q(fact["portions"] + dp * days_left)),
            "revenue": money(_q(Decimal(fact["revenue"]) + dr * days_left)),
            "pace": pace,
        }

    def pct(a: float, b: float) -> float | None:
        return round(float(a) / float(b) * 100, 1) if b else None

    empty = {"portions": 0, "revenue": Decimal("0"), "deliveries": 0}
    names = sorted(set(fact_by_client) | set(hist_by_client) | {k for k in manual if k})
    clients = []
    for name in names:
        fact = fact_by_client.get(name, empty)
        hist = hist_by_client.get(name, empty)
        plan = plan_for(name, hist)
        fc = forecast_for(fact, hist)
        clients.append({
            "client": name,
            "plan": plan,
            "fact": {"portions": fact["portions"], "revenue": money(fact["revenue"]), "deliveries": fact["deliveries"]},
            "forecast": fc,
            "fact_pct": pct(fact["portions"], plan["portions"]),
            "forecast_pct": pct(fc["portions"], plan["portions"]),
        })

    plan_total = plan_for("", hist_total)
    fc_total = forecast_for(fact_total, hist_total)
    hist_dp, hist_dr = daily(hist_total["portions"], hist_total["revenue"], hist_days)
    fact_dp, fact_dr = daily(fact_total["portions"], fact_total["revenue"], days_passed)
    return {
        "month": f"{start:%Y-%m}",
        "period": {"from": start.isoformat(), "to": end.isoformat()},
        "today": today.isoformat(),
        "days": {"total": days_total, "passed": days_passed, "left": days_left},
        "plan": plan_total,
        "fact": {"portions": fact_total["portions"], "revenue": money(fact_total["revenue"]), "deliveries": fact_total["deliveries"]},
        "forecast": fc_total,
        "fact_pct": pct(fact_total["portions"], plan_total["portions"]),
        "forecast_pct": pct(fc_total["portions"], plan_total["portions"]),
        "clients": clients,
        # Данные для окошка «откуда эти цифры»
        "explain": {
            "history_from": hist_from.isoformat() if hist_from else None,
            "history_to": hist_end.isoformat(),
            "history_days": hist_days,
            "history_portions": hist_total["portions"],
            "history_revenue": money(hist_total["revenue"]),
            "history_daily_portions": float(round(hist_dp, 1)),
            "history_daily_revenue": money(_q(hist_dr)),
            "month_daily_portions": float(round(fact_dp, 1)),
            "month_daily_revenue": money(_q(fact_dr)),
        },
    }


@router.get("/plan")
async def get_plan(
    month: str | None = Query(None, description="YYYY-MM, по умолчанию — текущий месяц"),
    user: AuthUser = Depends(require_roles("owner")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    today = dt.date.today()
    return await compute_plan(session, parse_month(month, today), today)


class PlanIn(BaseModel):
    month: str = Field(pattern=r"^\d{4}-\d{2}$")
    client: str | None = Field(None, max_length=255, description="Пусто — план по кухне в целом")
    portions: int = Field(ge=0, le=1_000_000)
    revenue: Decimal = Field(ge=0)


@router.put("/plan")
async def put_plan(
    body: PlanIn,
    user: AuthUser = Depends(require_roles("owner")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    today = dt.date.today()
    start = parse_month(body.month, today)
    key = (body.client or "").strip()
    row = (
        await session.execute(select(SalesPlan).where(SalesPlan.month == start, SalesPlan.client_name == key))
    ).scalar_one_or_none()
    if row is None:
        row = SalesPlan(month=start, client_name=key)
        session.add(row)
    row.portions, row.revenue, row.created_by = body.portions, body.revenue, user.telegram_id
    await session.commit()
    return await compute_plan(session, start, today)


@router.delete("/plan")
async def delete_plan(
    month: str = Query(...),
    client: str | None = Query(None),
    user: AuthUser = Depends(require_roles("owner")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    today = dt.date.today()
    start = parse_month(month, today)
    await session.execute(
        delete(SalesPlan).where(SalesPlan.month == start, SalesPlan.client_name == (client or "").strip())
    )
    await session.commit()
    return await compute_plan(session, start, today)
