"""
Импорт исторических данных из Учёт_кухни.xlsx в PostgreSQL.

Запуск:
    python -m migrations.migrate_excel "Учёт_кухни.xlsx"            # импорт
    python -m migrations.migrate_excel "Учёт_кухни.xlsx" --dry-run  # только проверка, без записи
    python -m migrations.migrate_excel "Учёт_кухни.xlsx" --reset    # очистить и импортировать заново

Листы:
    "Цены продуктов"  → stock              (товар, единица, цена, [остаток])
    "Себестоимость"   → recipes            (блюдо, выход)
    "Состав закладок" → recipe_ingredients (блюдо, продукт, количество, единица)
    "По дням"         → client_deliveries  (дата, клиент, блюдо, порции, сумма, себестоимость)

Колонки ищутся по названиям заголовков (с синонимами), а не по буквам —
скрипт не сломается, если в таблицу добавят столбец.
После импорта сверяются контрольные суммы: 527 порций и ~15.8 млн сум выручки.
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
from api.models import Batch, Client, ClientDelivery, Purchase, Recipe, RecipeIngredient, Stock
from api.units import normalize_unit

DEFAULT_CLIENTS = ["FOYDALI FOOD GROUP", "NO NAMELA"]
EXPECTED_PORTIONS = 527
EXPECTED_REVENUE = Decimal("15800000")

# Синонимы заголовков (сравнение без регистра, по вхождению подстроки)
HEADERS = {
    "date": ["дата", "date", "kun", "sana"],
    "client": ["клиент", "заказчик", "client", "mijoz", "покупатель"],
    "dish": ["блюдо", "наименование блюда", "закладка", "dish", "рецепт", "taom"],
    "product": ["продукт", "товар", "наименование", "ингредиент", "product", "mahsulot"],
    "portions": ["порц", "кол-во порций", "portions", "porsiya"],
    "amount": ["сумма", "выручка", "итого", "amount", "summa"],
    "price": ["цена", "price", "narx", "стоимость за"],
    "cost": ["себестоимость", "себест", "cost", "tannarx"],
    "quantity": ["количество", "кол-во", "вес", "норма", "quantity", "miqdor", "брутто"],
    "unit": ["ед", "единица", "unit", "o'lchov"],
    "yield": ["выход", "порций в закладке", "yield", "chiqish"],
    "stock_qty": ["остаток", "склад", "stock", "qoldiq"],
}

KEY_PRIORITY = [
    "dish", "date", "client", "cost", "portions", "yield", "stock_qty",
    "product", "amount", "price", "quantity", "unit",
]

SKIP_WORDS = ("итого", "всего", "total", "jami")


# ------------------------------------------------------------------ Разбор значений

def to_decimal(value) -> Decimal | None:
    """'1 250 000,50 сум' → Decimal('1250000.50')."""
    if value is None or value == "":
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
        for fmt in ("%d.%m.%Y", "%d.%m.%y", "%Y-%m-%d", "%d/%m/%Y"):
            try:
                return dt.datetime.strptime(value.strip(), fmt).date()
            except ValueError:
                continue
    return None


def clean(value) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def is_total_row(*values) -> bool:
    return any(clean(v).lower().startswith(SKIP_WORDS) for v in values if v)


# ------------------------------------------------------------------ Поиск заголовков

def find_header(ws, required: list[str], optional: list[str], scan_rows: int = 15) -> tuple[int, dict[str, int]]:
    """
    Ищет строку заголовков среди первых scan_rows строк.
    Возвращает (номер строки, {ключ: индекс колонки}). Бросает ValueError, если не нашли.
    """
    # Более специфичные ключи сопоставляем первыми: "Наименование блюда" должно уйти в dish,
    # а не в product; "Себестоимость" — в cost, а не в price; "Кол-во порций" — в portions.
    keys = sorted(required + optional, key=lambda k: KEY_PRIORITY.index(k))
    for row_idx, row in enumerate(ws.iter_rows(min_row=1, max_row=scan_rows, values_only=True), start=1):
        cells = [clean(c).lower() for c in row]
        mapping: dict[str, int] = {}
        for key in keys:
            for col, text in enumerate(cells):
                if text and col not in mapping.values() and any(s in text for s in HEADERS[key]):
                    mapping[key] = col
                    break
        if all(k in mapping for k in required):
            return row_idx, mapping
    raise ValueError(f"Лист «{ws.title}»: не найдены колонки {required}")


def rows_after(ws, header_row: int, width: int = 64):
    """Строки данных после заголовка, дополненные None до одинаковой длины
    (в режиме read_only openpyxl отдаёт короткие кортежи для пустых хвостов)."""
    for row in ws.iter_rows(min_row=header_row + 1, values_only=True):
        yield tuple(row) + (None,) * max(0, width - len(row))


def get_sheet(wb, name: str):
    """Лист по имени без учёта регистра и лишних пробелов."""
    for ws in wb.worksheets:
        if clean(ws.title).lower() == name.lower():
            return ws
    raise ValueError(f"Лист «{name}» не найден. Есть: {[ws.title for ws in wb.worksheets]}")


# ------------------------------------------------------------------ Чтение листов

def read_products(wb) -> list[dict]:
    ws = get_sheet(wb, "Цены продуктов")
    header_row, cols = find_header(ws, ["product", "price"], ["unit", "stock_qty"])
    items: dict[str, dict] = {}
    for row in rows_after(ws, header_row):
        name = clean(row[cols["product"]])
        price = to_decimal(row[cols["price"]])
        if not name or price is None or is_total_row(name):
            continue
        unit = normalize_unit(clean(row[cols["unit"]])) if "unit" in cols else "kg"
        qty = to_decimal(row[cols["stock_qty"]]) if "stock_qty" in cols else None
        items[name.lower()] = {"name": name, "unit": unit or "kg", "price": price, "quantity": qty or Decimal("0")}
    return list(items.values())


def read_recipes(wb) -> tuple[dict[str, Decimal], dict[str, Decimal]]:
    """
    Возвращает (базовый выход по блюдам, себестоимость ОДНОЙ порции по блюдам).
    Себестоимость нужна, если в листе "По дням" нет своей колонки себестоимости.
    """
    ws = get_sheet(wb, "Себестоимость")
    header_row, cols = find_header(ws, ["dish"], ["yield", "cost"])
    header_cells = [clean(c).lower() for c in next(ws.iter_rows(min_row=header_row, max_row=header_row, values_only=True))]
    cost_is_per_portion = "cost" in cols and "порц" in header_cells[cols["cost"]]

    recipes: dict[str, Decimal] = {}
    unit_costs: dict[str, Decimal] = {}
    for row in rows_after(ws, header_row):
        name = clean(row[cols["dish"]])
        if not name or is_total_row(name):
            continue
        y = to_decimal(row[cols["yield"]]) if "yield" in cols else None
        base_yield = y if y and y > 0 else Decimal("1")
        recipes[name] = base_yield
        cost = to_decimal(row[cols["cost"]]) if "cost" in cols else None
        if cost is not None:
            unit_costs[name.lower()] = cost if cost_is_per_portion else cost / base_yield
    return recipes, unit_costs


def read_ingredients(wb) -> list[dict]:
    """
    Поддерживает два формата листа:
    1) таблица с колонкой "Блюдо" в каждой строке;
    2) блоки: строка с названием блюда, под ней строки ингредиентов.
    """
    ws = get_sheet(wb, "Состав закладок")
    header_row, cols = find_header(ws, ["product", "quantity"], ["dish", "unit"])
    rows: list[dict] = []
    current_dish: str | None = None
    for row in rows_after(ws, header_row):
        product = clean(row[cols["product"]]) if cols["product"] < len(row) else ""
        quantity = to_decimal(row[cols["quantity"]]) if cols["quantity"] < len(row) else None
        dish = clean(row[cols["dish"]]) if "dish" in cols and cols["dish"] < len(row) else ""

        if dish and not is_total_row(dish):
            current_dish = dish
        elif not dish and product and quantity is None and "dish" not in cols:
            # Строка-заголовок блока: только название блюда
            current_dish = product
            continue
        if product and quantity and quantity > 0 and current_dish and not is_total_row(product):
            unit = normalize_unit(clean(row[cols["unit"]])) if "unit" in cols else "g"
            rows.append({"dish": current_dish, "product": product, "quantity": quantity, "unit": unit})
    # Отбрасываем строку самого заголовка, если она попала в выборку
    return [r for r in rows if r["product"].lower() not in HEADERS["product"]]


def read_deliveries(wb) -> list[dict]:
    ws = get_sheet(wb, "По дням")
    header_row, cols = find_header(ws, ["date", "portions"], ["client", "dish", "amount", "price", "cost"])
    rows: list[dict] = []
    last_date: dt.date | None = None
    for row in rows_after(ws, header_row):
        d = to_date(row[cols["date"]]) or last_date  # объединённые ячейки даты
        portions = to_decimal(row[cols["portions"]])
        client = clean(row[cols["client"]]) if "client" in cols else ""
        dish = clean(row[cols["dish"]]) if "dish" in cols else ""
        if d is None or not portions or portions <= 0 or is_total_row(row[cols["date"]], client, dish):
            continue
        last_date = d
        amount = to_decimal(row[cols["amount"]]) if "amount" in cols else None
        if amount is None and "price" in cols:
            price = to_decimal(row[cols["price"]])
            amount = price * portions if price is not None else None
        cost = to_decimal(row[cols["cost"]]) if "cost" in cols else None
        rows.append(
            {
                "date": d,
                "client": client or DEFAULT_CLIENTS[0],
                "dish": dish or "—",
                "portions": int(portions),
                "amount": amount or Decimal("0"),
                "cost": cost or Decimal("0"),
            }
        )
    return rows


# ------------------------------------------------------------------ Запись в БД

def canonical_client(name: str) -> str:
    """Приводит написание клиента к эталонному (FOYDALI FOOD GROUP / NO NAMELA)."""
    key = re.sub(r"[^a-zа-я0-9]", "", name.lower())
    for ref in DEFAULT_CLIENTS:
        # "Foydali Food Group LLC", "FOYDALI-FOOD GROUP" → FOYDALI FOOD GROUP
        if key and re.sub(r"[^a-z0-9]", "", ref.lower()) in key:
            return ref
    return name


async def migrate(path: Path, reset: bool, dry_run: bool) -> int:
    print(f"📂 Читаю {path} …")
    wb = load_workbook(path, data_only=True, read_only=True)  # data_only — значения формул, а не сами формулы

    products = read_products(wb)
    recipes, unit_costs = read_recipes(wb)
    ingredients = read_ingredients(wb)
    deliveries = read_deliveries(wb)

    # Себестоимость поставки не указана → порции × себестоимость порции из листа "Себестоимость"
    for d in deliveries:
        if not d["cost"] and d["dish"].lower() in unit_costs:
            d["cost"] = (unit_costs[d["dish"].lower()] * d["portions"]).quantize(Decimal("0.01"))

    total_portions = sum(d["portions"] for d in deliveries)
    total_revenue = sum((d["amount"] for d in deliveries), Decimal("0"))
    print(f"  Товаров: {len(products)}, рецептов: {len(recipes)}, строк состава: {len(ingredients)}")
    print(f"  Поставок: {len(deliveries)}, порций: {total_portions}, выручка: {total_revenue:,.0f} сум".replace(",", " "))

    # Контрольные суммы из ТЗ
    ok = True
    if total_portions != EXPECTED_PORTIONS:
        print(f"  ⚠️  Ожидалось {EXPECTED_PORTIONS} порций, получено {total_portions}")
        ok = False
    if abs(total_revenue - EXPECTED_REVENUE) > EXPECTED_REVENUE * Decimal("0.01"):
        print(f"  ⚠️  Ожидалось ~{EXPECTED_REVENUE:,.0f} сум выручки".replace(",", " "))
        ok = False

    if dry_run:
        print("🔎 Dry-run: в базу ничего не записано.")
        return 0 if ok else 2

    await init_db()
    async with session_scope() as session:
        existing = (await session.execute(select(func.count()).select_from(ClientDelivery))).scalar_one()
        if existing and not reset:
            print(f"❌ В базе уже есть {existing} поставок. Запустите с --reset, чтобы импортировать заново.")
            return 1
        if reset:
            print("🧹 Очищаю таблицы…")
            for model in (Batch, RecipeIngredient, Recipe, ClientDelivery, Purchase, Stock):
                await session.execute(delete(model))

        # Клиенты (создаём эталонных + всех из листа "По дням"; существующие привязки Telegram сохраняются)
        client_names = set(DEFAULT_CLIENTS)
        for d in deliveries:
            d["client"] = canonical_client(d["client"])
            client_names.add(d["client"])
        known = set((await session.execute(select(Client.name))).scalars().all())
        for name in sorted(client_names - known):
            session.add(Client(name=name))

        # Склад
        stock_by_name: dict[str, Stock] = {}
        for p in products:
            s = Stock(name=p["name"], unit=p["unit"], quantity=p["quantity"], price_per_unit=p["price"])
            session.add(s)
            stock_by_name[p["name"].lower()] = s
        await session.flush()

        # Рецепты: из "Себестоимость" + блюда, встречающиеся только в "Состав закладок"
        all_dishes = dict(recipes)
        for ing in ingredients:
            all_dishes.setdefault(ing["dish"], Decimal("1"))
        recipe_by_name: dict[str, Recipe] = {}
        for name, base_yield in all_dishes.items():
            r = Recipe(name=name, base_yield=base_yield)
            session.add(r)
            recipe_by_name[name.lower()] = r
        await session.flush()

        # Состав. Продукта нет в ценах → создаём с нулевой ценой, чтобы не потерять строку
        missing_products = set()
        for ing in ingredients:
            product = stock_by_name.get(ing["product"].lower())
            if product is None:
                stock_unit = "kg" if ing["unit"] in {"g", "kg"} else ("l" if ing["unit"] in {"ml", "l"} else ing["unit"])
                product = Stock(name=ing["product"], unit=stock_unit, quantity=Decimal("0"), price_per_unit=Decimal("0"))
                session.add(product)
                await session.flush()
                stock_by_name[ing["product"].lower()] = product
                missing_products.add(ing["product"])
            session.add(
                RecipeIngredient(
                    recipe_id=recipe_by_name[ing["dish"].lower()].id,
                    product_id=product.id,
                    quantity=ing["quantity"],
                    unit=ing["unit"],
                )
            )
        if missing_products:
            print(f"  ⚠️  Нет цены для {len(missing_products)} продуктов (созданы с ценой 0): {sorted(missing_products)[:10]}")

        # Поставки
        for d in deliveries:
            session.add(
                ClientDelivery(
                    client_name=d["client"], dish_name=d["dish"], portions=d["portions"],
                    amount=d["amount"], cost=d["cost"], date=d["date"],
                )
            )

    print("✅ Импорт завершён" + ("" if ok else " (с расхождениями контрольных сумм — проверьте файл)"))
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
