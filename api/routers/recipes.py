"""
Спецификации (техкарты) блюд.

    GET  /api/recipes               — техкарты с ингредиентами, остатками и себестоимостью
    POST /api/recipes               — новая техкарта (повар, владелец)
    PUT  /api/recipes/{id}          — изменить; если поменялся состав — версия +1
                                      (старые партии хранят свой фактический расход в batch_items)
    POST /api/recipes/{id}/photo    — фото блюда (сжимается на телефоне до ~1280px)
    GET  /api/recipes/{id}/photo    — фото блюда
"""
from __future__ import annotations

from decimal import Decimal

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth import AuthUser, require_roles
from api.database import get_session
from api.models import Recipe, RecipeIngredient, RecipePhoto, Stock
from api.services import ServiceError, money, qty
from api.units import UnitMismatch, convert, normalize_unit

router = APIRouter(prefix="/api", tags=["recipes"])

RECIPE_CATEGORIES = ("first", "main", "garnish", "bakery", "salad", "other")
PHOTO_TYPES = {"image/jpeg", "image/png", "image/webp"}
MAX_PHOTO_BYTES = 3 * 1024 * 1024


def recipe_to_dict(r: Recipe, has_photo: bool) -> dict:
    """Для каждого ингредиента — количество на базовый выход, остаток и стоимость."""
    ingredients = []
    cost_per_base = Decimal("0")
    for ing in r.ingredients:
        p = ing.product
        try:
            in_stock_unit = convert(Decimal(ing.quantity), ing.unit, p.unit)
            cost = in_stock_unit * Decimal(p.price_per_unit or 0)
            cost_per_base += cost
        except UnitMismatch:
            in_stock_unit, cost = None, None
        ingredients.append(
            {
                "product_id": p.id,
                "product": p.name,
                "quantity": qty(ing.quantity),
                "unit": ing.unit,
                "quantity_in_stock_unit": qty(in_stock_unit) if in_stock_unit is not None else None,
                "stock_unit": p.unit,
                "stock_quantity": qty(p.quantity),
                "price_per_unit": money(p.price_per_unit),
                "cost": money(cost) if cost is not None else None,
            }
        )
    base_yield = Decimal(r.base_yield or 1)
    return {
        "id": r.id,
        "name": r.name,
        "category": r.category or "main",
        "shelf_life_hours": r.shelf_life_hours or 24,
        "version": r.version or 1,
        "has_photo": has_photo,
        "base_yield": qty(base_yield),
        "cost_per_base": money(cost_per_base),
        "cost_per_portion": money(cost_per_base / base_yield) if base_yield else 0.0,
        "ingredients": ingredients,
    }


async def _photo_ids(session: AsyncSession) -> set[int]:
    return set((await session.execute(select(RecipePhoto.recipe_id))).scalars().all())


@router.get("/recipes")
async def get_recipes(
    user: AuthUser = Depends(require_roles("cook")),
    session: AsyncSession = Depends(get_session),
) -> list[dict]:
    recipes = (await session.execute(select(Recipe).order_by(Recipe.name))).scalars().all()
    photos = await _photo_ids(session)
    return [recipe_to_dict(r, r.id in photos) for r in recipes]


class IngredientIn(BaseModel):
    product_id: int
    quantity: Decimal = Field(gt=0)
    unit: str = Field(min_length=1, max_length=20)


class RecipeIn(BaseModel):
    name: str = Field(min_length=2, max_length=255)
    base_yield: Decimal = Field(gt=0, le=10000, description="На сколько порций рассчитан состав")
    category: str = Field("main", pattern="^(" + "|".join(RECIPE_CATEGORIES) + ")$")
    shelf_life_hours: int = Field(24, ge=1, le=720)
    ingredients: list[IngredientIn] = Field(min_length=1, max_length=60)


async def _build_ingredients(session: AsyncSession, items: list[IngredientIn]) -> list[RecipeIngredient]:
    """Проверяет, что сырьё есть в справочнике и единицы совместимы со складом (г↔кг, мл↔л)."""
    ids = {i.product_id for i in items}
    products = {p.id: p for p in (await session.execute(select(Stock).where(Stock.id.in_(ids)))).scalars().all()}
    result = []
    for i in items:
        p = products.get(i.product_id)
        if p is None:
            raise ServiceError(400, "product_not_found", "Товар не найден в справочнике")
        unit = normalize_unit(i.unit)
        try:
            convert(Decimal("1"), unit, p.unit)
        except UnitMismatch:
            raise ServiceError(400, "unit_mismatch", f"«{p.name}»: {unit} несовместимо со складской единицей {p.unit}")
        result.append(RecipeIngredient(product_id=p.id, quantity=i.quantity, unit=unit))
    return result


def _composition(ings) -> list[tuple]:
    return sorted((int(i.product_id), Decimal(i.quantity).normalize(), normalize_unit(i.unit)) for i in ings)


async def _check_name(session: AsyncSession, name: str, exclude_id: int | None = None) -> str:
    name = " ".join(name.split())
    # casefold в Python: lower() в SQLite не понимает кириллицу
    rows = (await session.execute(select(Recipe.id, Recipe.name))).all()
    if any(rid != exclude_id and " ".join(n.split()).casefold() == name.casefold() for rid, n in rows):
        raise ServiceError(409, "recipe_exists", "Техкарта с таким названием уже есть")
    return name


@router.post("/recipes", status_code=201)
async def create_recipe(
    body: RecipeIn,
    user: AuthUser = Depends(require_roles("cook")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    name = await _check_name(session, body.name)
    recipe = Recipe(name=name, base_yield=body.base_yield, category=body.category,
                    shelf_life_hours=body.shelf_life_hours, version=1)
    recipe.ingredients = await _build_ingredients(session, body.ingredients)
    session.add(recipe)
    await session.commit()
    recipe = await session.get(Recipe, recipe.id, populate_existing=True)
    return recipe_to_dict(recipe, False)


@router.put("/recipes/{recipe_id}")
async def update_recipe(
    recipe_id: int,
    body: RecipeIn,
    user: AuthUser = Depends(require_roles("cook")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    recipe = await session.get(Recipe, recipe_id)
    if recipe is None:
        raise HTTPException(404, "Recipe not found")
    recipe.name = await _check_name(session, body.name, exclude_id=recipe_id)
    new_ings = await _build_ingredients(session, body.ingredients)
    # Версия растёт, только если изменился состав или выход — так видно, по какой техкарте варили партию
    if _composition(new_ings) != _composition(recipe.ingredients) or Decimal(recipe.base_yield) != body.base_yield:
        recipe.version = (recipe.version or 1) + 1
    recipe.ingredients = new_ings
    recipe.base_yield = body.base_yield
    recipe.category = body.category
    recipe.shelf_life_hours = body.shelf_life_hours
    await session.commit()
    recipe = await session.get(Recipe, recipe_id, populate_existing=True)
    return recipe_to_dict(recipe, recipe_id in await _photo_ids(session))


@router.post("/recipes/{recipe_id}/photo")
async def upload_photo(
    recipe_id: int,
    file: UploadFile = File(...),
    user: AuthUser = Depends(require_roles("cook")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    if await session.get(Recipe, recipe_id) is None:
        raise HTTPException(404, "Recipe not found")
    mime = (file.content_type or "").lower()
    if mime not in PHOTO_TYPES:
        raise HTTPException(400, "Unsupported image type")
    data = await file.read(MAX_PHOTO_BYTES + 1)
    if not data or len(data) > MAX_PHOTO_BYTES:
        raise HTTPException(413, "Photo is too large (max 3 MB)")
    photo = await session.get(RecipePhoto, recipe_id)
    if photo is None:
        session.add(RecipePhoto(recipe_id=recipe_id, content=data, mime=mime))
    else:
        photo.content, photo.mime = data, mime
    await session.commit()
    return {"ok": True}


@router.get("/recipes/{recipe_id}/photo")
async def get_photo(
    recipe_id: int,
    user: AuthUser = Depends(require_roles("cook")),
    session: AsyncSession = Depends(get_session),
) -> Response:
    photo = await session.get(RecipePhoto, recipe_id)
    if photo is None:
        raise HTTPException(404, "No photo")
    return Response(content=photo.content, media_type=photo.mime, headers={"Cache-Control": "private, max-age=3600"})
