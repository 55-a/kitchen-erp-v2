"""
Распознавание чеков через Google Gemini (REST API, без тяжёлого SDK).

- Ответ модели принудительно в JSON (responseMimeType = application/json).
- Встроенный ограничитель частоты: не больше GEMINI_RPM запросов в минуту
  (бесплатный лимит — 15), чтобы не получать бан от Google.
- Сеть недоступна / Google вернул 5xx или 429 → GeminiUnavailable (API отдаёт 503).
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import time
from collections import deque
from decimal import Decimal, InvalidOperation

import httpx

from api.config import get_settings

log = logging.getLogger(__name__)

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

PROMPT = (
    "Распознай чек и верни JSON массив: [{name, quantity, unit, total_price}]. "
    "name — название товара как в чеке (строка), quantity — количество (число), "
    "unit — единица измерения: kg, g, l, ml или pcs, total_price — итоговая сумма "
    "по строке в сумах (число, без пробелов и валюты). Не включай итоговую сумму чека, "
    "скидки и НДС отдельными строками. Если чек не читается — верни []."
)

MAX_IMAGE_BYTES = 10 * 1024 * 1024  # 10 МБ


class GeminiUnavailable(Exception):
    """Gemini недоступен или не настроен → HTTP 503."""


class GeminiRateLimited(Exception):
    """Превышен локальный лимит запросов в минуту → HTTP 429."""


class ReceiptParseError(Exception):
    """Модель вернула ответ, который не удалось разобрать → HTTP 502."""


class _RateLimiter:
    """Скользящее окно в 60 секунд (на один процесс)."""

    def __init__(self) -> None:
        self._calls: deque[float] = deque()
        self._lock = asyncio.Lock()

    async def acquire(self, limit: int) -> None:
        async with self._lock:
            now = time.monotonic()
            while self._calls and now - self._calls[0] > 60:
                self._calls.popleft()
            if len(self._calls) >= limit:
                raise GeminiRateLimited()
            self._calls.append(now)


_limiter = _RateLimiter()


def _to_decimal(value) -> Decimal | None:
    """'12 500,50' → Decimal('12500.50'). Мусор → None."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    text = re.sub(r"[^\d,.\-]", "", str(value)).replace(",", ".")
    if text.count(".") > 1:  # "12.500.00" — точки как разделители тысяч
        head, _, tail = text.rpartition(".")
        text = head.replace(".", "") + "." + tail
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def normalize_items(raw) -> list[dict]:
    """Приводит ответ модели к списку {name, quantity, unit, total_price} и отбрасывает мусор."""
    if isinstance(raw, dict):
        # Иногда модель оборачивает массив: {"items": [...]}
        raw = raw.get("items") or next((v for v in raw.values() if isinstance(v, list)), [])
    if not isinstance(raw, list):
        raise ReceiptParseError("response is not a list")

    items: list[dict] = []
    for row in raw:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "").strip()
        quantity = _to_decimal(row.get("quantity"))
        total = _to_decimal(row.get("total_price"))
        if not name or quantity is None or quantity <= 0 or total is None or total < 0:
            continue
        items.append(
            {
                "name": name[:255],
                "quantity": float(quantity),
                "unit": str(row.get("unit") or "pcs").strip().lower()[:20],
                "total_price": float(total),
            }
        )
    return items


async def parse_receipt(image_bytes: bytes, mime_type: str = "image/jpeg") -> list[dict]:
    """Отправляет фото чека в Gemini и возвращает нормализованные позиции."""
    settings = get_settings()
    if not settings.gemini_api_key:
        raise GeminiUnavailable("GEMINI_API_KEY is not set")
    if not image_bytes:
        raise ReceiptParseError("empty image")
    if len(image_bytes) > MAX_IMAGE_BYTES:
        raise ReceiptParseError("image too large")

    await _limiter.acquire(settings.gemini_rpm)

    payload = {
        "contents": [
            {
                "parts": [
                    {"text": PROMPT},
                    {"inline_data": {"mime_type": mime_type, "data": base64.b64encode(image_bytes).decode()}},
                ]
            }
        ],
        "generationConfig": {"responseMimeType": "application/json", "temperature": 0},
    }
    url = GEMINI_URL.format(model=settings.gemini_model)
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0)) as client:
            resp = await client.post(url, json=payload, headers={"x-goog-api-key": settings.gemini_api_key})
    except httpx.HTTPError as exc:
        log.warning("Gemini network error: %s", exc)
        raise GeminiUnavailable(str(exc)) from exc

    if resp.status_code == 429 or resp.status_code >= 500:
        log.warning("Gemini unavailable: %s %s", resp.status_code, resp.text[:300])
        raise GeminiUnavailable(f"HTTP {resp.status_code}")
    if resp.status_code != 200:
        # 400/403 — неверный ключ или запрос; для клиента это тоже "сервис недоступен"
        log.error("Gemini error: %s %s", resp.status_code, resp.text[:500])
        raise GeminiUnavailable(f"HTTP {resp.status_code}")

    try:
        data = resp.json()
        text = data["candidates"][0]["content"]["parts"][0]["text"]
    except (ValueError, KeyError, IndexError) as exc:
        raise ReceiptParseError("unexpected Gemini response") from exc

    # На всякий случай убираем ```json ... ``` если модель их добавила
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    try:
        return normalize_items(json.loads(text))
    except json.JSONDecodeError as exc:
        raise ReceiptParseError("invalid JSON from Gemini") from exc
