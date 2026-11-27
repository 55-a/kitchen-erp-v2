"""Тесты простого экрана повара (шаги дня), заявок клиентов «Завтра нас: N», архива рецептов и «Закончилось»."""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select

from api.main import app
from api.models import Client, ClientDelivery, ClientOrder, Recipe, RecipeIngredient, Stock, User
from api.routers import lunch
from tests.conftest import CLIENT_ID, COOK_ID, OWNER_ID, PURCHASER_ID, auth_headers

pytestmark = pytest.mark.asyncio(loop_scope="session")

TZ = lunch.TASHKENT


def weekday_on_or_after(start: dt.date, weekday: int) -> dt.date:
    return start + dt.timedelta(days=(weekday - start.weekday()) % 7)


WED = weekday_on_or_after(dt.date(2026, 11, 23), 2)
THU, FRI = WED + dt.timedelta(days=1), WED + dt.timedelta(days=2)
MON = WED + dt.timedelta(days=5)


def set_now(monkeypatch, day: dt.date, hour: int, minute: int = 0) -> None:
    monkeypatch.setattr(lunch, "local_now", lambda: dt.datetime.combine(day, dt.time(hour, minute), tzinfo=TZ))


@pytest_asyncio.fixture(loop_scope="session")
async def api(db):
    async with db() as s:
        rice = Stock(name="Рис", unit="kg", quantity=Decimal("1"), price_per_unit=Decimal("15000"))
        carrot = Stock(name="Морковь", unit="kg", quantity=Decimal("5"), price_per_unit=Decimal("6000"))
        s.add_all([
            User(telegram_id=OWNER_ID, role="owner", language="ru"),
            User(telegram_id=COOK_ID, role="cook", language="ru"),
            User(telegram_id=PURCHASER_ID, role="purchaser", language="ru"),
            User(telegram_id=CLIENT_ID, role="client", language="ru"),
            Client(name="FOYDALI FOOD GROUP", telegram_id=CLIENT_ID, price_per_portion=Decimal("30000")),
            Client(name="NO NAMELA", price_per_portion=Decimal("30000")),
            rice, carrot,
        ])
        # История: обычно FOYDALI — 4 человека, NO NAMELA — 25
        for i, (f, n) in enumerate(((4, 25), (5, 30), (3, 20))):
            day = dt.date(2026, 11, 2) + dt.timedelta(days=i)
            s.add(ClientDelivery(client_name="FOYDALI FOOD GROUP", dish_name="Плов", portions=f,
                                 amount=Decimal(f * 30000), cost=0, date=day))
            s.add(ClientDelivery(client_name="NO NAMELA", dish_name="Плов", portions=n,
                                 amount=Decimal(n * 30000), cost=0, date=day))
        await s.flush()
        s.add_all([
            Recipe(name="Плов", base_yield=Decimal("10"), ingredients=[
                RecipeIngredient(product_id=rice.id, quantity=Decimal("2"), unit="kg"),
                RecipeIngredient(product_id=carrot.id, quantity=Decimal("1"), unit="kg")]),
            Recipe(name="Самса", base_yield=Decimal("10"), ingredients=[
                RecipeIngredient(product_id=carrot.id, quantity=Decimal("500"), unit="g")]),
        ])
        await s.commit()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def recipe_ids(db) -> dict[str, int]:
    async with db() as s:
        return {r.name: r.id for r in (await s.execute(select(Recipe))).scalars().all()}


# ------------------------------------------------------------------ Заявка клиента

async def test_client_order_until_deadline(api, monkeypatch):
    set_now(monkeypatch, WED, 10)
    r = await api.get("/api/client/order", headers=auth_headers(CLIENT_ID))
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["usual"] == 4 and data["next"]["date"] == THU.isoformat() and data["next"]["portions"] is None
    assert data["today"] == {"date": WED.isoformat(), "weekday": 2, "portions": 4, "ordered": False}

    r = await api.put("/api/client/order", json={"date": THU.isoformat(), "portions": 6}, headers=auth_headers(CLIENT_ID))
    assert r.status_code == 200 and r.json()["next"]["portions"] == 6

    set_now(monkeypatch, WED, 17, 30)   # после 17:00 завтрашний день закрыт — предлагаем пятницу
    assert (await api.get("/api/client/order", headers=auth_headers(CLIENT_ID))).json()["next"]["date"] == FRI.isoformat()
    r = await api.put("/api/client/order", json={"date": THU.isoformat(), "portions": 9}, headers=auth_headers(CLIENT_ID))
    assert r.status_code == 400 and r.json()["detail"]["code"] == "order_closed"

    set_now(monkeypatch, FRI, 18)       # в пятницу вечером — заявка на понедельник (срок — воскресенье 17:00)
    assert (await api.get("/api/client/order", headers=auth_headers(CLIENT_ID))).json()["next"]["date"] == MON.isoformat()

    set_now(monkeypatch, THU, 8)        # владелец может поправить заявку любого клиента и после срока
    r = await api.put("/api/client/order", json={"date": THU.isoformat(), "portions": 7, "client": "NO NAMELA"},
                      headers=auth_headers(OWNER_ID))
    assert r.status_code == 200 and r.json()["client"] == "NO NAMELA" and r.json()["today"]["portions"] == 7


async def test_cook_cannot_order_and_client_cannot_cook(api):
    assert (await api.get("/api/client/order", headers=auth_headers(COOK_ID))).status_code == 403
    assert (await api.get("/api/cook/day", headers=auth_headers(CLIENT_ID))).status_code == 403


# ------------------------------------------------------------------ Повар: шаги дня

async def test_cook_day_steps_and_undo(api, db, monkeypatch):
    set_now(monkeypatch, THU, 9)
    async with db() as s:
        s.add(ClientOrder(date=THU, client_name="FOYDALI FOOD GROUP", portions=6))
        await s.commit()
    ids = await recipe_ids(db)
    h = auth_headers(COOK_ID)

    day = (await api.get("/api/cook/day", headers=h)).json()
    assert day["step"] == "dishes"
    assert {p["client"]: (p["portions"], p["ordered"]) for p in day["people"]} == {
        "FOYDALI FOOD GROUP": (6, True), "NO NAMELA": (25, False)}
    assert day["people_total"] == 31 and day["to_cook"] == 33          # 31 + 5 % запаса, вверх

    day = (await api.put("/api/cook/day/dishes", json={"recipe_ids": [ids["Плов"], ids["Самса"]]}, headers=h)).json()
    assert day["step"] == "cook" and [d["recipe"] for d in day["dishes"]] == ["Плов", "Самса"]

    # Риса на складе 1 кг, а нужно 6,6 кг — повара это не останавливает, остаток уходит в минус
    r = await api.post("/api/cook/day/cooked", json={"portions": 33}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["step"] == "deliver" and r.json()["cooked"] == 33
    async with db() as s:
        assert (await s.execute(select(Stock.quantity).where(Stock.name == "Рис"))).scalar_one() == Decimal("-5.6")

    too_many = [{"client": "FOYDALI FOOD GROUP", "portions": 10}, {"client": "NO NAMELA", "portions": 30}]
    r = await api.post("/api/cook/day/delivered", json={"items": too_many}, headers=h)
    assert r.status_code == 400 and r.json()["detail"]["code"] == "not_enough_portions"

    items = [{"client": "FOYDALI FOOD GROUP", "portions": 6}, {"client": "NO NAMELA", "portions": 25}]
    day = (await api.post("/api/cook/day/delivered", json={"items": items}, headers=h)).json()
    assert day["step"] == "leftover" and day["remaining"] == 2 and day["delivered_total"] == 31
    async with db() as s:
        today = (await s.execute(select(ClientDelivery).where(ClientDelivery.date == THU))).scalars().all()
    # Одна поставка на клиента за весь обед (а не по каждому блюду) — выручка не задваивается
    assert sorted((d.client_name, d.dish_name, d.portions, d.amount) for d in today) == [
        ("FOYDALI FOOD GROUP", "Плов + Самса", 6, Decimal("180000.00")),
        ("NO NAMELA", "Плов + Самса", 25, Decimal("750000.00"))]

    day = (await api.post("/api/cook/day/leftover", json={"portions": 1}, headers=h)).json()
    assert day["step"] == "done" and day["leftover"] == 1 and day["remaining"] == 0

    # «↩ Отменить» — по одному шагу назад; после отмены «приготовил» продукты вернулись на склад
    assert (await api.post("/api/cook/day/undo", headers=h)).json()["step"] == "leftover"
    assert (await api.post("/api/cook/day/undo", headers=h)).json()["step"] == "deliver"
    async with db() as s:
        assert not (await s.execute(select(ClientDelivery).where(ClientDelivery.date == THU))).scalars().all()
    day = (await api.post("/api/cook/day/undo", headers=h)).json()
    assert day["step"] == "cook" and day["can_undo"] is True
    async with db() as s:
        assert (await s.execute(select(Stock.quantity).where(Stock.name == "Рис"))).scalar_one() == Decimal("1")
    # Защита от ошибок: можно отменить и выбор блюд — назад до самого начала
    day = (await api.post("/api/cook/day/undo", headers=h)).json()
    assert day["step"] == "dishes" and day["dishes"] == [] and day["can_undo"] is False
    r = await api.post("/api/cook/day/undo", headers=h)
    assert r.status_code == 400 and r.json()["detail"]["code"] == "nothing_to_undo"


async def test_cooked_needs_dishes_first(api, monkeypatch):
    set_now(monkeypatch, THU, 9)
    r = await api.post("/api/cook/day/cooked", json={"portions": 10}, headers=auth_headers(COOK_ID))
    assert r.status_code == 400 and r.json()["detail"]["code"] == "nothing_to_cook"


# ------------------------------------------------------------------ Рецепты: архив вместо удаления

async def test_recipe_archive_and_restore(api, db):
    ids = await recipe_ids(db)
    h = auth_headers(COOK_ID)
    r = await api.post(f"/api/recipes/{ids['Плов']}/archive", json={"archived": True}, headers=h)
    assert r.status_code == 200 and r.json()["archived"] is True
    assert [x["name"] for x in (await api.get("/api/recipes", headers=h)).json()] == ["Самса"]
    assert [x["name"] for x in (await api.get("/api/recipes", params={"archived": "true"}, headers=h)).json()] == ["Плов"]

    async with db() as s:
        carrot = (await s.execute(select(Stock.id).where(Stock.name == "Морковь"))).scalar_one()
    new = {"name": "плов", "base_yield": 10, "ingredients": [{"product_id": carrot, "quantity": 1, "unit": "kg"}]}
    r = await api.post("/api/recipes", json=new, headers=h)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "recipe_in_archive"

    await api.post(f"/api/recipes/{ids['Плов']}/archive", json={"archived": False}, headers=h)
    assert len((await api.get("/api/recipes", headers=h)).json()) == 2


# ------------------------------------------------------------------ «🛒 Закончилось»

async def test_shortage_goes_to_purchaser_and_owner(api):
    from api.notify import SENT

    SENT.clear()
    r = await api.post("/api/cook/shortage", json={"items": ["Масло", "  Рис  "]}, headers=auth_headers(COOK_ID))
    assert r.status_code == 200 and r.json()["sent"] == 2
    assert sorted(m["chat_id"] for m in SENT) == [OWNER_ID, PURCHASER_ID]
    assert "• Масло\n• Рис" in SENT[0]["text"] and SENT[0]["button_url"] is None
