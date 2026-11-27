"""
Кухонный цикл (этапы 2–3): раздача, остатки, списания, меню на неделю, список закупок, PDF.

Партия → куда ушли порции:
    GET  /api/kitchen/today               — меню на сегодня + партии дня с остатками
    GET  /api/kitchen/open                — открытые остатки (не всё раздали), срок годности
    POST /api/batch/{id}/distribute       — раздать клиентам: создаются поставки (выручка по цене клиента,
                                            себестоимость — из партии)
    POST /api/batch/{id}/leftover         — «Персоналу» или «Выбросить» (с причиной)
Каждая порция учтена: выход = раздали + персоналу + выбросили + остаток.

Меню и закупки:
    GET  /api/menu?week=YYYY-MM-DD        — меню пн–пт недели (дата любого дня недели)
    PUT  /api/menu/day                    — заменить меню дня
    POST /api/menu/copy                   — «Повторить прошлую неделю»
    GET  /api/menu/shopping?week=         — что купить на неделю (потребность по меню − остаток)
    POST /api/menu/pdf-send?week=         — PDF «Меню недели» в чат (без цен — можно клиентам)
    POST /api/recipes/pdf-send            — PDF всех техкарт с фото в чат (повар, владелец)

Цена порции клиента (владелец): PUT /api/clients/price
"""
from __future__ import annotations

import datetime as dt
from collections import defaultdict
from decimal import ROUND_HALF_UP, Decimal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth import AuthUser, get_current_user, require_roles
from api.database import get_session
from api.models import Batch, BatchMovement, Client, ClientDelivery, MenuItem, Recipe, RecipePhoto, Stock
from api.services import ServiceError, money, qty
from api.units import UnitMismatch, convert

router = APIRouter(prefix="/api", tags=["kitchen"])

WASTE_REASONS = ("spoiled", "defect", "expired", "other")
CENT = Decimal("0.01")


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _aware(d: dt.datetime | None) -> dt.datetime | None:
    """SQLite отдаёт время без пояса — считаем его UTC (в PostgreSQL пояс есть)."""
    if d is None:
        return None
    return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)


def batch_yield(b: Batch) -> Decimal:
    return Decimal(b.actual_yield if b.actual_yield is not None else b.planned_yield or 0)


def cost_per_portion(b: Batch) -> Decimal:
    y = batch_yield(b)
    return (Decimal(b.cost or 0) / y) if y else Decimal("0")


async def batch_states(session: AsyncSession, batches: list[Batch]) -> dict[int, dict]:
    """Сколько раздали / персоналу / выбросили, остаток и статус срока по каждой партии."""
    ids = [b.id for b in batches]
    sums: dict[int, dict[str, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
    if ids:
        rows = (
            await session.execute(
                select(BatchMovement.batch_id, BatchMovement.kind, func.coalesce(func.sum(BatchMovement.portions), 0))
                .where(BatchMovement.batch_id.in_(ids)).group_by(BatchMovement.batch_id, BatchMovement.kind)
            )
        ).all()
        for bid, kind, total in rows:
            sums[bid][kind] = Decimal(total or 0)
    now = _now()
    out = {}
    for b in batches:
        y = batch_yield(b)
        s = sums[b.id]
        remaining = y - s["client"] - s["staff"] - s["waste"]
        expires = _aware(b.expires_at)
        hours_left = (expires - now).total_seconds() / 3600 if expires else None
        if remaining <= 0:
            status = "closed"
        elif hours_left is not None and hours_left <= 0:
            status = "expired"
        elif hours_left is not None and hours_left <= 3:
            status = "soon"
        else:
            status = "fresh"
        out[b.id] = {
            "id": b.id,
            "recipe_id": b.recipe_id,
            "recipe": b.recipe.name if b.recipe else None,
            "date": b.date.isoformat(),
            "yield": qty(y),
            "given": qty(s["client"]),
            "staff": qty(s["staff"]),
            "waste": qty(s["waste"]),
            "remaining": qty(max(remaining, Decimal("0"))),
            "cost_per_portion": money(cost_per_portion(b)),
            "expires_at": expires.isoformat() if expires else None,
            "hours_left": round(hours_left, 1) if hours_left is not None else None,
            "status": status,
        }
    return out


async def client_price(session: AsyncSession, client: Client) -> Decimal | None:
    """Цена порции: из карточки клиента, иначе средняя по его последним поставкам."""
    if client.price_per_portion:
        return Decimal(client.price_per_portion)
    row = (
        await session.execute(
            select(ClientDelivery.amount, ClientDelivery.portions)
            .where(ClientDelivery.client_name == client.name, ClientDelivery.portions > 0)
            .order_by(ClientDelivery.date.desc(), ClientDelivery.id.desc()).limit(10)
        )
    ).all()
    total_a = sum((Decimal(a or 0) for a, _ in row), Decimal("0"))
    total_p = sum(int(p or 0) for _, p in row)
    return (total_a / total_p).quantize(CENT, rounding=ROUND_HALF_UP) if total_p else None


async def _locked_batch(session: AsyncSession, batch_id: int) -> Batch:
    b = (await session.execute(select(Batch).where(Batch.id == batch_id).with_for_update())).scalar_one_or_none()
    if b is None:
        raise HTTPException(404, "Batch not found")
    return b


# ================================================================ Раздача и остатки

class DistributeItem(BaseModel):
    client: str = Field(min_length=1, max_length=255)
    portions: Decimal = Field(gt=0, le=100000)


class DistributeIn(BaseModel):
    items: list[DistributeItem] = Field(min_length=1, max_length=20)
    date: dt.date = Field(default_factory=dt.date.today)


@router.post("/batch/{batch_id}/distribute", status_code=201)
async def distribute(
    batch_id: int,
    body: DistributeIn,
    user: AuthUser = Depends(require_roles("cook")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """
    Раздать порции клиентам. Для каждого клиента создаётся поставка:
    выручка = порции × цена порции клиента, себестоимость = порции × себестоимость порции партии.
    Нельзя раздать больше, чем осталось в партии.
    """
    b = await _locked_batch(session, batch_id)
    state = (await batch_states(session, [b]))[b.id]
    want = sum((i.portions for i in body.items), Decimal("0"))
    if want > Decimal(str(state["remaining"])):
        raise ServiceError(400, "not_enough_portions", "В партии осталось меньше порций",
                           {"remaining": state["remaining"], "requested": float(want)})
    names = {i.client for i in body.items}
    clients = {c.name: c for c in (await session.execute(select(Client).where(Client.name.in_(names)))).scalars().all()}
    unknown = sorted(names - set(clients))
    if unknown:
        raise ServiceError(400, "client_not_found", "Клиент не найден", {"clients": unknown})
    cpp = cost_per_portion(b)
    created = []
    for item in body.items:
        c = clients[item.client]
        price = await client_price(session, c)
        if not price:
            raise ServiceError(400, "no_client_price", "У клиента не задана цена порции — попросите владельца", {"client": c.name})
        d = ClientDelivery(
            client_name=c.name, dish_name=b.recipe.name if b.recipe else "—", portions=int(item.portions),
            amount=(price * item.portions).quantize(CENT), cost=(cpp * item.portions).quantize(CENT),
            date=body.date, batch_id=b.id, created_by=user.telegram_id,
        )
        session.add(d)
        await session.flush()
        session.add(BatchMovement(batch_id=b.id, kind="client", client_name=c.name, portions=item.portions,
                                  cost=(cpp * item.portions).quantize(CENT), delivery_id=d.id,
                                  date=body.date, created_by=user.telegram_id))
        created.append({"client": c.name, "portions": int(item.portions), "amount": money(d.amount)})
    await session.commit()
    return {"deliveries": created, "batch": (await batch_states(session, [b]))[b.id]}


class LeftoverIn(BaseModel):
    kind: str = Field(pattern="^(staff|waste)$")
    portions: Decimal = Field(gt=0, le=100000)
    reason: str | None = Field(None, pattern="^(" + "|".join(WASTE_REASONS) + ")$")
    date: dt.date = Field(default_factory=dt.date.today)


@router.post("/batch/{batch_id}/leftover", status_code=201)
async def leftover(
    batch_id: int,
    body: LeftoverIn,
    user: AuthUser = Depends(require_roles("cook")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """«Персоналу» (питание персонала) или «Выбросить» (причина обязательна). Себестоимость идёт в расходы."""
    b = await _locked_batch(session, batch_id)
    state = (await batch_states(session, [b]))[b.id]
    if body.portions > Decimal(str(state["remaining"])):
        raise ServiceError(400, "not_enough_portions", "В партии осталось меньше порций",
                           {"remaining": state["remaining"], "requested": float(body.portions)})
    if body.kind == "waste" and not body.reason:
        raise ServiceError(400, "reason_required", "Укажите причину списания")
    session.add(BatchMovement(batch_id=b.id, kind=body.kind, portions=body.portions,
                              reason=body.reason if body.kind == "waste" else None,
                              cost=(cost_per_portion(b) * body.portions).quantize(CENT),
                              date=body.date, created_by=user.telegram_id))
    await session.commit()
    return {"batch": (await batch_states(session, [b]))[b.id]}


@router.get("/kitchen/open")
async def open_batches(
    days: int = Query(7, ge=1, le=60),
    user: AuthUser = Depends(require_roles("cook")),
    session: AsyncSession = Depends(get_session),
) -> list[dict]:
    """Партии с остатком за последние дни: сначала те, у которых срок истекает раньше."""
    since = dt.date.today() - dt.timedelta(days=days)
    batches = (await session.execute(select(Batch).where(Batch.date >= since))).scalars().all()
    states = await batch_states(session, batches)
    rows = [s for s in states.values() if s["status"] != "closed"]
    return sorted(rows, key=lambda s: (s["expires_at"] or "9999", s["id"]))


async def _menu_days(session: AsyncSession, monday: dt.date) -> list[dict]:
    friday = monday + dt.timedelta(days=4)
    items = (
        await session.execute(
            select(MenuItem).where(MenuItem.date >= monday, MenuItem.date <= friday).order_by(MenuItem.date, MenuItem.id)
        )
    ).scalars().all()
    days = []
    for i in range(5):
        d = monday + dt.timedelta(days=i)
        day_items = [
            {"id": m.id, "recipe_id": m.recipe_id, "recipe": m.recipe.name if m.recipe else "—",
             "client": m.client_name, "portions": m.portions}
            for m in items if m.date == d
        ]
        days.append({"date": d.isoformat(), "weekday": i, "items": day_items,
                     "portions": sum(x["portions"] for x in day_items)})
    return days


@router.get("/kitchen/today")
async def kitchen_today(
    user: AuthUser = Depends(require_roles("cook")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Меню на сегодня (по блюдам: всего и по клиентам) и партии дня с остатками — экран «Сегодня»."""
    today = dt.date.today()
    menu = (await session.execute(select(MenuItem).where(MenuItem.date == today))).scalars().all()
    plan: dict[int, dict] = {}
    for m in menu:
        p = plan.setdefault(m.recipe_id, {"recipe_id": m.recipe_id, "recipe": m.recipe.name if m.recipe else "—",
                                          "portions": 0, "clients": {}})
        p["portions"] += m.portions
        if m.client_name:
            p["clients"][m.client_name] = p["clients"].get(m.client_name, 0) + m.portions
    batches = (await session.execute(select(Batch).where(Batch.date == today).order_by(Batch.id))).scalars().all()
    states = await batch_states(session, batches)
    cooked = defaultdict(Decimal)
    for b in batches:
        cooked[b.recipe_id] += batch_yield(b)
    for p in plan.values():
        p["cooked"] = qty(cooked.get(p["recipe_id"], Decimal("0")))
    clients = (await session.execute(select(Client.name).order_by(Client.name))).scalars().all()
    return {"date": today.isoformat(), "plan": list(plan.values()), "batches": list(states.values()), "clients": clients}


# ================================================================ Цена порции клиента (владелец)

class PriceIn(BaseModel):
    client: str = Field(min_length=1, max_length=255)
    price: Decimal = Field(ge=0)


@router.put("/clients/price")
async def set_client_price(
    body: PriceIn,
    user: AuthUser = Depends(require_roles("owner")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    c = (await session.execute(select(Client).where(Client.name == body.client))).scalar_one_or_none()
    if c is None:
        raise ServiceError(400, "client_not_found", "Клиент не найден")
    c.price_per_portion = body.price or None
    await session.commit()
    return {"client": c.name, "price": money(await client_price(session, c) or 0)}


@router.get("/clients/prices")
async def client_prices(
    user: AuthUser = Depends(require_roles("owner")),
    session: AsyncSession = Depends(get_session),
) -> list[dict]:
    clients = (await session.execute(select(Client).order_by(Client.name))).scalars().all()
    return [{"client": c.name, "price": money(await client_price(session, c) or 0),
             "manual": bool(c.price_per_portion)} for c in clients]


# ================================================================ Меню на неделю

def monday_of(value: str | None) -> dt.date:
    try:
        d = dt.date.fromisoformat(value) if value else dt.date.today()
    except ValueError:
        raise HTTPException(400, "week must be YYYY-MM-DD")
    return d - dt.timedelta(days=d.weekday())


@router.get("/menu")
async def get_menu(
    week: str | None = Query(None, description="Любая дата недели, YYYY-MM-DD"),
    user: AuthUser = Depends(require_roles("cook")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    monday = monday_of(week)
    return {"week": monday.isoformat(), "days": await _menu_days(session, monday)}


class MenuItemIn(BaseModel):
    recipe_id: int
    client: str | None = Field(None, max_length=255)
    portions: int = Field(ge=1, le=100000)


class MenuDayIn(BaseModel):
    date: dt.date
    items: list[MenuItemIn] = Field(default_factory=list, max_length=60)


@router.put("/menu/day")
async def put_menu_day(
    body: MenuDayIn,
    user: AuthUser = Depends(require_roles("cook")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Заменяет меню дня целиком (проще и надёжнее, чем править по строке)."""
    ids = {i.recipe_id for i in body.items}
    found = set((await session.execute(select(Recipe.id).where(Recipe.id.in_(ids)))).scalars().all()) if ids else set()
    if ids - found:
        raise ServiceError(400, "recipe_not_found", "Техкарта не найдена")
    await session.execute(delete(MenuItem).where(MenuItem.date == body.date))
    for i in body.items:
        session.add(MenuItem(date=body.date, recipe_id=i.recipe_id, client_name=(i.client or None),
                             portions=i.portions, created_by=user.telegram_id))
    await session.commit()
    monday = body.date - dt.timedelta(days=body.date.weekday())
    return {"week": monday.isoformat(), "days": await _menu_days(session, monday)}


class CopyIn(BaseModel):
    from_week: dt.date
    to_week: dt.date


@router.post("/menu/copy")
async def copy_week(
    body: CopyIn,
    user: AuthUser = Depends(require_roles("cook")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """«Повторить прошлую неделю»: меню пн–пт копируется на другую неделю (её меню заменяется)."""
    src = body.from_week - dt.timedelta(days=body.from_week.weekday())
    dst = body.to_week - dt.timedelta(days=body.to_week.weekday())
    items = (
        await session.execute(select(MenuItem).where(MenuItem.date >= src, MenuItem.date <= src + dt.timedelta(days=4)))
    ).scalars().all()
    if not items:
        raise ServiceError(400, "menu_empty", "В той неделе меню пустое")
    await session.execute(delete(MenuItem).where(MenuItem.date >= dst, MenuItem.date <= dst + dt.timedelta(days=4)))
    shift = dst - src
    for m in items:
        session.add(MenuItem(date=m.date + shift, recipe_id=m.recipe_id, client_name=m.client_name,
                             portions=m.portions, created_by=user.telegram_id))
    await session.commit()
    return {"week": dst.isoformat(), "days": await _menu_days(session, dst)}


@router.get("/menu/shopping")
async def shopping_list(
    week: str | None = Query(None),
    user: AuthUser = Depends(require_roles("cook", "purchaser")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """
    Что купить на неделю: потребность по меню (техкарты, пересчитанные на порции) минус текущий остаток.
    Для текущей недели считаем с сегодняшнего дня — прошедшие дни уже приготовлены.
    """
    monday = monday_of(week)
    start = max(monday, dt.date.today())
    friday = monday + dt.timedelta(days=4)
    items = (
        await session.execute(select(MenuItem).where(MenuItem.date >= start, MenuItem.date <= friday))
    ).scalars().all()
    need: dict[int, Decimal] = defaultdict(Decimal)
    for m in items:
        r = m.recipe
        if not r:
            continue
        factor = Decimal(m.portions) / Decimal(r.base_yield or 1)
        for ing in r.ingredients:
            try:
                need[ing.product_id] += convert(Decimal(ing.quantity) * factor, ing.unit, ing.product.unit)
            except UnitMismatch:
                continue
    products = {p.id: p for p in (await session.execute(select(Stock).where(Stock.id.in_(list(need))))).scalars().all()} if need else {}
    rows = []
    total = Decimal("0")
    for pid, amount in need.items():
        p = products[pid]
        to_buy = max(amount - Decimal(p.quantity or 0), Decimal("0"))
        est = to_buy * Decimal(p.price_per_unit or 0)
        total += est
        rows.append({"product_id": pid, "product": p.name, "category": p.category or "other", "unit": p.unit,
                     "need": qty(amount), "have": qty(p.quantity), "to_buy": qty(to_buy),
                     "price_per_unit": money(p.price_per_unit), "estimate": money(est)})
    rows.sort(key=lambda r: (r["to_buy"] <= 0, r["category"], r["product"]))
    return {"week": monday.isoformat(), "from": start.isoformat(), "items": rows, "estimate_total": money(total),
            "dishes": len(items)}


# ================================================================ PDF рецептов и меню (бот присылает в чат)

async def _photos(session: AsyncSession, ids: list[int]) -> dict[int, bytes]:
    if not ids:
        return {}
    rows = (await session.execute(select(RecipePhoto).where(RecipePhoto.recipe_id.in_(ids)))).scalars().all()
    return {p.recipe_id: p.content for p in rows}


@router.post("/recipes/pdf-send")
async def send_recipes_pdf(
    user: AuthUser = Depends(require_roles("cook")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Книга техкарт с фото, составом и себестоимостью — повару/владельцу в чат."""
    from api.notify import send_document
    from api.pdf_generator import build_recipes_pdf
    from bot.translations import t

    recipes = (await session.execute(
        select(Recipe).where(Recipe.archived.isnot(True)).order_by(Recipe.category, Recipe.name))).scalars().all()
    pdf = build_recipes_pdf(recipes, await _photos(session, [r.id for r in recipes]), user.language)
    await send_document(user.telegram_id, pdf, f"recipes_{dt.date.today():%Y%m%d}.pdf", t(user.language, "recipes_pdf_caption"))
    return {"ok": True}


@router.post("/menu/pdf-send")
async def send_menu_pdf(
    week: str | None = Query(None),
    user: AuthUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """«Меню недели» с фото, без цен и состава — можно повару, владельцу и клиентам."""
    from api.notify import send_document
    from api.pdf_generator import build_menu_pdf
    from bot.translations import t

    if not user.role:
        raise HTTPException(403, "Forbidden: role is not allowed")
    monday = monday_of(week)
    days = await _menu_days(session, monday)
    ids = sorted({i["recipe_id"] for d in days for i in d["items"]})
    if not ids:
        raise ServiceError(400, "menu_empty", "В этой неделе меню пустое")
    recipes = {r.id: r for r in (await session.execute(select(Recipe).where(Recipe.id.in_(ids)))).scalars().all()}
    pdf = build_menu_pdf(monday, days, recipes, await _photos(session, ids), user.language)
    await send_document(user.telegram_id, pdf, f"menu_{monday:%Y%m%d}.pdf", t(user.language, "menu_pdf_caption"))
    return {"ok": True}
