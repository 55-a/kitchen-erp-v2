"""
Импорт исторических данных из Учёт_кухни.xlsx в PostgreSQL.

Запуск:
    python -m migrations.migrate_excel "Учёт_кухни.xlsx"            # импорт
    python -m migrations.migrate_excel "Учёт_кухни.xlsx" --dry-run  # только проверка, без записи
    python -m migrations.migrate_excel "Учёт_кухни.xlsx" --reset    # очистить и импортировать заново

Реальная структура файла:
    Лист "По дням" — широкая таблица: слева дата, дальше блок колонок на каждого клиента
        (Блюдо | Порций | Начислено | Оплата | Себестоимость), справа блок «ВСЕГО».
        Над строкой заголовков — строка с названиями клиентов.
        Строки «Сентябрь 2026», «Итого за …», «ИТОГО ЗА ВЕСЬ ПЕРИОД» пропускаются.
        Строка «Предоплата» без даты — оплата до первого заказа (ставим дату первой поставки).
        → client_deliveries (порции > 0) и payments (оплата > 0)
    Лист "Себестоимость" — три таблицы одна под другой:
        «Блюдо | Закладка на 100 порций | Себестоимость порции …»  → recipes
        «Продукт | Примечание | Ед. | Цена»                        → stock (остаток 0)
        «Блюдо | Продукт | Кол-во | Ед. | Цена | Сумма»            → recipe_ingredients
    Лист "Итог" — контрольные суммы (порции, начислено, оплачено по каждому клиенту).

Таблицы и колонки ищутся по тексту заголовков, а не по номерам строк и буквам,
поэтому скрипт не сломается, если в файл добавят строки или столбцы.
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import re
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path

from openpyxl import load_workbook
from sqlalchemy import delete, func, select

from api.database import init_db, session_scope
from api.models import Batch, Client, ClientDelivery, Payment, Purchase, Recipe, RecipeIngredient, Stock
from api.units import normalize_unit

DEFAULT_CLIENTS = ["FOYDALI FOOD GROUP", "NO NAMELA"]
DEFAULT_BASE_YIELD = Decimal("100")
CENT = Decimal("0.01")

SKIP_PREFIXES = ("итого", "всего", "total", "jami", "средняя")


# ------------------------------------------------------------------ Разбор значений

def clean(value) -> str:
    return re.sub(r"\s+", " ", str(value if value is not None else "")).strip()


def low(value) -> str:
    return clean(value).lower()


def to_decimal(value) -> Decimal | None:
    """'1 250 000,50 сум' → Decimal('1250000.50'); '—' и пусто → None."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float, Decimal)):
        return Decimal(str(value))
    text = re.sub(r"[^\d,.\-]", "", str(value)).replace(",", ".")
    if text.count(".") > 1:
        head, _, tail = text.rpartition(".")
        text = head.replace(".", "") + "." + tail
    try:
        return Decimal(text) if text not in {"", "-", "."} else None
    except InvalidOperation:
        return None


def to_date(value) -> dt.date | None:
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    if isinstance(value, (int, float)) and 30000 < value < 80000:  # серийная дата Excel
        return dt.date(1899, 12, 30) + dt.timedelta(days=int(value))
    if isinstance(value, str):
        for fmt in ("%d.%m.%Y", "%d.%m.%y", "%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%d/%m/%Y"):
            try:
                return dt.datetime.strptime(value.strip(), fmt).date()
            except ValueError:
                continue
    return None


def is_dash(text: str) -> bool:
    return text in {"", "—", "-", "–"}


def is_skip(text: str) -> bool:
    return low(text).startswith(SKIP_PREFIXES)


def money(value: Decimal | None) -> Decimal:
    return (value or Decimal("0")).quantize(CENT)


# ------------------------------------------------------------------ Работа с листами

def load_rows(ws) -> list[list]:
    """Все строки листа как списки одинаковой длины."""
    rows = [list(r) for r in ws.iter_rows(values_only=True)]
    width = max((len(r) for r in rows), default=0)
    return [r + [None] * (width - len(r)) for r in rows]


def get_sheet(wb, *names: str):
    """Лист по имени без учёта регистра и лишних пробелов."""
    for name in names:
        for ws in wb.worksheets:
            if low(ws.title) == name.lower():
                return ws
    raise ValueError(f"Лист {names} не найден. Есть: {[ws.title for ws in wb.worksheets]}")


def find_col(header: list, *variants: str, start: int = 0, stop: int | None = None) -> int | None:
    """Индекс первой колонки, заголовок которой начинается с одного из вариантов."""
    stop = len(header) if stop is None else stop
    for idx in range(start, stop):
        text = low(header[idx])
        if text and any(text.startswith(v) for v in variants):
            return idx
    return None


def find_header_row(rows: list[list], predicate, start: int = 0) -> int | None:
    for idx in range(start, len(rows)):
        if predicate([low(c) for c in rows[idx]]):
            return idx
    return None


# ------------------------------------------------------------------ Лист "По дням"

def read_daily(wb) -> tuple[list[dict], list[dict]]:
    """Возвращает (поставки, оплаты)."""
    rows = load_rows(get_sheet(wb, "По дням"))
    h = find_header_row(rows, lambda c: "дата" in c and any(x.startswith("порц") for x in c))
    if h is None or h == 0:
        raise ValueError("Лист «По дням»: не найдена строка заголовков с «Дата» и «Порций»")
    header, groups = rows[h], rows[h - 1]
    date_col = find_col(header, "дата")

    # Блоки клиентов: непустая ячейка в строке над заголовками начинает блок до следующей
    starts = [i for i, v in enumerate(groups) if clean(v)]
    blocks = []
    for n, col in enumerate(starts):
        name = clean(groups[col])
        if low(name) in {"когда", "всего", "итого", "total"}:
            continue
        stop = starts[n + 1] if n + 1 < len(starts) else len(header)
        block = {
            "client": canonical_client(name),
            "dish": find_col(header, "блюдо", start=col, stop=stop),
            "portions": find_col(header, "порц", start=col, stop=stop),
            "amount": find_col(header, "начислено", "сумма", "выручка", start=col, stop=stop),
            "payment": find_col(header, "оплат", start=col, stop=stop),
            "cost": find_col(header, "себестоимость", start=col, stop=stop),
        }
        if block["portions"] is None:
            continue
        blocks.append(block)
    if not blocks:
        raise ValueError("Лист «По дням»: не найдены блоки клиентов над строкой заголовков")
    print("  Клиенты в «По дням»: " + ", ".join(b["client"] for b in blocks))

    deliveries: list[dict] = []
    payments: list[dict] = []
    for row in rows[h + 1:]:
        first = clean(row[0])
        if is_skip(first):
            continue
        d = to_date(row[date_col])
        for b in blocks:
            def val(key):
                return to_decimal(row[b[key]]) if b[key] is not None else None

            dish = clean(row[b["dish"]]) if b["dish"] is not None else ""
            portions = val("portions")
            payment = val("payment")
            if d is None:
                # Только оплаты без даты («Предоплата»); строки-месяцы пропускаются
                if payment and payment > 0:
                    payments.append({"date": None, "client": b["client"], "amount": payment, "note": dish or first})
                continue
            if portions and portions > 0:
                deliveries.append(
                    {
                        "date": d,
                        "client": b["client"],
                        "dish": dish if not is_dash(dish) else "—",
                        "portions": int(portions),
                        "amount": money(val("amount")),
                        "cost": money(val("cost")),
                    }
                )
            if payment and payment > 0:
                payments.append({"date": d, "client": b["client"], "amount": payment, "note": dish})

    # Предоплата без даты → дата первой поставки
    first_date = min((x["date"] for x in deliveries), default=dt.date.today())
    for p in payments:
        if p["date"] is None:
            p["date"] = first_date
        p["amount"] = money(p["amount"])
    return deliveries, payments


# ------------------------------------------------------------------ Лист "Себестоимость"

def read_costing(wb) -> tuple[dict[str, Decimal], list[dict], list[dict]]:
    """Возвращает (рецепты {блюдо: выход}, товары, состав закладок)."""
    rows = load_rows(get_sheet(wb, "Себестоимость"))

    # Выход закладки: строка «Порций в каждой закладке | 100» или «на 100 порций» в заголовке
    base_yield = DEFAULT_BASE_YIELD
    for row in rows[:10]:
        if "порций в" in low(row[0]):
            y = next((to_decimal(c) for c in row[1:] if to_decimal(c)), None)
            if y and y > 0:
                base_yield = y
                break

    # 1) Итог по блюдам
    recipes: dict[str, Decimal] = {}
    h = find_header_row(rows, lambda c: c[0] == "блюдо" and len(c) > 1 and not c[1].startswith("продукт"))
    if h is not None:
        m = re.search(r"на\s+(\d+)\s+порц", low(rows[h][1]))
        if m:
            base_yield = Decimal(m.group(1))
        for row in rows[h + 1:]:
            name = clean(row[0])
            if not name:
                break
            if not is_skip(name):
                recipes[name] = base_yield

    # 2) Цены продуктов
    products: dict[str, dict] = {}
    h = find_header_row(rows, lambda c: c[0] == "продукт")
    if h is None:
        raise ValueError("Лист «Себестоимость»: не найдена таблица «Продукт | … | Ед. | Цена»")
    header = rows[h]
    unit_col = find_col(header, "ед")
    price_col = find_col(header, "цена")
    for row in rows[h + 1:]:
        name = clean(row[0])
        if not name:
            break
        price = to_decimal(row[price_col]) if price_col is not None else None
        if price is None or is_skip(name):
            continue
        unit = normalize_unit(clean(row[unit_col])) if unit_col is not None else "kg"
        products[name.lower()] = {"name": name, "unit": unit, "price": money(price)}

    # 3) Состав закладок
    ingredients: list[dict] = []
    h = find_header_row(rows, lambda c: c[0] == "блюдо" and len(c) > 1 and c[1].startswith("продукт"))
    if h is None:
        raise ValueError("Лист «Себестоимость»: не найдена таблица «Блюдо | Продукт | Кол-во | Ед.»")
    header = rows[h]
    qty_col = find_col(header, "кол", "количество")
    unit_col = find_col(header, "ед")
    for row in rows[h + 1:]:
        dish, product = clean(row[0]), clean(row[1])
        if not dish and not product:
            break
        qty = to_decimal(row[qty_col]) if qty_col is not None else None
        if not dish or not product or not qty or qty <= 0 or is_skip(dish):
            continue
        unit = normalize_unit(clean(row[unit_col])) if unit_col is not None else "kg"
        ingredients.append({"dish": dish, "product": product, "quantity": qty, "unit": unit})
        recipes.setdefault(dish, base_yield)

    return recipes, list(products.values()), ingredients


# ------------------------------------------------------------------ Лист "Итог" (контроль)

def read_expected(wb) -> dict[str, dict[str, Decimal]] | None:
    """{'portions'|'amount'|'paid': {клиент или 'ВСЕГО': число}} из листа «Итог»."""
    try:
        rows = load_rows(get_sheet(wb, "Итог"))
    except ValueError:
        return None
    h = find_header_row(rows, lambda c: c[0] == "показатель")
    if h is None:
        return None
    names = [canonical_client(clean(c)) if clean(c) else "" for c in rows[h]]
    keys = {"продано порций": "portions", "начислено": "amount", "оплачено": "paid"}
    result: dict[str, dict[str, Decimal]] = {}
    for row in rows[h + 1:]:
        title = low(row[0])
        if not title:
            break
        key = next((v for k, v in keys.items() if title.startswith(k)), None)
        if key:
            result[key] = {
                ("ВСЕГО" if low(names[i]) == "всего" else names[i]): to_decimal(row[i]) or Decimal("0")
                for i in range(1, len(row))
                if names[i]
            }
    return result or None


# ------------------------------------------------------------------ Миграция

def canonical_client(name: str) -> str:
    """Приводит написание клиента к эталонному (FOYDALI FOOD GROUP / NO NAMELA)."""
    key = re.sub(r"[^a-zа-я0-9]", "", name.lower())
    for ref in DEFAULT_CLIENTS:
        if key and re.sub(r"[^a-z0-9]", "", ref.lower()) in key:
            return ref
    return name


def fmt(n) -> str:
    return f"{Decimal(n):,.0f}".replace(",", " ")


def check_totals(deliveries: list[dict], payments: list[dict], expected) -> bool:
    """Сверка с листом «Итог». True — всё сошлось."""
    clients = sorted({d["client"] for d in deliveries} | {p["client"] for p in payments})
    actual = {"portions": {}, "amount": {}, "paid": {}}
    for c in clients:
        actual["portions"][c] = Decimal(sum(d["portions"] for d in deliveries if d["client"] == c))
        actual["amount"][c] = sum((d["amount"] for d in deliveries if d["client"] == c), Decimal("0"))
        actual["paid"][c] = sum((p["amount"] for p in payments if p["client"] == c), Decimal("0"))
    for key in actual:
        actual[key]["ВСЕГО"] = sum(actual[key].values(), Decimal("0"))

    labels = {"portions": "Порций", "amount": "Начислено, сум", "paid": "Оплачено, сум"}
    for key, label in labels.items():
        print(f"  {label}: " + ", ".join(f"{c} = {fmt(v)}" for c, v in actual[key].items()))

    if not expected:
        print("  ⚠️  Лист «Итог» не найден — сверка не выполнена")
        return True
    ok = True
    for key, label in labels.items():
        for who, exp in expected.get(key, {}).items():
            got = actual[key].get(who, Decimal("0"))
            if abs(got - exp) > Decimal("1"):
                print(f"  ⚠️  {label} / {who}: в «Итог» {fmt(exp)}, посчитано {fmt(got)}")
                ok = False
    print("  ✅ Сверка с листом «Итог» сошлась" if ok else "  ❌ Есть расхождения с листом «Итог»")
    return ok


async def migrate(path: Path, reset: bool, dry_run: bool) -> int:
    print(f"📂 Читаю {path} …")
    wb = load_workbook(path, data_only=True)  # data_only — значения формул, а не сами формулы

    deliveries, payments = read_daily(wb)
    recipes, products, ingredients = read_costing(wb)
    expected = read_expected(wb)

    print(f"  Товаров: {len(products)}, рецептов: {len(recipes)}, строк состава: {len(ingredients)}")
    print(f"  Поставок: {len(deliveries)}, оплат: {len(payments)}")
    ok = check_totals(deliveries, payments, expected)

    if dry_run:
        print("🔎 Dry-run: в базу ничего не записано.")
        return 0 if ok else 2

    await init_db()
    async with session_scope() as session:
        existing = (await session.execute(select(func.count()).select_from(ClientDelivery))).scalar_one()
        existing += (await session.execute(select(func.count()).select_from(Payment))).scalar_one()
        existing += (await session.execute(select(func.count()).select_from(Stock))).scalar_one()
        if existing and not reset:
            print(f"❌ В базе уже есть данные ({existing} записей). Запустите с --reset, чтобы импортировать заново.")
            return 1
        if reset:
            print("🧹 Очищаю таблицы (поставки, оплаты, склад, рецепты, закупки, партии)…")
            for model in (Batch, RecipeIngredient, Recipe, ClientDelivery, Payment, Purchase, Stock):
                await session.execute(delete(model))

        # Клиенты: существующие привязки Telegram сохраняются
        client_names = set(DEFAULT_CLIENTS) | {d["client"] for d in deliveries} | {p["client"] for p in payments}
        known = set((await session.execute(select(Client.name))).scalars().all())
        for name in sorted(client_names - known):
            session.add(Client(name=name))

        # Склад (остатков в файле нет — 0, появятся после закупок)
        stock_by_name: dict[str, Stock] = {}
        for p in products:
            s = Stock(name=p["name"], unit=p["unit"], quantity=Decimal("0"), price_per_unit=p["price"])
            session.add(s)
            stock_by_name[p["name"].lower()] = s
        await session.flush()

        # Рецепты
        recipe_by_name: dict[str, Recipe] = {}
        for name, base_yield in recipes.items():
            r = Recipe(name=name, base_yield=base_yield)
            session.add(r)
            recipe_by_name[name.lower()] = r
        await session.flush()

        # Состав. Продукта нет в ценах → создаём с ценой 0, чтобы не потерять строку
        missing = set()
        for ing in ingredients:
            product = stock_by_name.get(ing["product"].lower())
            if product is None:
                product = Stock(name=ing["product"], unit=ing["unit"], quantity=Decimal("0"), price_per_unit=Decimal("0"))
                session.add(product)
                await session.flush()
                stock_by_name[ing["product"].lower()] = product
                missing.add(ing["product"])
            session.add(
                RecipeIngredient(
                    recipe_id=recipe_by_name[ing["dish"].lower()].id,
                    product_id=product.id,
                    quantity=ing["quantity"],
                    unit=ing["unit"],
                )
            )
        if missing:
            print(f"  ⚠️  Нет цены для {len(missing)} продуктов (созданы с ценой 0): {sorted(missing)[:10]}")

        for d in deliveries:
            session.add(
                ClientDelivery(
                    client_name=d["client"], dish_name=d["dish"], portions=d["portions"],
                    amount=d["amount"], cost=d["cost"], date=d["date"],
                )
            )
        for p in payments:
            session.add(Payment(client_name=p["client"], amount=p["amount"], payment_type="cash", date=p["date"]))

    print("✅ Импорт завершён" + ("" if ok else " (с расхождениями — проверьте файл)"))
    return 0 if ok else 2


def main() -> None:
    parser = argparse.ArgumentParser(description="Импорт Учёт_кухни.xlsx в базу Kuxnya NVGR")
    parser.add_argument("path", nargs="?", default="Учёт_кухни.xlsx")
    parser.add_argument("--reset", action="store_true", help="очистить данные перед импортом")
    parser.add_argument("--dry-run", action="store_true", help="только прочитать и проверить файл")
    args = parser.parse_args()
    path = Path(args.path)
    if not path.exists():
        print(f"❌ Файл не найден: {path}")
        sys.exit(1)
    sys.exit(asyncio.run(migrate(path, args.reset, args.dry_run)))


if __name__ == "__main__":
    main()
