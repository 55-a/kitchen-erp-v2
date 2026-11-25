"""Тесты API: авторизация initData, /api/stats, /api/stock и ключевые бизнес-правила."""
from __future__ import annotations

import datetime as dt
import time
from decimal import Decimal

import httpx
import pytest
import pytest_asyncio

from api.auth import InitDataError, validate_init_data
from api.main import app
from api.models import Client, ClientDelivery, Payment, Recipe, RecipeIngredient, Stock, User
from tests.conftest import (
    CLIENT_ID,
    COOK_ID,
    OWNER_ID,
    PURCHASER_ID,
    auth_headers,
    make_init_data,
)

pytestmark = pytest.mark.asyncio(loop_scope="session")


@pytest_asyncio.fixture(loop_scope="session")
async def client(db):
    """HTTP-клиент к приложению + базовые данные."""
    async with db() as s:
        s.add_all(
            [
                User(telegram_id=COOK_ID, role="cook", language="ru"),
                User(telegram_id=PURCHASER_ID, role="purchaser", language="ru"),
                User(telegram_id=CLIENT_ID, role="client", language="en"),
                Client(name="FOYDALI FOOD GROUP", telegram_id=CLIENT_ID),
                Client(name="NO NAMELA"),
                Stock(name="Рис", unit="kg", quantity=Decimal("10"), price_per_unit=Decimal("15000")),
                Stock(name="Морковь", unit="kg", quantity=Decimal("5"), price_per_unit=Decimal("6000")),
                ClientDelivery(client_name="FOYDALI FOOD GROUP", dish_name="Плов", portions=100,
                               amount=Decimal("3000000"), cost=Decimal("1800000"), date=dt.date(2025, 10, 1)),
                ClientDelivery(client_name="NO NAMELA", dish_name="Плов", portions=50,
                               amount=Decimal("1500000"), cost=Decimal("900000"), date=dt.date(2025, 11, 2)),
                Payment(client_name="FOYDALI FOOD GROUP", amount=Decimal("1000000"), payment_type="cash",
                        date=dt.date(2025, 10, 5)),
            ]
        )
        await s.commit()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# ------------------------------------------------------------------ Авторизация

async def test_health_is_public(client):
    r = await client.get("/api/health")
    assert r.status_code == 200


async def test_stats_without_init_data_is_403(client):
    r = await client.get("/api/stats")
    assert r.status_code == 403


async def test_stats_with_forged_signature_is_403(client):
    forged = make_init_data(OWNER_ID, bot_token="999:WRONG")
    r = await client.get("/api/stats", headers={"X-Telegram-Init-Data": forged})
    assert r.status_code == 403


async def test_expired_init_data_is_rejected():
    old = make_init_data(OWNER_ID, auth_date=int(time.time()) - 3 * 86400)
    with pytest.raises(InitDataError):
        validate_init_data(old, "123456:TEST-TOKEN", ttl=86400)


async def test_authorization_header_tma_is_supported(client):
    r = await client.get("/api/stats", headers={"Authorization": "tma " + make_init_data(OWNER_ID)})
    assert r.status_code == 200


async def test_stats_forbidden_for_cook(client):
    r = await client.get("/api/stats", headers=auth_headers(COOK_ID))
    assert r.status_code == 403


# ------------------------------------------------------------------ /api/stats

async def test_stats_totals(client):
    r = await client.get("/api/stats", headers=auth_headers(OWNER_ID))
    assert r.status_code == 200
    s = r.json()
    assert s["revenue"] == 4_500_000
    assert s["cost"] == 2_700_000
    assert s["profit"] == 1_800_000
    assert s["margin"] == 40.0
    assert s["portions"] == 150
    assert [m["month"] for m in s["by_month"]] == ["2025-10", "2025-11"]
    foydali = next(c for c in s["by_client"] if c["client"] == "FOYDALI FOOD GROUP")
    assert foydali["debt"] == 2_000_000


async def test_stats_period_filter(client):
    r = await client.get("/api/stats", params={"date_from": "2025-11-01"}, headers=auth_headers(OWNER_ID))
    assert r.status_code == 200
    assert r.json()["revenue"] == 1_500_000


# ------------------------------------------------------------------ /api/stock

async def test_stock_list(client):
    r = await client.get("/api/stock", headers=auth_headers(COOK_ID))
    assert r.status_code == 200
    names = [p["name"] for p in r.json()]
    assert names == sorted(names)
    rice = next(p for p in r.json() if p["name"] == "Рис")
    assert rice["quantity"] == 10.0
    assert rice["value"] == 150_000.0


async def test_stock_forbidden_for_client(client):
    r = await client.get("/api/stock", headers=auth_headers(CLIENT_ID))
    assert r.status_code == 403


# ------------------------------------------------------------------ Закупка

async def test_purchase_unknown_product_returns_400(client):
    r = await client.post(
        "/api/purchase",
        json={"product_name": "Несуществующий товар", "quantity": 1, "total_price": 1000},
        headers=auth_headers(PURCHASER_ID),
    )
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "product_not_found"


async def test_purchase_increases_stock_with_unit_conversion(client, db):
    stock = (await client.get("/api/stock", headers=auth_headers(PURCHASER_ID))).json()
    rice_id = next(p["id"] for p in stock if p["name"] == "Рис")
    r = await client.post(
        "/api/purchase",
        json={"product_id": rice_id, "quantity": 5000, "unit": "g", "total_price": 80000},
        headers=auth_headers(PURCHASER_ID),
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["stock"]["quantity"] == 15.0  # 10 кг + 5000 г
    # Средневзвешенная цена: (10*15000 + 80000) / 15
    assert body["stock"]["price_per_unit"] == pytest.approx(15333.33, abs=0.01)


# ------------------------------------------------------------------ Партия

async def test_batch_insufficient_stock_and_success(client, db):
    async with db() as s:
        rice = (await s.execute(Stock.__table__.select().where(Stock.name == "Рис"))).first()
        recipe = Recipe(name="Плов", base_yield=Decimal("10"))
        s.add(recipe)
        await s.flush()
        s.add(RecipeIngredient(recipe_id=recipe.id, product_id=rice.id, quantity=Decimal("2000"), unit="g"))
        await s.commit()
        recipe_id = recipe.id

    # 100 порций = 20 кг риса, на складе 10 → 400
    r = await client.post("/api/batch", json={"recipe_id": recipe_id, "planned_yield": 100}, headers=auth_headers(COOK_ID))
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "insufficient_stock"

    # 20 порций = 4 кг → списываем
    r = await client.post(
        "/api/batch", json={"recipe_id": recipe_id, "planned_yield": 20, "actual_yield": 19}, headers=auth_headers(COOK_ID)
    )
    assert r.status_code == 201, r.text
    assert r.json()["cost"] == 60_000.0
    stock = (await client.get("/api/stock", headers=auth_headers(COOK_ID))).json()
    assert next(p for p in stock if p["name"] == "Рис")["quantity"] == 6.0


# ------------------------------------------------------------------ Клиент

async def test_client_invoice_and_isolation(client):
    r = await client.get("/api/client/invoice", params={"tg_id": CLIENT_ID}, headers=auth_headers(CLIENT_ID))
    assert r.status_code == 200
    inv = r.json()
    assert (inv["charged"], inv["paid"], inv["debt"]) == (3_000_000, 1_000_000, 2_000_000)

    # Попытка подставить чужой tg_id
    r = await client.get("/api/client/invoice", params={"tg_id": 999}, headers=auth_headers(CLIENT_ID))
    assert r.status_code == 403


async def test_owner_in_client_role_can_preview_any_client(client, db):
    """Владелец, переключившийся в роль «Клиент», выбирает компанию и видит её кабинет."""
    async with db() as s:
        s.add(User(telegram_id=OWNER_ID, role="client", language="ru"))
        await s.commit()
    h = auth_headers(OWNER_ID)

    r = await client.get("/api/clients", headers=h)
    assert r.status_code == 200
    assert {c["name"] for c in r.json()} == {"FOYDALI FOOD GROUP", "NO NAMELA"}

    r = await client.get("/api/client/invoice", params={"client": "NO NAMELA"}, headers=h)
    assert r.status_code == 200 and r.json()["charged"] == 1_500_000

    r = await client.get("/api/client/deliveries", params={"client": "FOYDALI FOOD GROUP"}, headers=h)
    assert r.status_code == 200 and r.json()["total_portions"] == 100

    # Без выбора компании (и без привязки) — 400, а не чужие данные
    r = await client.get("/api/client/invoice", headers=h)
    assert r.status_code == 400


async def test_regular_client_cannot_list_or_pick_other_clients(client):
    h = auth_headers(CLIENT_ID)
    assert (await client.get("/api/clients", headers=h)).status_code == 403
    # Параметр client игнорируется: обычный клиент всегда получает только свою компанию
    r = await client.get("/api/client/invoice", params={"client": "NO NAMELA"}, headers=h)
    assert r.status_code == 200 and r.json()["client"] == "FOYDALI FOOD GROUP"


async def test_stats_csv_export_has_totals_and_clients(client):
    """CSV-отчёт для кнопки «Экспорт»: итоги, месяцы и клиенты, BOM для Excel."""
    from api.routers.stats import build_stats_csv

    r = await client.get("/api/stats", headers=auth_headers(OWNER_ID))
    data = build_stats_csv(r.json())
    text = data.decode("utf-8")
    assert text.startswith("\ufeff")
    assert "Выручка, сум;4500000" in text
    assert "FOYDALI FOOD GROUP;100;3000000" in text


async def test_stats_export_forbidden_for_cook(client):
    r = await client.post("/api/stats/export-send", json={}, headers=auth_headers(COOK_ID))
    assert r.status_code == 403
