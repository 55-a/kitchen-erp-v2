"""
Единицы измерения и их пересчёт.

В рецептах ингредиенты часто записаны в граммах, а склад ведётся в килограммах —
без пересчёта партия списала бы в 1000 раз больше продукта.
"""
from __future__ import annotations

from decimal import Decimal

# Синонимы → каноническая единица
_ALIASES = {
    "kg": "kg", "кг": "kg", "килограмм": "kg", "kilo": "kg", "公斤": "kg", "千克": "kg",
    "g": "g", "гр": "g", "г": "g", "грамм": "g", "gr": "g", "克": "g",
    "l": "l", "л": "l", "литр": "l", "lt": "l", "升": "l",
    "ml": "ml", "мл": "ml", "миллилитр": "ml", "毫升": "ml",
    "pcs": "pcs", "шт": "pcs", "штук": "pcs", "pc": "pcs", "dona": "pcs", "个": "pcs",
}

# Каноническая единица → (базовая единица, множитель)
_BASE = {
    "kg": ("kg", Decimal("1")),
    "g": ("kg", Decimal("0.001")),
    "l": ("l", Decimal("1")),
    "ml": ("l", Decimal("0.001")),
    "pcs": ("pcs", Decimal("1")),
}


class UnitMismatch(ValueError):
    """Единицы несовместимы (например, кг и штуки)."""


def normalize_unit(unit: str | None) -> str:
    """Приводит единицу к канону: 'Кг.' → 'kg'. Неизвестную возвращает как есть (в нижнем регистре)."""
    if not unit:
        return "pcs"
    key = unit.strip().lower().rstrip(".")
    return _ALIASES.get(key, key)


def convert(quantity: Decimal, from_unit: str, to_unit: str) -> Decimal:
    """Пересчитывает количество между совместимыми единицами (г↔кг, мл↔л)."""
    src, dst = normalize_unit(from_unit), normalize_unit(to_unit)
    if src == dst:
        return Decimal(quantity)
    if src not in _BASE or dst not in _BASE:
        raise UnitMismatch(f"{from_unit} → {to_unit}")
    src_base, src_k = _BASE[src]
    dst_base, dst_k = _BASE[dst]
    if src_base != dst_base:
        raise UnitMismatch(f"{from_unit} → {to_unit}")
    return Decimal(quantity) * src_k / dst_k
