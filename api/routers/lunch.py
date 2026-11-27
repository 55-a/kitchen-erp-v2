"""
Обед на сегодня — простой экран повара и заявки клиентов.

Как это работает (у кухни один обед в день, например «Плов + пирожки», клиенты — фирмы):
1. Клиент в своём кабинете говорит, сколько человек будет обедать: «Завтра нас: 85».
   Поменять можно до ORDER_DEADLINE_HOUR (17:00 по Ташкенту) накануне.
   Не сказал — берём его обычное количество (среднее по последним обедам).
2. Повар видит: «Готовить: сумма заявок + запас LUNCH_BUFFER_PCT %» и идёт по шагам:
   🍽 что на обед → ✅ приготовил N порций → 🚚 отвёз (по клиентам) → 📦 сколько осталось.
   Склад повар не ведёт: продукты списываются по рецептам сами, нехватка не блокирует.
3. «↩ Отменить» откатывает последний шаг, если повар ошибся.

    GET  /api/client/order            — заявка клиента: сегодня и ближайший день, который ещё можно поменять
    PUT  /api/client/order            — «Завтра нас: N»
    GET  /api/cook/day                — экран «Сегодня»: люди, сколько готовить, блюда, шаг
    PUT  /api/cook/day/dishes         — что на обед (рецепты)
    POST /api/cook/day/cooked         — «Приготовил N порций» (партия по каждому блюду)
    POST /api/cook/day/delivered      — «Отвёз»: одна поставка на клиента (выручка по его цене)
    POST /api/cook/day/leftover       — «Осталось N порций»
    POST /api/cook/day/undo           — отменить последний шаг
    POST /api/cook/shortage           — «🛒 Закончилось»: сообщение закупщику и владельцу
"""
from __future__ import annotations

import datetime as dt
import html
import math
from collections import defaultdict
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth import AuthUser, require_roles, resolve_role
from api.config import get_settings
from api.database import get_session
from api.models import Batch, BatchItem, BatchMovement, Client, ClientDelivery, ClientOrder, MenuItem, Recipe, Stock, User
from api.routers.client import resolve_client
from api.routers.kitchen import CENT, batch_states, client_price, cost_per_portion
from api.services import ServiceError, create_batch, money

router = APIRouter(prefix="/api", tags=["lunch"])

TASHKENT = dt.timezone(dt.timedelta(hours=5))   # в Узбекистане нет перехода на летнее время


def local_now() -> dt.datetime:
    return dt.datetime.now(TASHKENT)


def local_today() -> dt.date:
    return local_now().date()


# ================================================================ Заявки клиентов

def order_deadline(day: dt.date) -> dt.datetime:
    """До какого момента можно менять заявку на этот день: накануне в ORDER_DEADLINE_HOUR."""
    hour = get_settings().order_deadline_hour
    return dt.datetime.combine(day - dt.timedelta(days=1), dt.time(hour), tzinfo=TASHKENT)


def can_change(day: dt.date, now: dt.datetime) -> bool:
    return day.weekday() < 5 and now < order_deadline(day)


def next_order_day(now: dt.datetime) -> dt.date:
    """Ближайший рабочий день (пн–пт), заявку на который ещё можно поменять."""
    day = now.date() + dt.timedelta(days=1)
    while not can_change(day, now):
        day += dt.timedelta(days=1)
    return day


async def usual_portions(session: AsyncSession, client_name: str) -> int:
    """Обычное количество клиента: среднее по 10 последним дням с поставками.
    В день берём максимум по строкам — раньше одна поставка могла записываться по каждому блюду."""
    rows = (
        await session.execute(
            select(ClientDelivery.date, ClientDelivery.portions)
            .where(ClientDelivery.client_name == client_name, ClientDelivery.portions > 0)
            .order_by(ClientDelivery.date.desc()).limit(60)
        )
    ).all()
    per_day: dict[dt.date, int] = {}
    for d, p in rows:
        per_day[d] = max(per_day.get(d, 0), int(p or 0))
    last = sorted(per_day.items(), reverse=True)[:10]
    return round(sum(p for _, p in last) / len(last)) if last else 0


async def _orders_for(session: AsyncSession, day: dt.date) -> dict[str, int]:
    rows = (await session.execute(select(ClientOrder).where(ClientOrder.date == day))).scalars().all()
    return {o.client_name: int(o.portions) for o in rows}


async def _order_state(session: AsyncSession, client: Client) -> dict:
    now = local_now()
    today, nxt = now.date(), next_order_day(now)
    usual = await usual_portions(session, client.name)
    rows = (
        await session.execute(
            select(ClientOrder).where(ClientOrder.client_name == client.name, ClientOrder.date.in_([today, nxt]))
        )
    ).scalars().all()
    by_day = {o.date: int(o.portions) for o in rows}
    return {
        "client": client.name,
        "usual": usual,
        "today": {"date": today.isoformat(), "weekday": today.weekday(), "portions": by_day.get(today, usual),
                  "ordered": today in by_day},
        "next": {"date": nxt.isoformat(), "weekday": nxt.weekday(), "portions": by_day.get(nxt),
                 "deadline": order_deadline(nxt).isoformat()},
    }


@router.get("/client/order")
async def get_order(
    tg_id: int | None = Query(None),
    client: str | None = Query(None),
    user: AuthUser = Depends(require_roles("client")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    return await _order_state(session, await resolve_client(session, user, tg_id, client))


class OrderIn(BaseModel):
    date: dt.date
    portions: int = Field(ge=0, le=5000)
    client: str | None = Field(None, max_length=255)


@router.put("/client/order")
async def put_order(
    body: OrderIn,
    user: AuthUser = Depends(require_roles("client")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """«Завтра нас: N». Клиент меняет заявку только до срока; владелец — в любой день, начиная с сегодня."""
    c = await resolve_client(session, user, None, body.client)
    now = local_now()
    if user.is_owner_account:
        if body.date < now.date():
            raise ServiceError(400, "order_closed", "Этот день уже прошёл")
    elif not can_change(body.date, now):
        raise ServiceError(400, "order_closed", "Заявку на этот день уже не поменять",
                           {"deadline": order_deadline(body.date).isoformat()})
    row = (
        await session.execute(
            select(ClientOrder).where(ClientOrder.date == body.date, ClientOrder.client_name == c.name)
        )
    ).scalar_one_or_none()
    if row is None:
        session.add(ClientOrder(date=body.date, client_name=c.name, portions=body.portions, updated_by=user.telegram_id))
    else:
        row.portions = body.portions
        row.updated_by = user.telegram_id
    await session.commit()
    return await _order_state(session, c)


# ================================================================ Экран повара «Сегодня»

async def _today_batches(session: AsyncSession, day: dt.date) -> list[Batch]:
    return list((await session.execute(select(Batch).where(Batch.date == day).order_by(Batch.id))).scalars().all())


async def _movements(session: AsyncSession, batch_ids: list[int]) -> list[BatchMovement]:
    if not batch_ids:
        return []
    return list((await session.execute(
        select(BatchMovement).where(BatchMovement.batch_id.in_(batch_ids)).order_by(BatchMovement.id))).scalars().all())


async def day_state(session: AsyncSession) -> dict:
    """Всё для экрана «Сегодня»: кто обедает, сколько готовить, блюда и текущий шаг."""
    settings = get_settings()
    today = local_today()
    orders = await _orders_for(session, today)
    people = []
    for c in (await session.execute(select(Client).order_by(Client.name))).scalars().all():
        if c.name in orders:
            people.append({"client": c.name, "portions": orders[c.name], "ordered": True})
        else:
            usual = await usual_portions(session, c.name)
            if usual:
                people.append({"client": c.name, "portions": usual, "ordered": False})
    total = sum(p["portions"] for p in people)
    to_cook = math.ceil(total * (100 + settings.lunch_buffer_pct) / 100) if total else 0

    menu = (await session.execute(select(MenuItem).where(MenuItem.date == today).order_by(MenuItem.id))).scalars().all()
    batches = await _today_batches(session, today)
    states = await batch_states(session, batches)
    by_recipe = {b.recipe_id: b for b in batches}
    dishes, seen = [], set()
    for m in menu:
        if m.recipe_id in seen:
            continue
        seen.add(m.recipe_id)
        b = by_recipe.get(m.recipe_id)
        dishes.append({"recipe_id": m.recipe_id, "recipe": m.recipe.name if m.recipe else "—",
                       "batch_id": b.id if b else None})
    # Партии, сваренные в старом экране без меню, тоже показываем
    for b in batches:
        if b.recipe_id not in seen:
            seen.add(b.recipe_id)
            dishes.append({"recipe_id": b.recipe_id, "recipe": b.recipe.name if b.recipe else "—", "batch_id": b.id})

    moves = await _movements(session, [b.id for b in batches])
    lead = batches[0].id if batches else None
    delivered: dict[str, float] = defaultdict(float)
    leftover = 0.0
    for mv in moves:
        if mv.batch_id != lead:
            continue
        if mv.kind == "client":
            delivered[mv.client_name or "—"] += float(mv.portions)
        elif mv.kind == "waste" and mv.reason == "leftover":
            leftover += float(mv.portions)
    remaining = max((states[b.id]["remaining"] for b in batches), default=0.0)
    cooked = max((states[b.id]["yield"] for b in batches), default=0.0)

    if not dishes:
        step = "dishes"
    elif any(d["batch_id"] is None for d in dishes):
        step = "cook"
    elif not delivered:
        step = "deliver"
    elif remaining > 0:
        step = "leftover"
    else:
        step = "done"
    return {
        "date": today.isoformat(),
        "weekday": today.weekday(),
        "people": people,
        "people_total": total,
        "buffer_pct": settings.lunch_buffer_pct,
        "to_cook": to_cook,
        "dishes": dishes,
        "cooked": cooked,
        "delivered": [{"client": k, "portions": v} for k, v in delivered.items()],
        "delivered_total": sum(delivered.values()),
        "remaining": remaining,
        "leftover": leftover,
        "step": step,
        "can_undo": bool(batches),
    }


@router.get("/cook/day")
async def get_day(
    user: AuthUser = Depends(require_roles("cook")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    return await day_state(session)


class DishesIn(BaseModel):
    recipe_ids: list[int] = Field(max_length=8)


@router.put("/cook/day/dishes")
async def put_dishes(
    body: DishesIn,
    user: AuthUser = Depends(require_roles("cook")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Что сегодня на обед. Блюдо, которое уже сварили, убрать нельзя (сначала «Отменить»)."""
    today = local_today()
    wanted = list(dict.fromkeys(body.recipe_ids))
    found = (await session.execute(select(Recipe).where(Recipe.id.in_(wanted)))).scalars().all()
    if len(found) != len(wanted):
        raise ServiceError(404, "recipe_not_found", "Рецепт не найден")
    cooked = {b.recipe_id for b in await _today_batches(session, today)}
    current = (await session.execute(select(MenuItem).where(MenuItem.date == today))).scalars().all()
    for m in current:
        if m.recipe_id not in wanted and m.recipe_id not in cooked:
            await session.delete(m)
    have = {m.recipe_id for m in current}
    for rid in wanted:
        if rid not in have:
            session.add(MenuItem(date=today, recipe_id=rid, portions=0, created_by=user.telegram_id))
    await session.commit()
    return await day_state(session)


class CookedIn(BaseModel):
    portions: int = Field(gt=0, le=5000)


@router.post("/cook/day/cooked")
async def post_cooked(
    body: CookedIn,
    user: AuthUser = Depends(require_roles("cook")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """«Приготовил N порций»: партия по каждому блюду обеда, продукты списываются по рецепту.
    Нехватка на складе не мешает — склад поправит инвентаризация."""
    state = await day_state(session)
    todo = [d for d in state["dishes"] if d["batch_id"] is None]
    if not todo:
        raise ServiceError(400, "nothing_to_cook", "Сначала выберите, что на обед")
    today = local_today()
    for d in todo:
        await create_batch(session, recipe_id=d["recipe_id"], planned_yield=Decimal(body.portions), actual_yield=None,
                           date=today, created_by=user.telegram_id, strict=False)
    await session.commit()
    return await day_state(session)


class DeliveredItem(BaseModel):
    client: str = Field(min_length=1, max_length=255)
    portions: int = Field(ge=0, le=5000)


class DeliveredIn(BaseModel):
    items: list[DeliveredItem] = Field(min_length=1, max_length=30)


@router.post("/cook/day/delivered")
async def post_delivered(
    body: DeliveredIn,
    user: AuthUser = Depends(require_roles("cook")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """«Отвёз»: одна поставка на клиента за весь обед (все блюда), выручка = порции × цена клиента."""
    today = local_today()
    items = [i for i in body.items if i.portions > 0]
    if not items:
        raise ServiceError(400, "quantity_required", "Укажите, сколько порций отвезли")
    batches = list((await session.execute(
        select(Batch).where(Batch.date == today).order_by(Batch.id).with_for_update())).scalars().all())
    if not batches:
        raise ServiceError(400, "nothing_cooked", "Сначала отметьте, сколько приготовили")
    states = await batch_states(session, batches)
    want = sum(i.portions for i in items)
    room = min(Decimal(str(states[b.id]["remaining"])) for b in batches)
    if want > room:
        raise ServiceError(400, "not_enough_portions", "Отвезли больше, чем приготовили",
                           {"remaining": float(room), "requested": want})
    names = {i.client for i in items}
    clients = {c.name: c for c in (await session.execute(select(Client).where(Client.name.in_(names)))).scalars().all()}
    unknown = sorted(names - set(clients))
    if unknown:
        raise ServiceError(400, "client_not_found", "Клиент не найден", {"clients": unknown})
    dish = " + ".join(b.recipe.name for b in batches if b.recipe) or "—"
    for item in items:
        c = clients[item.client]
        price = await client_price(session, c)
        if not price:
            raise ServiceError(400, "no_client_price", "У клиента не задана цена порции — попросите владельца",
                               {"client": c.name})
        n = Decimal(item.portions)
        costs = {b.id: (cost_per_portion(b) * n).quantize(CENT) for b in batches}
        d = ClientDelivery(client_name=c.name, dish_name=dish[:255], portions=item.portions,
                           amount=(price * n).quantize(CENT), cost=sum(costs.values(), Decimal("0")),
                           date=today, batch_id=batches[0].id, created_by=user.telegram_id)
        session.add(d)
        await session.flush()
        for b in batches:
            session.add(BatchMovement(batch_id=b.id, kind="client", client_name=c.name, portions=n, cost=costs[b.id],
                                      delivery_id=d.id, date=today, created_by=user.telegram_id))
    await session.commit()
    return await day_state(session)


class LeftoverIn(BaseModel):
    portions: int = Field(ge=0, le=5000)


@router.post("/cook/day/leftover")
async def post_leftover(
    body: LeftoverIn,
    user: AuthUser = Depends(require_roles("cook")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """«Осталось N порций» — закрываем день. Остаток идёт в списание с причиной «осталось после обеда»,
    а если повар указал меньше, чем по расчёту, разницу съели на кухне."""
    today = local_today()
    batches = list((await session.execute(
        select(Batch).where(Batch.date == today).order_by(Batch.id).with_for_update())).scalars().all())
    if not batches:
        raise ServiceError(400, "nothing_cooked", "Сначала отметьте, сколько приготовили")
    states = await batch_states(session, batches)
    for b in batches:
        remaining = Decimal(str(states[b.id]["remaining"]))
        left = min(Decimal(body.portions), remaining)
        eaten = remaining - left
        cpp = cost_per_portion(b)
        for kind, n in (("waste", left), ("staff", eaten)):
            if n > 0:
                session.add(BatchMovement(batch_id=b.id, kind=kind, portions=n,
                                          reason="leftover" if kind == "waste" else None,
                                          cost=(cpp * n).quantize(CENT), date=today, created_by=user.telegram_id))
    await session.commit()
    return await day_state(session)


@router.post("/cook/day/undo")
async def post_undo(
    user: AuthUser = Depends(require_roles("cook")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Отменить последний шаг: «осталось» → «отвёз» → «приготовил» (продукты вернутся на склад)."""
    today = local_today()
    batches = await _today_batches(session, today)
    if not batches:
        raise ServiceError(400, "nothing_to_undo", "Отменять нечего")
    ids = [b.id for b in batches]
    moves = await _movements(session, ids)
    rest = [m for m in moves if m.kind in ("staff", "waste")]
    given = [m for m in moves if m.kind == "client"]
    if rest:
        await session.execute(delete(BatchMovement).where(BatchMovement.id.in_([m.id for m in rest])))
    elif given:
        delivery_ids = {m.delivery_id for m in given if m.delivery_id}
        await session.execute(delete(BatchMovement).where(BatchMovement.id.in_([m.id for m in given])))
        if delivery_ids:
            await session.execute(delete(ClientDelivery).where(ClientDelivery.id.in_(delivery_ids)))
    else:
        lines = (await session.execute(select(BatchItem).where(BatchItem.batch_id.in_(ids)))).scalars().all()
        back: dict[int, Decimal] = defaultdict(Decimal)
        for ln in lines:
            if ln.product_id:
                back[ln.product_id] += Decimal(ln.quantity)
        if back:
            for p in (await session.execute(
                    select(Stock).where(Stock.id.in_(sorted(back))).order_by(Stock.id).with_for_update())).scalars().all():
                p.quantity = Decimal(p.quantity) + back[p.id]
        await session.execute(delete(BatchItem).where(BatchItem.batch_id.in_(ids)))
        await session.execute(delete(Batch).where(Batch.id.in_(ids)))
    await session.commit()
    return await day_state(session)


# ================================================================ «🛒 Закончилось»

class ShortageIn(BaseModel):
    items: list[str] = Field(min_length=1, max_length=20)
    note: str = Field("", max_length=300)


@router.post("/cook/shortage")
async def post_shortage(
    body: ShortageIn,
    user: AuthUser = Depends(require_roles("cook")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Повар не считает склад — он просто говорит, что закончилось. Сообщение уходит закупщикам и владельцам."""
    from api.notify import send_message
    from bot.translations import t

    items = [" ".join(i.split())[:80] for i in body.items if i.strip()]
    if not items:
        raise ServiceError(400, "quantity_required", "Выберите, что закончилось")
    settings = get_settings()
    users = (await session.execute(select(User))).scalars().all()
    targets = [u for u in users if u.telegram_id != user.telegram_id and (
        u.telegram_id in settings.owner_ids or resolve_role(u.telegram_id, u.role, settings) == "purchaser")]
    who = html.escape(user.first_name or str(user.telegram_id))
    lines = "\n".join("• " + html.escape(i) for i in items)
    sent = 0
    for u in targets:
        lang = u.language or "ru"
        text = t(lang, "shortage_msg", who=who, items=lines)
        if body.note.strip():
            text += "\n\n💬 " + html.escape(body.note.strip())
        if await send_message(u.telegram_id, text):
            sent += 1
    return {"sent": sent}
