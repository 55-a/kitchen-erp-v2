"""
ORM-модели базы данных кухни.

Таблицы из ТЗ: clients, stock, recipes, recipe_ingredients, client_deliveries,
payments, purchases, batches.
Дополнительно:
- users          — роль и язык каждого пользователя Telegram (нужно для /start и /lang);
- receipt_drafts — распознанные чеки, ожидающие подтверждения в боте
                   (хранятся в БД, чтобы не теряться при перезапуске).
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

from sqlalchemy import (
    JSON,
    LargeBinary,
    BigInteger,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from api.database import Base

# Деньги в сумах (2 знака), количества — 3 знака (кг/л с граммами)
Money = Numeric(16, 2)
Qty = Numeric(14, 3)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    full_name: Mapped[str | None] = mapped_column(String(255))
    # owner | cook | purchaser | client | None (ещё не выбрал)
    role: Mapped[str | None] = mapped_column(String(20))
    language: Mapped[str] = mapped_column(String(5), default="ru")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Client(Base):
    __tablename__ = "clients"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    # Привязка к аккаунту Telegram (ставит владелец командой /link_client)
    telegram_id: Mapped[int | None] = mapped_column(BigInteger, unique=True, index=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Stock(Base):
    __tablename__ = "stock"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    unit: Mapped[str] = mapped_column(String(20), default="kg")
    quantity: Mapped[Decimal] = mapped_column(Qty, default=Decimal("0"))
    price_per_unit: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    # Категория сырья в справочнике: meat, veg, grain, dairy, oil, spice, bakery, pack, other
    category: Mapped[str | None] = mapped_column(String(30), default="other")
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Recipe(Base):
    __tablename__ = "recipes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    # Базовый выход закладки (в порциях), на который рассчитаны ингредиенты
    base_yield: Mapped[Decimal] = mapped_column(Qty, default=Decimal("1"))
    # Категория блюда (first, main, garnish, bakery, salad, other), срок годности готового блюда
    # и версия техкарты: каждое изменение состава увеличивает версию, старые партии хранят свой расход
    category: Mapped[str | None] = mapped_column(String(30), default="main")
    shelf_life_hours: Mapped[int | None] = mapped_column(Integer, default=24)
    version: Mapped[int | None] = mapped_column(Integer, default=1)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    ingredients: Mapped[list["RecipeIngredient"]] = relationship(
        back_populates="recipe", cascade="all, delete-orphan", lazy="selectin"
    )


class RecipeIngredient(Base):
    __tablename__ = "recipe_ingredients"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    recipe_id: Mapped[int] = mapped_column(ForeignKey("recipes.id", ondelete="CASCADE"), index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("stock.id", ondelete="RESTRICT"), index=True)
    quantity: Mapped[Decimal] = mapped_column(Qty)
    unit: Mapped[str] = mapped_column(String(20))

    recipe: Mapped[Recipe] = relationship(back_populates="ingredients")
    product: Mapped[Stock] = relationship(lazy="selectin")


class ClientDelivery(Base):
    __tablename__ = "client_deliveries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    client_name: Mapped[str] = mapped_column(String(255), index=True)
    dish_name: Mapped[str] = mapped_column(String(255))
    portions: Mapped[int] = mapped_column(Integer, default=0)
    amount: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))  # выручка
    cost: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))  # себестоимость
    date: Mapped[dt.date] = mapped_column(Date, index=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Payment(Base):
    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    client_name: Mapped[str] = mapped_column(String(255), index=True)
    amount: Mapped[Decimal] = mapped_column(Money)
    payment_type: Mapped[str] = mapped_column(String(30), default="cash")
    date: Mapped[dt.date] = mapped_column(Date, index=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Purchase(Base):
    __tablename__ = "purchases"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    product_id: Mapped[int | None] = mapped_column(ForeignKey("stock.id", ondelete="SET NULL"))
    product_name: Mapped[str] = mapped_column(String(255))
    quantity: Mapped[Decimal] = mapped_column(Qty)
    unit: Mapped[str] = mapped_column(String(20))
    total_price: Mapped[Decimal] = mapped_column(Money)
    payment_method: Mapped[str] = mapped_column(String(30), default="cash")
    date: Mapped[dt.date] = mapped_column(Date, index=True)
    created_by: Mapped[int | None] = mapped_column(BigInteger)  # Telegram ID закупщика
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Batch(Base):
    __tablename__ = "batches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    recipe_id: Mapped[int] = mapped_column(ForeignKey("recipes.id", ondelete="RESTRICT"), index=True)
    planned_yield: Mapped[Decimal] = mapped_column(Qty)
    actual_yield: Mapped[Decimal | None] = mapped_column(Qty)
    # Себестоимость списанных продуктов на момент партии
    cost: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    date: Mapped[dt.date] = mapped_column(Date, index=True)
    recipe_version: Mapped[int | None] = mapped_column(Integer)
    created_by: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    recipe: Mapped[Recipe] = relationship(lazy="selectin")


class ReceiptDraft(Base):
    __tablename__ = "receipt_drafts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, index=True)
    # Список позиций: [{name, quantity, unit, total_price, product_id, status}]
    items: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SalesPlan(Base):
    """
    План продаж на месяц, заданный владельцем вручную.
    client_name = "" — план по кухне в целом; иначе — по конкретному клиенту.
    Нет записи — план считается автоматически по средним продажам (api/plan.py).
    """

    __tablename__ = "sales_plans"
    __table_args__ = (UniqueConstraint("month", "client_name", name="uq_sales_plan_month_client"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    month: Mapped[dt.date] = mapped_column(Date, index=True)          # первое число месяца
    client_name: Mapped[str] = mapped_column(String(255), default="")
    portions: Mapped[int] = mapped_column(Integer, default=0)
    revenue: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    created_by: Mapped[int | None] = mapped_column(BigInteger)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class RecipePhoto(Base):
    """Фото блюда для карточки техкарты и PDF рецептов (сжатое на телефоне до ~1280px)."""

    __tablename__ = "recipe_photos"

    recipe_id: Mapped[int] = mapped_column(ForeignKey("recipes.id", ondelete="CASCADE"), primary_key=True)
    content: Mapped[bytes] = mapped_column(LargeBinary)
    mime: Mapped[str] = mapped_column(String(50), default="image/jpeg")
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class StockAdjustment(Base):
    """
    Изменение остатка не закупкой и не партией: инвентаризация (пересчёт склада).
    Хранится было/стало/разница — расхождения видны владельцу.
    """

    __tablename__ = "stock_adjustments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("stock.id", ondelete="CASCADE"), index=True)
    product_name: Mapped[str] = mapped_column(String(255))
    old_quantity: Mapped[Decimal] = mapped_column(Qty)
    new_quantity: Mapped[Decimal] = mapped_column(Qty)
    delta: Mapped[Decimal] = mapped_column(Qty)
    unit: Mapped[str] = mapped_column(String(20))
    price_per_unit: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    reason: Mapped[str] = mapped_column(String(30), default="inventory")
    date: Mapped[dt.date] = mapped_column(Date, index=True)
    created_by: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class BatchItem(Base):
    """Фактический расход продукта в партии (повар может поправить количество против техкарты)."""

    __tablename__ = "batch_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("batches.id", ondelete="CASCADE"), index=True)
    product_id: Mapped[int | None] = mapped_column(ForeignKey("stock.id", ondelete="SET NULL"))
    product_name: Mapped[str] = mapped_column(String(255))
    quantity: Mapped[Decimal] = mapped_column(Qty)            # в единицах склада
    unit: Mapped[str] = mapped_column(String(20))
    planned_quantity: Mapped[Decimal | None] = mapped_column(Qty)   # по техкарте
    cost: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
