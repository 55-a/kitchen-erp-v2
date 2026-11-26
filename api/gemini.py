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

# Запасные модели, если основная выведена из обращения или перегружена
FALLBACK_MODELS = ("gemini-3.5-flash", "gemini-flash-latest", "gemini-3.8-flash", "gemini-3.1-flash-lite")


def _model_chain(configured: str) -> list[str]:
    """GEMINI_MODEL (через запятую) + запасные, без повторов."""
    chain: list[str] = []
    for name in [*configured.split(","), *FALLBACK_MODELS]:
        name = name.strip().removeprefix("models/")
        if name and name not in chain:
            chain.append(name)
    return chain

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
    text = await _generate(payload, purpose="receipt")

    # На всякий случай убираем ```json ... ``` если модель их добавила
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    try:
        return normalize_items(json.loads(text))
    except json.JSONDecodeError as exc:
        raise ReceiptParseError("invalid JSON from Gemini") from exc


async def _generate(
    payload: dict,
    purpose: str,
    *,
    per_model_timeout: float = 40.0,
    deadline: float | None = None,
    models: list[str] | None = None,
) -> str:
    """
    Отправляет запрос в Gemini и возвращает текст ответа.
    Google регулярно выводит старые модели из обращения (404) и отвечает 503 при перегрузке
    новых. Поэтому пробуем по очереди: GEMINI_MODEL (можно несколько через запятую),
    затем запасные модели. Неверный ключ (400/401/403) — сразу ошибка, перебор не поможет.

    per_model_timeout — сколько ждать одну модель; deadline — общий лимит в секундах на весь
    перебор (чат не должен «висеть» минуту: пользователь закроет приложение раньше).
    """
    settings = get_settings()
    resp = None
    last_error = "no models"
    started = time.monotonic()
    async with httpx.AsyncClient() as client:
        for model in models or _model_chain(settings.gemini_model):
            timeout = per_model_timeout
            if deadline is not None:
                left = deadline - (time.monotonic() - started)
                if left < 3:                       # времени на ещё одну модель не осталось
                    last_error = "deadline exceeded"
                    break
                timeout = min(timeout, left)
            try:
                resp = await client.post(
                    GEMINI_URL.format(model=model), json=payload,
                    headers={"x-goog-api-key": settings.gemini_api_key},
                    timeout=httpx.Timeout(timeout, connect=min(10.0, timeout)),
                )
            except httpx.HTTPError as exc:
                # У ReadTimeout пустой текст — пишем тип ошибки, чтобы в логах было видно, что случилось
                log.warning("Gemini %s network error (%.0fs): %s %s", model, timeout, type(exc).__name__, exc)
                last_error, resp = type(exc).__name__, None
                continue
            if resp.status_code == 200:
                log.info("Gemini %s: %s ok", model, purpose)
                break
            if resp.status_code in {404, 429} or resp.status_code >= 500:
                log.warning("Gemini %s unavailable: %s %s", model, resp.status_code, resp.text[:300])
                last_error, resp = f"HTTP {resp.status_code}", None
                continue
            log.error("Gemini %s error: %s %s", model, resp.status_code, resp.text[:500])
            raise GeminiUnavailable(f"HTTP {resp.status_code}")
    if resp is None:
        raise GeminiUnavailable(last_error)
    try:
        data = resp.json()
        parts = data["candidates"][0]["content"]["parts"]
        # У «думающих» моделей бывает несколько частей; служебные (thought) пропускаем
        return "".join(p.get("text", "") for p in parts if not p.get("thought"))
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise ReceiptParseError(f"unexpected Gemini response ({purpose})") from exc


# ---------------------------------------------------------------- ИИ-помощник закупщика

CHAT_MAX_TURNS = 16          # сколько последних сообщений диалога отправляем модели
CHAT_MAX_CHARS = 2000        # максимальная длина одного сообщения пользователя
CHAT_MODEL_TIMEOUT = 15.0    # сколько ждать одну модель в чате, секунд
CHAT_DEADLINE = 50.0         # общий лимит на ответ чата, секунд (приложение ждёт 70)
CHAT_FAST_MODEL = "gemini-3.1-flash-lite"   # быстрая запасная модель — вторая в очереди


async def chat(history: list[dict], system: str) -> str:
    """
    Свободный диалог с ИИ-помощником.
    history — [{role: "user"|"assistant", text: "..."}], последнее сообщение — от пользователя.
    system  — инструкция с контекстом (склад, цены, последние закупки).
    """
    settings = get_settings()
    if not settings.gemini_api_key:
        raise GeminiUnavailable("GEMINI_API_KEY is not set")
    await _limiter.acquire(settings.gemini_rpm)

    contents = [
        {"role": "model" if m["role"] == "assistant" else "user", "parts": [{"text": m["text"][:CHAT_MAX_CHARS]}]}
        for m in history[-CHAT_MAX_TURNS:]
    ]
    payload = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": contents,
        "generationConfig": {"temperature": 0.6, "maxOutputTokens": 8192},   # с запасом: «думающие» модели тратят часть лимита на размышления
    }
    # Для чата важна скорость: быстрая «lite»-модель идёт второй, каждую ждём до 15 с,
    # а весь перебор — не дольше CHAT_DEADLINE (иначе пользователь видит бесконечные «…»)
    chain = _model_chain(settings.gemini_model)
    if CHAT_FAST_MODEL in chain:
        chain.remove(CHAT_FAST_MODEL)
    chain.insert(min(1, len(chain)), CHAT_FAST_MODEL)
    text = (await _generate(payload, purpose="chat", per_model_timeout=CHAT_MODEL_TIMEOUT,
                            deadline=CHAT_DEADLINE, models=chain)).strip()
    if not text:
        raise ReceiptParseError("empty chat answer")
    return text
