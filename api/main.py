"""
Точка входа FastAPI.

Запуск: uvicorn api.main:app --host 0.0.0.0 --port 8000

- /api/*        — REST API (каждый запрос проверяется TelegramAuthMiddleware)
- /app/{role}   — HTML Mini App (owner, cook, purchaser, client)
- /static/*     — CSS/JS
- Если RUN_BOT_IN_API=true, бот запускается в этом же процессе (polling) —
  удобно для одного сервиса на Railway.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from api.auth import TelegramAuthMiddleware
from api.config import get_settings
from api.database import init_db
from api.routers import ai, batch, client, me, plan, purchase, recipes, stats, stock
from api.services import ServiceError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("kitchen-erp")

BASE_DIR = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"
MINI_APPS = {"owner", "cook", "purchaser", "client"}


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Создаём таблицы и (опционально) запускаем бота в фоне."""
    settings = get_settings()
    await init_db()

    bot_task: asyncio.Task | None = None
    if settings.run_bot_in_api and settings.bot_token:
        from bot.main import run_polling

        bot_task = asyncio.create_task(run_polling(), name="telegram-bot")
        log.info("Telegram bot started inside API process")
    elif not settings.bot_token:
        log.warning("BOT_TOKEN is empty: initData cannot be validated, all /api requests will get 403")

    try:
        yield
    finally:
        if bot_task:
            bot_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await bot_task


app = FastAPI(title="Kuxnya NVGR ERP", version="1.0.0", lifespan=lifespan)
app.add_middleware(TelegramAuthMiddleware)

for r in (stats, stock, recipes, batch, purchase, ai, client, me, plan):
    app.include_router(r.router)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.exception_handler(ServiceError)
async def service_error_handler(request: Request, exc: ServiceError) -> JSONResponse:
    """Ошибки бизнес-правил (товар не найден, не хватает продуктов и т.п.)."""
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.as_detail()})


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """Ошибки валидации — 400 с понятным списком полей (вместо 422 по умолчанию)."""
    errors = [
        {"field": ".".join(str(p) for p in e.get("loc", [])[1:]), "message": e.get("msg")} for e in exc.errors()
    ]
    return JSONResponse(status_code=400, content={"detail": {"code": "validation_error", "errors": errors}})


@app.get("/api/health")
async def health() -> dict:
    return {"status": "ok"}


@app.get("/")
async def root() -> RedirectResponse:
    return RedirectResponse("/app")


@app.get("/app")
async def mini_app_entry() -> FileResponse:
    """Экран «Войти»: кнопка меню бота открывает его, дальше — экран своей роли или выбор роли."""
    return FileResponse(TEMPLATES_DIR / "index.html", media_type="text/html", headers={"Cache-Control": "no-cache"})


@app.get("/app/{role}")
async def mini_app(role: str) -> FileResponse:
    """Отдаёт HTML Mini App. Сама страница публичная — данные защищены на уровне /api."""
    if role not in MINI_APPS:
        raise HTTPException(404, "Not found")
    return FileResponse(TEMPLATES_DIR / f"{role}.html", media_type="text/html", headers={"Cache-Control": "no-cache"})
