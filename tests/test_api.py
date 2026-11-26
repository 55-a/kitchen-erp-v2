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


# ------------------------------------------------------------------ Профиль, язык, ИИ-чат

async def test_me_returns_role_language_and_support(client, monkeypatch):
    import dataclasses

    import api.routers.me as me_router

    patched = dataclasses.replace(me_router.get_settings(), support_telegram="kitchen_help")
    monkeypatch.setattr(me_router, "get_settings", lambda: patched)   # Settings заморожен — подменяем целиком
    r = await client.get("/api/me", headers=auth_headers(CLIENT_ID))
    assert r.status_code == 200
    me = r.json()
    assert (me["role"], me["language"]) == ("client", "en")
    assert me["support_url"] == "https://t.me/kitchen_help"


async def test_language_is_saved_and_validated(client, db):
    r = await client.post("/api/me/language", json={"language": "uz"}, headers=auth_headers(COOK_ID))
    assert r.status_code == 200
    async with db() as s:
        from sqlalchemy import select
        assert (await s.execute(select(User.language).where(User.telegram_id == COOK_ID))).scalar_one() == "uz"
    r = await client.post("/api/me/language", json={"language": "oz"}, headers=auth_headers(COOK_ID))
    assert r.status_code == 200                               # узбекский на кириллице
    r = await client.post("/api/me/language", json={"language": "de"}, headers=auth_headers(COOK_ID))
    assert r.status_code == 400


async def test_ai_chat_sends_stock_context_and_returns_answer(client, monkeypatch):
    import api.routers.ai as ai_router

    seen = {}

    async def fake_chat(history, system):
        seen["history"], seen["system"] = history, system
        return "Купите 10 кг риса 🍚"

    monkeypatch.setattr(ai_router, "chat", fake_chat)
    r = await client.post("/api/ai/chat", json={"messages": [{"role": "user", "text": "Сколько риса купить?"}]},
                          headers=auth_headers(PURCHASER_ID))
    assert r.status_code == 200 and r.json()["answer"] == "Купите 10 кг риса 🍚"
    assert "Рис: остаток 10 kg" in seen["system"]          # ИИ видит реальный склад
    assert seen["history"][-1]["text"] == "Сколько риса купить?"


async def test_ai_chat_rules(client):
    msgs = {"messages": [{"role": "user", "text": "привет"}]}
    assert (await client.post("/api/ai/chat", json=msgs, headers=auth_headers(CLIENT_ID))).status_code == 403
    bad = {"messages": [{"role": "user", "text": "a"}, {"role": "assistant", "text": "b"}]}
    assert (await client.post("/api/ai/chat", json=bad, headers=auth_headers(PURCHASER_ID))).status_code == 400


async def test_ai_chat_skips_hanging_model_and_uses_fast_one(monkeypatch):
    """Google перегружен: первая модель «висит» — чат не ждёт минуту, а сразу берёт быструю lite-модель."""
    import dataclasses

    import httpx

    import api.gemini as gemini

    calls = []

    class FakeClient:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, json, headers, timeout):
            model = url.split("/models/")[1].split(":")[0]
            calls.append((model, timeout.read))
            if len(calls) == 1:
                raise httpx.ReadTimeout("slow")
            return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": "Ответ ✅"}]}}]},
                                  request=httpx.Request("POST", url))

    settings = dataclasses.replace(gemini.get_settings(), gemini_api_key="test-key", gemini_model="gemini-3.5-flash")
    monkeypatch.setattr(gemini, "get_settings", lambda: settings)
    monkeypatch.setattr(gemini.httpx, "AsyncClient", FakeClient)
    gemini._limiter._calls.clear()

    answer = await gemini.chat([{"role": "user", "text": "привет"}], "system")
    assert answer == "Ответ ✅"
    assert [m for m, _ in calls] == ["gemini-3.5-flash", gemini.CHAT_FAST_MODEL]   # lite — вторая
    assert all(t <= gemini.CHAT_MODEL_TIMEOUT for _, t in calls)                    # не 40 секунд


# ------------------------------------------------------------------ Акт сверки «как в 1С»

async def test_statement_opening_rows_and_closing(client, db):
    """Долг на начало + операции по датам с долгом после каждой + долг на конец."""
    from api.services import client_invoice, client_statement

    async with db() as s:
        full = await client_statement(s, "FOYDALI FOOD GROUP")
        assert full["opening"] == 0
        assert [r["kind"] for r in full["rows"]] == ["delivery", "payment"]
        assert [r["balance"] for r in full["rows"]] == [3_000_000, 2_000_000]
        # Без дат итог акта совпадает со «Счётом» клиента — текущие цифры не меняются
        assert float(full["closing"]) == (await client_invoice(s, "FOYDALI FOOD GROUP"))["debt"]

        part = await client_statement(s, "FOYDALI FOOD GROUP", dt.date(2025, 10, 3), dt.date(2025, 10, 31))
        assert part["opening"] == 3_000_000                 # поставка 01.10 — до начала периода
        assert [r["kind"] for r in part["rows"]] == ["payment"]
        assert part["closing"] == 2_000_000 and part["delivered"] == 0 and part["paid"] == 1_000_000


async def test_act_pdf_builds_in_all_languages(client, db):
    from api.pdf_generator import build_act_pdf
    from api.services import client_statement

    async with db() as s:
        st = await client_statement(s, "FOYDALI FOOD GROUP")
    for lang in ("ru", "uz", "oz", "en", "zh"):
        pdf = build_act_pdf(statement=st, lang=lang)
        assert pdf.startswith(b"%PDF") and len(pdf) > 5000


async def test_act_pdf_endpoint_for_client(client):
    r = await client.get("/api/client/act-pdf", params={"date_from": "2025-10-03"}, headers=auth_headers(CLIENT_ID))
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf"


# ------------------------------------------------------------------ План / факт / прогноз

async def test_plan_auto_from_history_and_forecast(client, db):
    from api.routers.plan import compute_plan, working_days

    assert working_days(dt.date(2025, 10, 1), dt.date(2025, 10, 31)) == 23
    assert working_days(dt.date(2025, 11, 1), dt.date(2025, 11, 30)) == 20
    async with db() as s:
        # Ноябрь 2025: история — октябрь (100 порций за 23 раб. дня), факт — 50 порций
        p = await compute_plan(s, dt.date(2025, 11, 1), today=dt.date(2025, 11, 12))
        assert p["plan"] == {"portions": 87, "revenue": 2_608_696.0, "source": "auto"}   # 100/23×20
        assert p["fact"]["portions"] == 50 and p["days"] == {"total": 20, "passed": 8, "left": 12}
        assert p["forecast"]["portions"] == 125 and p["forecast"]["pace"] == "month"    # 50 + 50/8×12

        done = await compute_plan(s, dt.date(2025, 11, 1), today=dt.date(2025, 12, 5))
        assert done["forecast"]["portions"] == 50 and done["forecast"]["pace"] == "final"


async def test_manual_plan_overrides_auto_and_can_be_reset(client):
    h = auth_headers(OWNER_ID)
    r = await client.put("/api/plan", json={"month": "2025-11", "client": None, "portions": 100, "revenue": 3_000_000}, headers=h)
    assert r.status_code == 200
    assert r.json()["plan"] == {"portions": 100, "revenue": 3_000_000.0, "source": "manual"}
    r = await client.delete("/api/plan", params={"month": "2025-11"}, headers=h)
    assert r.json()["plan"]["source"] == "auto"
    assert (await client.get("/api/plan", headers=auth_headers(COOK_ID))).status_code == 403


# ------------------------------------------------------------------ Вход: выбор роли в приложении

async def test_role_choice_in_app_follows_bot_rules(client, db):
    r = await client.post("/api/me/role", json={"role": "cook"}, headers=auth_headers(CLIENT_ID))
    assert r.status_code == 200
    me = (await client.get("/api/me", headers=auth_headers(CLIENT_ID))).json()
    assert me["role"] == "cook"
    r = await client.post("/api/me/role", json={"role": "owner"}, headers=auth_headers(CLIENT_ID))
    assert r.status_code == 403                                   # владелец — только из OWNER_IDS


async def test_entry_page_is_served(client):
    r = await client.get("/app")
    assert r.status_code == 200 and "role-grid" in r.text


async def test_pages_load_fresh_files_after_update(client):
    """После обновления Telegram не должен показывать старую копию: у CSS/JS метка версии и no-cache."""
    from api.main import ASSET_VERSION

    for path in ("/app", "/app/cook", "/app/owner", "/app/purchaser", "/app/client"):
        r = await client.get(path)
        assert r.status_code == 200 and "no-store" in r.headers["cache-control"]
        assert f'/static/js/app.js?v={ASSET_VERSION}"' in r.text
        assert f'/static/css/style.css?v={ASSET_VERSION}"' in r.text
        assert '/static/js/app.js"' not in r.text and '/static/js/i18n.js"' not in r.text
    r = await client.get(f"/static/js/app.js?v={ASSET_VERSION}")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-cache"


# ------------------------------------------------------------------ Этап 1: справочники, инвентаризация, техкарты, факт партии

async def test_purchaser_adds_new_product_and_buys_it(client):
    """«+ Новый товар» у закупщика → товар в справочнике сырья → первая закупка задаёт цену."""
    h = auth_headers(PURCHASER_ID)
    r = await client.post("/api/stock", json={"name": "  Сметана  20% ", "unit": "шт", "category": "dairy"}, headers=h)
    assert r.status_code == 201
    p = r.json()
    assert (p["name"], p["unit"], p["category"], p["quantity"], p["price_per_unit"]) == ("Сметана 20%", "pcs", "dairy", 0, 0)
    dup = await client.post("/api/stock", json={"name": "сметана 20%", "unit": "pcs"}, headers=h)
    assert dup.status_code == 409 and dup.json()["detail"]["code"] == "product_exists"
    r = await client.post("/api/purchase", json={"product_id": p["id"], "quantity": 4, "unit": "pcs", "total_price": 72000}, headers=h)
    assert r.status_code == 201 and r.json()["stock"]["price_per_unit"] == 18000
    assert (await client.post("/api/stock", json={"name": "Соль", "unit": "kg"}, headers=auth_headers(CLIENT_ID))).status_code == 403


async def test_inventory_sets_quantities_and_keeps_history(client, db):
    from sqlalchemy import select

    from api.models import StockAdjustment

    stock = (await client.get("/api/stock", headers=auth_headers(COOK_ID))).json()
    rice = next(p for p in stock if p["name"] == "Рис")
    new = (await client.post("/api/stock", json={"name": "Курдюк", "unit": "kg", "category": "meat"}, headers=auth_headers(COOK_ID))).json()
    r = await client.post("/api/stock/inventory", json={"items": [
        {"product_id": rice["id"], "quantity": 7.5, "price_per_unit": 1},         # цена у риса уже есть — не меняется
        {"product_id": new["id"], "quantity": 2, "price_per_unit": 130000},        # новый товар — цену берём
    ]}, headers=auth_headers(COOK_ID))
    assert r.status_code == 200 and r.json()["count"] == 2
    stock = {p["name"]: p for p in (await client.get("/api/stock", headers=auth_headers(COOK_ID))).json()}
    assert (stock["Рис"]["quantity"], stock["Рис"]["price_per_unit"]) == (7.5, 15000)
    assert (stock["Курдюк"]["quantity"], stock["Курдюк"]["price_per_unit"]) == (2, 130000)
    async with db() as s:
        adj = (await s.execute(select(StockAdjustment).where(StockAdjustment.product_name == "Рис"))).scalar_one()
        assert (adj.old_quantity, adj.new_quantity, adj.delta) == (10, Decimal("7.5"), Decimal("-2.5"))


async def test_cook_creates_and_edits_recipe_with_versions(client):
    h = auth_headers(COOK_ID)
    stock = {p["name"]: p for p in (await client.get("/api/stock", headers=h)).json()}
    body = {"name": "Плов тестовый", "base_yield": 10, "category": "main", "shelf_life_hours": 12,
            "ingredients": [{"product_id": stock["Рис"]["id"], "quantity": 1000, "unit": "g"},
                            {"product_id": stock["Морковь"]["id"], "quantity": 0.8, "unit": "kg"}]}
    r = await client.post("/api/recipes", json=body, headers=h)
    assert r.status_code == 201
    rec = r.json()
    assert (rec["version"], rec["shelf_life_hours"], rec["cost_per_base"]) == (1, 12, 15000 + 0.8 * 6000)

    body["shelf_life_hours"] = 6                                   # срок — не состав: версия та же
    assert (await client.put(f"/api/recipes/{rec['id']}", json=body, headers=h)).json()["version"] == 1
    body["ingredients"][1]["quantity"] = 1                         # поменяли состав — версия 2
    assert (await client.put(f"/api/recipes/{rec['id']}", json=body, headers=h)).json()["version"] == 2

    assert (await client.post("/api/recipes", json=body, headers=h)).status_code == 409   # дубль названия
    bad = dict(body, name="Другое", ingredients=[{"product_id": stock["Рис"]["id"], "quantity": 2, "unit": "pcs"}])
    r = await client.post("/api/recipes", json=bad, headers=h)
    assert r.status_code == 400 and r.json()["detail"]["code"] == "unit_mismatch"
    assert (await client.post("/api/recipes", json=body | {"name": "X блюдо"}, headers=auth_headers(PURCHASER_ID))).status_code == 403


async def test_recipe_photo_upload_and_download(client, db):
    h = auth_headers(COOK_ID)
    async with db() as s:
        s.add(Recipe(name="Фото-блюдо", base_yield=Decimal("1")))
        await s.commit()
    rid = next(r["id"] for r in (await client.get("/api/recipes", headers=h)).json() if r["name"] == "Фото-блюдо")
    jpeg = b"\xff\xd8\xff\xe0" + b"0" * 100
    r = await client.post(f"/api/recipes/{rid}/photo", files={"file": ("p.jpg", jpeg, "image/jpeg")}, headers=h)
    assert r.status_code == 200
    r = await client.get(f"/api/recipes/{rid}/photo", headers=h)
    assert r.status_code == 200 and r.content == jpeg
    assert next(x for x in (await client.get("/api/recipes", headers=h)).json() if x["id"] == rid)["has_photo"] is True


async def test_batch_with_actual_consumption_and_price_block(client, db):
    from sqlalchemy import select

    from api.models import BatchItem

    h = auth_headers(COOK_ID)
    async with db() as s:
        rice = (await s.execute(select(Stock).where(Stock.name == "Рис"))).scalar_one()
        carrot = (await s.execute(select(Stock).where(Stock.name == "Морковь"))).scalar_one()
        recipe = Recipe(name="Каша", base_yield=Decimal("10"))
        recipe.ingredients = [RecipeIngredient(product_id=rice.id, quantity=Decimal("1"), unit="kg")]
        s.add(recipe)
        await s.commit()
        rid, rice_id, carrot_id = recipe.id, rice.id, carrot.id

    # Повар потратил риса больше, чем по техкарте, и добавил морковь
    r = await client.post("/api/batch", json={"recipe_id": rid, "planned_yield": 10, "actual_yield": 12,
                                              "items": [{"product_id": rice_id, "quantity": 1.2},
                                                        {"product_id": carrot_id, "quantity": 0.5}]}, headers=h)
    assert r.status_code == 201
    assert r.json()["cost"] == 1.2 * 15000 + 0.5 * 6000
    stock = {p["name"]: p["quantity"] for p in (await client.get("/api/stock", headers=h)).json()}
    assert (stock["Рис"], stock["Морковь"]) == (8.8, 4.5)
    async with db() as s:
        lines = {ln.product_name: ln for ln in (await s.execute(select(BatchItem))).scalars().all()}
        assert lines["Рис"].planned_quantity == 1 and lines["Рис"].quantity == Decimal("1.2")
        assert lines["Морковь"].planned_quantity is None
    hist = (await client.get("/api/batch", headers=h)).json()[0]
    assert hist["cost_per_portion"] == 1750.0 and len(hist["items"]) == 2      # 21 000 ÷ 12 фактических порций

    # Товар без цены — партия блокируется
    new = (await client.post("/api/stock", json={"name": "Нахат", "unit": "kg"}, headers=h)).json()
    await client.post("/api/stock/inventory", json={"items": [{"product_id": new["id"], "quantity": 5}]}, headers=h)
    r = await client.post("/api/batch", json={"recipe_id": rid, "planned_yield": 1,
                                              "items": [{"product_id": new["id"], "quantity": 1}]}, headers=h)
    assert r.status_code == 400 and r.json()["detail"]["code"] == "no_price"


async def test_product_rename_and_category(client):
    h = auth_headers(PURCHASER_ID)
    rice = next(p for p in (await client.get("/api/stock", headers=h)).json() if p["name"] == "Рис")
    r = await client.patch(f"/api/stock/{rice['id']}", json={"name": "Рис девзира", "category": "grain"}, headers=h)
    assert r.status_code == 200 and (r.json()["name"], r.json()["category"]) == ("Рис девзира", "grain")
    r = await client.patch(f"/api/stock/{rice['id']}", json={"name": "морковь"}, headers=h)
    assert r.status_code == 409


# ------------------------------------------------------------------ Этап 2: раздача, остатки, списания

async def _make_batch(client, db, portions=10):
    """Техкарта «Каша» (1 кг риса на 10 порц.) и партия на N порций — 15 000 сум за 10 порций."""
    async with db() as s:
        rice = (await s.execute(Stock.__table__.select().where(Stock.name == "Рис"))).first()
        recipe = Recipe(name=f"Каша {portions}", base_yield=Decimal("10"), shelf_life_hours=24)
        recipe.ingredients = [RecipeIngredient(product_id=rice.id, quantity=Decimal("1"), unit="kg")]
        s.add(recipe)
        await s.commit()
        rid = recipe.id
    r = await client.post("/api/batch", json={"recipe_id": rid, "planned_yield": portions}, headers=auth_headers(COOK_ID))
    assert r.status_code == 201, r.text
    return r.json()["id"], rid


async def test_distribute_creates_deliveries_and_tracks_remaining(client, db):
    bid, _ = await _make_batch(client, db, 10)          # себестоимость порции = 1 500
    h = auth_headers(COOK_ID)
    before = (await client.get("/api/client/invoice", params={"client": "FOYDALI FOOD GROUP"}, headers=auth_headers(OWNER_ID))).json()
    r = await client.post(f"/api/batch/{bid}/distribute", json={"items": [{"client": "FOYDALI FOOD GROUP", "portions": 6}]}, headers=h)
    assert r.status_code == 201, r.text
    # Цена клиента — из его поставок: 3 000 000 / 100 = 30 000
    assert r.json()["deliveries"] == [{"client": "FOYDALI FOOD GROUP", "portions": 6, "amount": 180000.0}]
    assert (r.json()["batch"]["given"], r.json()["batch"]["remaining"]) == (6, 4)
    after = (await client.get("/api/client/invoice", params={"client": "FOYDALI FOOD GROUP"}, headers=auth_headers(OWNER_ID))).json()
    assert after["charged"] - before["charged"] == 180000
    async with db() as s:
        d = (await s.execute(ClientDelivery.__table__.select().where(ClientDelivery.batch_id == bid))).first()
        assert d.cost == Decimal("9000.00")             # 6 × 1 500
    r = await client.post(f"/api/batch/{bid}/distribute", json={"items": [{"client": "NO NAMELA", "portions": 5}]}, headers=h)
    assert r.status_code == 400 and r.json()["detail"]["code"] == "not_enough_portions"


async def test_leftovers_staff_and_waste_go_to_owner_stats(client, db):
    bid, _ = await _make_batch(client, db, 10)
    h = auth_headers(COOK_ID)
    r = await client.post(f"/api/batch/{bid}/leftover", json={"kind": "waste", "portions": 1}, headers=h)
    assert r.status_code == 400 and r.json()["detail"]["code"] == "reason_required"
    assert (await client.post(f"/api/batch/{bid}/leftover", json={"kind": "waste", "portions": 1, "reason": "spoiled"}, headers=h)).status_code == 201
    r = await client.post(f"/api/batch/{bid}/leftover", json={"kind": "staff", "portions": 2}, headers=h)
    assert (r.json()["batch"]["staff"], r.json()["batch"]["waste"], r.json()["batch"]["remaining"]) == (2, 1, 7)
    opened = (await client.get("/api/kitchen/open", headers=h)).json()
    assert any(o["id"] == bid and o["remaining"] == 7 and o["status"] == "fresh" for o in opened)
    s = (await client.get("/api/stats", headers=auth_headers(OWNER_ID))).json()
    assert (s["writeoffs"], s["staff_meals"]) == (1500.0, 3000.0)
    assert s["profit_after_writeoffs"] == s["profit"] - 4500          # валовая прибыль не изменилась


async def test_owner_sets_client_price_used_in_distribution(client, db):
    r = await client.put("/api/clients/price", json={"client": "NO NAMELA", "price": 25000}, headers=auth_headers(OWNER_ID))
    assert r.status_code == 200 and r.json()["price"] == 25000
    bid, _ = await _make_batch(client, db, 10)
    r = await client.post(f"/api/batch/{bid}/distribute", json={"items": [{"client": "NO NAMELA", "portions": 2}]}, headers=auth_headers(COOK_ID))
    assert r.json()["deliveries"][0]["amount"] == 50000
    assert (await client.put("/api/clients/price", json={"client": "NO NAMELA", "price": 1}, headers=auth_headers(COOK_ID))).status_code == 403


# ------------------------------------------------------------------ Этап 3: меню, закупки, PDF

async def test_week_menu_copy_and_shopping_list(client, db):
    import datetime as _dt

    _, rid = await _make_batch(client, db, 10)                       # на складе осталось 9 кг риса
    h = auth_headers(COOK_ID)
    monday = _dt.date.today() - _dt.timedelta(days=_dt.date.today().weekday()) + _dt.timedelta(days=7)   # следующая неделя
    r = await client.put("/api/menu/day", json={"date": monday.isoformat(), "items": [
        {"recipe_id": rid, "client": "NO NAMELA", "portions": 100},
        {"recipe_id": rid, "client": "FOYDALI FOOD GROUP", "portions": 20}]}, headers=h)
    assert r.status_code == 200 and r.json()["days"][0]["portions"] == 120
    shop = (await client.get("/api/menu/shopping", params={"week": monday.isoformat()}, headers=auth_headers(PURCHASER_ID))).json()
    rice = next(i for i in shop["items"] if i["product"] == "Рис")
    assert (rice["need"], rice["have"], rice["to_buy"], rice["estimate"]) == (12, 9, 3, 45000)   # 120 порц. × 0,1 кг
    nxt = monday + _dt.timedelta(days=7)
    r = await client.post("/api/menu/copy", json={"from_week": monday.isoformat(), "to_week": nxt.isoformat()}, headers=h)
    assert r.status_code == 200 and r.json()["days"][0]["portions"] == 120


async def test_recipe_and_menu_pdfs(client, db, monkeypatch):
    import datetime as _dt

    import api.notify as notify

    sent = []

    async def fake_send(chat_id, data, filename, caption):
        sent.append((chat_id, filename, data[:4]))

    monkeypatch.setattr(notify, "send_document", fake_send)
    _, rid = await _make_batch(client, db, 10)
    async with db() as s:
        from api.models import RecipePhoto
        import io
        from PIL import Image
        buf = io.BytesIO(); Image.new("RGB", (40, 30), (200, 100, 50)).save(buf, "JPEG")
        s.add(RecipePhoto(recipe_id=rid, content=buf.getvalue(), mime="image/jpeg"))
        await s.commit()
    h = auth_headers(COOK_ID)
    assert (await client.post("/api/recipes/pdf-send", headers=h)).status_code == 200
    today = _dt.date.today()
    await client.put("/api/menu/day", json={"date": today.isoformat(), "items": [{"recipe_id": rid, "portions": 10}]}, headers=h)
    assert (await client.post("/api/menu/pdf-send", params={"week": today.isoformat()}, headers=auth_headers(CLIENT_ID))).status_code == 200
    assert [(c, f[:5], d) for c, f, d in sent] == [(COOK_ID, "recip", b"%PDF"), (CLIENT_ID, "menu_", b"%PDF")]


async def test_today_screen_and_reminder_text(client, db):
    import datetime as _dt

    bid, rid = await _make_batch(client, db, 10)
    h = auth_headers(COOK_ID)
    await client.put("/api/menu/day", json={"date": _dt.date.today().isoformat(), "items": [
        {"recipe_id": rid, "client": "NO NAMELA", "portions": 8}]}, headers=h)
    today = (await client.get("/api/kitchen/today", headers=h)).json()
    assert today["plan"][0]["portions"] == 8 and today["plan"][0]["clients"] == {"NO NAMELA": 8}
    assert today["plan"][0]["cooked"] == 10 and today["batches"][0]["remaining"] == 10

    from bot.reminders import collect_leftovers, reminder_text
    left = await collect_leftovers()
    assert any(x["id"] == bid for x in left)
    text = reminder_text("ru", left)
    assert "Остались порции" in text and "10 порц." in text
