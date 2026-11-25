"""GET /api/stats — статистика для владельца."""
from __future__ import annotations

import csv
import datetime as dt
import io
import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth import AuthUser, require_roles
from api.config import get_settings
from api.database import get_session
from api.services import compute_stats

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["stats"])


@router.get("/stats")
async def get_stats(
    date_from: dt.date | None = Query(None, description="Начало периода, YYYY-MM-DD"),
    date_to: dt.date | None = Query(None, description="Конец периода, YYYY-MM-DD"),
    user: AuthUser = Depends(require_roles("owner")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Выручка, себестоимость, прибыль, маржа (%), разбивка по месяцам и клиентам."""
    if date_from and date_to and date_from > date_to:
        raise HTTPException(400, "date_from must be before date_to")
    return await compute_stats(session, date_from, date_to)


class ExportRequest(BaseModel):
    date_from: dt.date | None = None
    date_to: dt.date | None = None


def build_stats_csv(stats: dict) -> bytes:
    """
    Отчёт для Excel: разделитель «;» и UTF-8 с BOM — так русская версия Excel
    открывает файл двойным щелчком без «кракозябр».
    """
    def n(v):
        """Суммы — целым числом, дробные (маржа) — с запятой, как привык русский Excel."""
        v = float(v or 0)
        return str(int(round(v))) if v.is_integer() or abs(v) >= 1000 else f"{v:.1f}".replace(".", ",")

    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    period = stats.get("period") or {}
    w.writerow(["Kuxnya NVGR — отчёт", f"{period.get('from') or 'всё время'} — {period.get('to') or dt.date.today().isoformat()}"])
    w.writerow([])
    w.writerow(["Показатель", "Значение"])
    for label, key in (
        ("Выручка, сум", "revenue"), ("Себестоимость, сум", "cost"), ("Прибыль, сум", "profit"),
        ("Маржа, %", "margin"), ("Порций", "portions"), ("Закупки, сум", "purchases_total"),
        ("Склад, сум", "stock_value"),
    ):
        w.writerow([label, n(stats.get(key, 0))])
    w.writerow([])
    w.writerow(["Месяц", "Выручка, сум", "Себестоимость, сум", "Прибыль, сум"])
    for m in stats.get("by_month", []):
        w.writerow([m["month"], n(m["revenue"]), n(m["cost"]), n(m["profit"])])
    w.writerow([])
    w.writerow(["Клиент", "Порций", "Начислено, сум", "Оплачено, сум", "Остаток, сум"])
    for c in stats.get("by_client", []):
        w.writerow([c["client"], c["portions"], n(c["charged_total"]), n(c["paid_total"]), n(c["debt"])])
    return ("\ufeff" + buf.getvalue()).encode("utf-8")


@router.post("/stats/export-send")
async def post_stats_export(
    body: ExportRequest,
    user: AuthUser = Depends(require_roles("owner")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Кнопка «Экспорт»: бот присылает отчёт CSV в личный чат (скачивание из WebView на телефоне ненадёжно)."""
    from aiogram import Bot
    from aiogram.types import BufferedInputFile

    if body.date_from and body.date_to and body.date_from > body.date_to:
        raise HTTPException(400, "date_from must be before date_to")
    stats = await compute_stats(session, body.date_from, body.date_to)
    data = build_stats_csv(stats)

    bot = Bot(token=get_settings().bot_token)
    try:
        await bot.send_document(
            chat_id=user.telegram_id,
            document=BufferedInputFile(data, filename=f"kuxnya_report_{dt.date.today():%Y%m%d}.csv"),
            caption="📊 Kuxnya NVGR — отчёт",
        )
    except Exception as exc:
        log.exception("Failed to send stats export: %s", exc)
        raise HTTPException(502, "Could not send the document via Telegram")
    finally:
        await bot.session.close()
    return {"ok": True}
