"""
Бизнес-логика кухни. Используется и роутерами API, и обработчиками бота,
чтобы правила (валидация, пересчёт единиц, списание) были в одном месте.
"""
from __future__ import annotations

import datetime as dt
import difflib
from collections import defaultdict
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.models import Batch, BatchItem, BatchMovement, Client, ClientDelivery, Payment, Purchase, Recipe, Stock
from api.units import UnitMismatch, convert, normalize_unit

ALLOWED_PAYMENT_METHODS = {"cash", "card", "transfer"}


class ServiceError(Exception):
    """Ошибка бизнес-правила. status_code уходит в HTTP-ответ."""

    def __init__(self, status_code: int, code: str, message: str, extra: dict | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.extra = extra or {}

    def as_detail(self) -> dict:
        return {"code": self.code, "message": self.message, **self.extra}


def money(value) -> float:
    """Decimal → float с округлением до 2 знаков (для JSON)."""
    return float(Decimal(value or 0).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def qty(value) -> float:
    return float(Decimal(value or 0).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP))


# ---------------------------------------------------------------- Склад

async def match_product(session: AsyncSession, name: str, cutoff: float = 0.6) -> Stock | None:
    """
    Ищет товар на складе по названию: сначала точное совпадение без учёта регистра,
    затем нечёткое (difflib) — для названий из распознанных чеков.
    """
    clean = (name or "").strip()
    if not clean:
        return None
    exact = (
        await session.execute(select(Stock).where(func.lower(Stock.name) == clean.lower()))
    ).scalar_one_or_none()
    if exact:
        return exact
    products = (await session.execute(select(Stock))).scalars().all()
    by_name = {p.name.lower(): p for p in products}
    best = difflib.get_close_matches(clean.lower(), list(by_name), n=1, cutoff=cutoff)
    return by_name[best[0]] if best else None


async def add_purchase(
    session: AsyncSession,
    *,
    product_id: int | None,
    product_name: str | None,
    quantity: Decimal,
    unit: str | None,
    total_price: Decimal,
    payment_method: str,
    date: dt.date,
    created_by: int | None,
) -> tuple[Purchase, Stock]:
    """
    Проводит закупку: проверяет товар, пересчитывает единицы, увеличивает остаток
    и обновляет цену за единицу по средневзвешенной стоимости.
    Если товар не найден на складе — ServiceError 400 (баг из обзора: раньше
    закупка "успешно" сохранялась для несуществующего товара).
    """
    if quantity is None or quantity <= 0:
        raise ServiceError(400, "bad_quantity", "Количество должно быть больше нуля")
    if total_price is None or total_price < 0:
        raise ServiceError(400, "bad_price", "Сумма не может быть отрицательной")
    if payment_method not in ALLOWED_PAYMENT_METHODS:
        raise ServiceError(400, "bad_payment_method", "Неизвестный способ оплаты")

    # Блокируем строку товара, чтобы две одновременные закупки не затёрли остаток
    stmt = select(Stock).with_for_update()
    if product_id:
        stmt = stmt.where(Stock.id == product_id)
    elif product_name:
        stmt = stmt.where(func.lower(Stock.name) == product_name.strip().lower())
    else:
        raise ServiceError(400, "product_not_found", "Товар не указан")
    product = (await session.execute(stmt)).scalar_one_or_none()
    if product is None:
        raise ServiceError(400, "product_not_found", "Товар не найден на складе")

    purchase_unit = normalize_unit(unit or product.unit)
    try:
        qty_in_stock_unit = convert(Decimal(quantity), purchase_unit, product.unit)
    except UnitMismatch:
        raise ServiceError(
            400, "unit_mismatch", f"Единица {unit} несовместима со складской ({product.unit})"
        )

    # Средневзвешенная цена: (старый остаток * старая цена + сумма закупки) / новый остаток
    old_qty = max(Decimal(product.quantity or 0), Decimal("0"))
    old_value = old_qty * Decimal(product.price_per_unit or 0)
    new_qty = Decimal(product.quantity or 0) + qty_in_stock_unit
    if old_qty + qty_in_stock_unit > 0:
        product.price_per_unit = ((old_value + Decimal(total_price)) / (old_qty + qty_in_stock_unit)).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
    product.quantity = new_qty

    purchase = Purchase(
        product_id=product.id,
        product_name=product.name,
        quantity=Decimal(quantity),
        unit=purchase_unit,
        total_price=Decimal(total_price),
        payment_method=payment_method,
        date=date,
        created_by=created_by,
    )
    session.add(purchase)
    await session.flush()
    return purchase, product


# ---------------------------------------------------------------- Партии

async def create_batch(
    session: AsyncSession,
    *,
    recipe_id: int,
    planned_yield: Decimal,
    actual_yield: Decimal | None,
    date: dt.date,
    created_by: int | None,
    items: list[dict] | None = None,
    strict: bool = True,
) -> tuple[Batch, list[dict]]:
    """
    Создаёт партию и списывает продукты со склада.
    items — фактический расход от повара: [{product_id, quantity}] в единицах склада.
    Без items — по техкарте, пропорционально плановому выходу.
    Блокируется (400), если чего-то не хватает или у продукта ещё нет цены — иначе
    себестоимость партии была бы неверной (правило из архитектуры).
    Фактический расход каждого продукта сохраняется в batch_items.
    strict=False — «простой режим» повара: склад повар не ведёт, поэтому нехватка и отсутствие цены
    не блокируют готовку. Остаток может уйти в минус (его поправит инвентаризация закупщика),
    продукт без цены считается по 0.
    """
    if planned_yield is None or planned_yield <= 0:
        raise ServiceError(400, "bad_yield", "Плановый выход должен быть больше нуля")
    if actual_yield is not None and actual_yield < 0:
        raise ServiceError(400, "bad_yield", "Фактический выход не может быть отрицательным")

    recipe = await session.get(Recipe, recipe_id)
    if recipe is None:
        raise ServiceError(404, "recipe_not_found", "Рецепт не найден")
    if not recipe.ingredients and strict:
        raise ServiceError(400, "recipe_empty", "В рецепте нет ингредиентов")

    factor = Decimal(planned_yield) / Decimal(recipe.base_yield or 1)

    # Блокируем все нужные строки склада одним запросом (в порядке id — без взаимоблокировок)
    product_ids = sorted({ing.product_id for ing in recipe.ingredients})
    locked = (
        await session.execute(select(Stock).where(Stock.id.in_(product_ids)).order_by(Stock.id).with_for_update())
    ).scalars().all()
    stock_by_id = {p.id: p for p in locked}

    # По техкарте (план) — для сравнения с фактом
    planned: dict[int, Decimal] = defaultdict(Decimal)
    for ing in recipe.ingredients:
        product = stock_by_id[ing.product_id]
        try:
            planned[product.id] += convert(Decimal(ing.quantity) * factor, ing.unit, product.unit)
        except UnitMismatch:
            raise ServiceError(
                400, "unit_mismatch", f"Ингредиент «{product.name}»: {ing.unit} ≠ {product.unit}"
            )

    if items:
        # Фактический расход от повара: можно поправить количество и добавить продукт не из техкарты
        extra_ids = sorted({int(i["product_id"]) for i in items} - set(stock_by_id))
        if extra_ids:
            extra = (
                await session.execute(select(Stock).where(Stock.id.in_(extra_ids)).order_by(Stock.id).with_for_update())
            ).scalars().all()
            stock_by_id.update({p.id: p for p in extra})
        need: dict[int, Decimal] = defaultdict(Decimal)
        for i in items:
            pid = int(i["product_id"])
            if pid not in stock_by_id:
                raise ServiceError(400, "product_not_found", "Товар не найден в справочнике")
            amount = Decimal(str(i["quantity"]))
            if amount < 0:
                raise ServiceError(400, "bad_quantity", "Расход не может быть отрицательным")
            need[pid] += amount
        need = {pid: a for pid, a in need.items() if a > 0}
        if not need:
            raise ServiceError(400, "recipe_empty", "Не указан расход продуктов")
    else:
        need = dict(planned)

    no_price = [stock_by_id[pid].name for pid in need if not Decimal(stock_by_id[pid].price_per_unit or 0)]
    if no_price and strict:
        raise ServiceError(400, "no_price", "У продуктов нет цены — внесите закупку или цену в инвентаризации",
                           {"products": no_price})

    shortages = [
        {
            "product": stock_by_id[pid].name,
            "need": qty(amount),
            "available": qty(stock_by_id[pid].quantity),
            "unit": stock_by_id[pid].unit,
        }
        for pid, amount in need.items()
        if Decimal(stock_by_id[pid].quantity or 0) < amount
    ]
    if shortages and strict:
        raise ServiceError(400, "insufficient_stock", "Недостаточно продуктов на складе", {"shortages": shortages})

    consumed: list[dict] = []
    total_cost = Decimal("0")
    lines: list[BatchItem] = []
    for pid, amount in need.items():
        product = stock_by_id[pid]
        product.quantity = Decimal(product.quantity) - amount
        line_cost = amount * Decimal(product.price_per_unit or 0)
        total_cost += line_cost
        consumed.append(
            {"product": product.name, "quantity": qty(amount), "unit": product.unit, "cost": money(line_cost),
             "planned": qty(planned[pid]) if pid in planned else None}
        )
        lines.append(BatchItem(product_id=pid, product_name=product.name, quantity=amount, unit=product.unit,
                               planned_quantity=planned.get(pid), cost=line_cost.quantize(Decimal("0.01"))))

    batch = Batch(
        recipe_id=recipe.id,
        recipe_version=recipe.version or 1,
        # Годно до = сейчас + срок годности техкарты (по умолчанию 24 ч)
        expires_at=dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=recipe.shelf_life_hours or 24),
        planned_yield=Decimal(planned_yield),
        actual_yield=Decimal(actual_yield) if actual_yield is not None else None,
        cost=total_cost.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
        date=date,
        created_by=created_by,
    )
    session.add(batch)
    await session.flush()
    for line in lines:
        line.batch_id = batch.id
        session.add(line)
    await session.flush()
    return batch, consumed


# ---------------------------------------------------------------- Статистика

async def compute_stats(
    session: AsyncSession, date_from: dt.date | None = None, date_to: dt.date | None = None
) -> dict:
    """Выручка, себестоимость, прибыль, маржа + разбивка по месяцам и клиентам."""
    stmt = select(
        ClientDelivery.date, ClientDelivery.client_name, ClientDelivery.portions,
        ClientDelivery.amount, ClientDelivery.cost,
    )
    if date_from:
        stmt = stmt.where(ClientDelivery.date >= date_from)
    if date_to:
        stmt = stmt.where(ClientDelivery.date <= date_to)
    rows = (await session.execute(stmt)).all()

    revenue = cost = Decimal("0")
    portions = 0
    months: dict[str, dict[str, Decimal]] = defaultdict(lambda: {"revenue": Decimal("0"), "cost": Decimal("0")})
    clients: dict[str, dict] = defaultdict(lambda: {"revenue": Decimal("0"), "cost": Decimal("0"), "portions": 0})
    # Группировка в Python — одинаково работает в PostgreSQL и SQLite (тесты)
    for d, client_name, p, amount, c in rows:
        amount, c = Decimal(amount or 0), Decimal(c or 0)
        revenue += amount
        cost += c
        portions += int(p or 0)
        key = d.strftime("%Y-%m")
        months[key]["revenue"] += amount
        months[key]["cost"] += c
        clients[client_name]["revenue"] += amount
        clients[client_name]["cost"] += c
        clients[client_name]["portions"] += int(p or 0)

    # Оплаты за всё время (долг считается накопительно)
    paid_rows = (
        await session.execute(select(Payment.client_name, func.coalesce(func.sum(Payment.amount), 0)).group_by(Payment.client_name))
    ).all()
    paid_by_client = {name: Decimal(total or 0) for name, total in paid_rows}
    charged_rows = (
        await session.execute(
            select(ClientDelivery.client_name, func.coalesce(func.sum(ClientDelivery.amount), 0)).group_by(ClientDelivery.client_name)
        )
    ).all()
    charged_by_client = {name: Decimal(total or 0) for name, total in charged_rows}

    profit = revenue - cost
    margin = (profit / revenue * 100) if revenue else Decimal("0")

    stock_value = (
        await session.execute(select(func.coalesce(func.sum(Stock.quantity * Stock.price_per_unit), 0)))
    ).scalar_one()

    purchases_stmt = select(func.coalesce(func.sum(Purchase.total_price), 0))
    if date_from:
        purchases_stmt = purchases_stmt.where(Purchase.date >= date_from)
    if date_to:
        purchases_stmt = purchases_stmt.where(Purchase.date <= date_to)
    purchases_total = (await session.execute(purchases_stmt)).scalar_one()
    # Количества — для окошек «откуда эти цифры» в приложении
    purchases_count_stmt = select(func.count()).select_from(Purchase)
    if date_from:
        purchases_count_stmt = purchases_count_stmt.where(Purchase.date >= date_from)
    if date_to:
        purchases_count_stmt = purchases_count_stmt.where(Purchase.date <= date_to)
    purchases_count = (await session.execute(purchases_count_stmt)).scalar_one()
    stock_items = (await session.execute(select(func.count()).select_from(Stock).where(Stock.quantity > 0))).scalar_one()
    # Списания и питание персонала из партий (этап 2) — отдельными строками, валовую прибыль не меняют
    mv_stmt = select(BatchMovement.kind, func.coalesce(func.sum(BatchMovement.cost), 0), func.coalesce(func.sum(BatchMovement.portions), 0)).where(BatchMovement.kind.in_(("waste", "staff")))
    if date_from:
        mv_stmt = mv_stmt.where(BatchMovement.date >= date_from)
    if date_to:
        mv_stmt = mv_stmt.where(BatchMovement.date <= date_to)
    mv = {k: (Decimal(c or 0), Decimal(p or 0)) for k, c, p in (await session.execute(mv_stmt.group_by(BatchMovement.kind))).all()}
    waste_cost, waste_portions = mv.get("waste", (Decimal("0"), Decimal("0")))
    staff_cost, staff_portions = mv.get("staff", (Decimal("0"), Decimal("0")))

    all_client_names = set(clients) | set(paid_by_client) | set(charged_by_client)
    known = (await session.execute(select(Client.name))).scalars().all()
    all_client_names |= set(known)

    return {
        "period": {"from": date_from.isoformat() if date_from else None, "to": date_to.isoformat() if date_to else None},
        "revenue": money(revenue),
        "cost": money(cost),
        "profit": money(profit),
        "margin": float(Decimal(margin).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)),
        "portions": portions,
        "purchases_total": money(purchases_total),
        "stock_value": money(stock_value),
        "deliveries_count": len(rows),
        "purchases_count": purchases_count,
        "stock_items": stock_items,
        "writeoffs": money(waste_cost), "writeoff_portions": qty(waste_portions),
        "staff_meals": money(staff_cost), "staff_portions": qty(staff_portions),
        "profit_after_writeoffs": money(profit - waste_cost - staff_cost),
        "by_month": [
            {
                "month": m,
                "revenue": money(v["revenue"]),
                "cost": money(v["cost"]),
                "profit": money(v["revenue"] - v["cost"]),
            }
            for m, v in sorted(months.items())
        ],
        "by_client": sorted(
            (
                {
                    "client": name,
                    "revenue": money(clients[name]["revenue"]) if name in clients else 0.0,
                    "portions": clients[name]["portions"] if name in clients else 0,
                    "cost": money(clients[name]["cost"]) if name in clients else 0.0,
                    "charged_total": money(charged_by_client.get(name, 0)),
                    "paid_total": money(paid_by_client.get(name, 0)),
                    "debt": money(charged_by_client.get(name, Decimal(0)) - paid_by_client.get(name, Decimal(0))),
                }
                for name in all_client_names
            ),
            key=lambda x: -x["revenue"],
        ),
    }


# ---------------------------------------------------------------- Клиенты

async def client_deliveries(
    session: AsyncSession, client_name: str, date_from: dt.date | None = None, date_to: dt.date | None = None
) -> list[ClientDelivery]:
    stmt = select(ClientDelivery).where(ClientDelivery.client_name == client_name)
    if date_from:
        stmt = stmt.where(ClientDelivery.date >= date_from)
    if date_to:
        stmt = stmt.where(ClientDelivery.date <= date_to)
    stmt = stmt.order_by(ClientDelivery.date.desc(), ClientDelivery.id.desc())
    return list((await session.execute(stmt)).scalars().all())


async def client_invoice(session: AsyncSession, client_name: str) -> dict:
    """Счёт клиента: начислено, оплачено, долг + последние оплаты."""
    charged = (
        await session.execute(
            select(func.coalesce(func.sum(ClientDelivery.amount), 0)).where(ClientDelivery.client_name == client_name)
        )
    ).scalar_one()
    paid = (
        await session.execute(select(func.coalesce(func.sum(Payment.amount), 0)).where(Payment.client_name == client_name))
    ).scalar_one()
    payments = (
        await session.execute(
            select(Payment).where(Payment.client_name == client_name).order_by(Payment.date.desc()).limit(50)
        )
    ).scalars().all()
    return {
        "client": client_name,
        "charged": money(charged),
        "paid": money(paid),
        "debt": money(Decimal(charged) - Decimal(paid)),
        "payments": [
            {"date": p.date.isoformat(), "amount": money(p.amount), "payment_type": p.payment_type} for p in payments
        ],
    }


# ---------------------------------------------------------------- Акт сверки (как в 1С, простыми словами)

async def client_statement(
    session: AsyncSession, client_name: str, date_from: dt.date | None = None, date_to: dt.date | None = None
) -> dict:
    """
    Движения по клиенту за период — основа акта сверки:
      долг на начало = все поставки до начала периода − все оплаты до начала периода;
      строки        = поставки и оплаты периода по датам, после каждой — текущий долг;
      долг на конец = долг на начало + поставили − оплатили.
    Положительный долг — клиент должен нам, отрицательный — переплата (аванс).
    Без дат — весь период работы; итог тогда совпадает со «Счётом» клиента.
    """
    opening = Decimal("0")
    if date_from:
        before_d = (
            await session.execute(
                select(func.coalesce(func.sum(ClientDelivery.amount), 0))
                .where(ClientDelivery.client_name == client_name, ClientDelivery.date < date_from)
            )
        ).scalar_one()
        before_p = (
            await session.execute(
                select(func.coalesce(func.sum(Payment.amount), 0))
                .where(Payment.client_name == client_name, Payment.date < date_from)
            )
        ).scalar_one()
        opening = Decimal(before_d) - Decimal(before_p)

    d_stmt = select(ClientDelivery).where(ClientDelivery.client_name == client_name)
    p_stmt = select(Payment).where(Payment.client_name == client_name)
    if date_from:
        d_stmt, p_stmt = d_stmt.where(ClientDelivery.date >= date_from), p_stmt.where(Payment.date >= date_from)
    if date_to:
        d_stmt, p_stmt = d_stmt.where(ClientDelivery.date <= date_to), p_stmt.where(Payment.date <= date_to)
    deliveries = (await session.execute(d_stmt)).scalars().all()
    payments = (await session.execute(p_stmt)).scalars().all()

    # В один день сначала поставка, потом оплата — так «долг после» читается естественно
    ops = [(d.date, 0, d.id or 0, "delivery", d) for d in deliveries] + [(p.date, 1, p.id or 0, "payment", p) for p in payments]
    ops.sort(key=lambda x: (x[0], x[1], x[2]))

    balance = opening
    rows: list[dict] = []
    delivered = paid = Decimal("0")
    portions = 0
    for date, _, _, kind, obj in ops:
        amount = Decimal(obj.amount or 0)
        if kind == "delivery":
            balance += amount
            delivered += amount
            portions += int(obj.portions or 0)
            rows.append({"date": date, "kind": kind, "dish": obj.dish_name, "portions": int(obj.portions or 0),
                         "delivered": amount, "paid": None, "balance": balance})
        else:
            balance -= amount
            paid += amount
            rows.append({"date": date, "kind": kind, "payment_type": obj.payment_type,
                         "delivered": None, "paid": amount, "balance": balance})

    period_from = date_from or (rows[0]["date"] if rows else dt.date.today())
    period_to = date_to or (rows[-1]["date"] if rows else dt.date.today())
    return {
        "client": client_name,
        "date_from": period_from,
        "date_to": period_to,
        "opening": opening,
        "rows": rows,
        "delivered": delivered,
        "paid": paid,
        "portions": portions,
        "closing": opening + delivered - paid,
    }
