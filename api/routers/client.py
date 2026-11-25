"""
Кабинет клиента:
    GET  /api/client/deliveries?tg_id=...   — поставки
    GET  /api/client/invoice?tg_id=...      — начислено / оплачено / долг
    GET  /api/client/act-pdf?tg_id=...      — PDF акт сверки (initData или одноразовый token)
    POST /api/client/act-link               — одноразовая ссылка на PDF (для tg.downloadFile)
    POST /api/client/act-send               — бот присылает PDF в чат
Для владельца:
    GET  /api/clients                       — список клиентов
    POST /api/client/payment                — внести оплату
    POST /api/client/delivery               — внести поставку

Безопасность: клиент видит ТОЛЬКО свои данные. Параметр tg_id из ТЗ сохранён,
но если он не совпадает с проверенным initData — 403 (защита от подбора чужого ID).
Владелец может смотреть любого клиента (по tg_id или по client=Название).
"""
from __future__ import annotations

import datetime as dt
import logging
from decimal import Decimal
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth import (
    AuthUser,
    get_current_user,
    require_roles,
    sign_download_token,
    verify_download_token,
)
from api.config import get_settings
from api.database import get_session
from api.models import Client, ClientDelivery, Payment, User
from api.pdf_generator import build_act_pdf
from api.services import client_deliveries, client_invoice, money

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["client"])


async def resolve_client(
    session: AsyncSession, user: AuthUser, tg_id: int | None, client: str | None
) -> Client:
    """Определяет, данные какого клиента можно отдать текущему пользователю."""
    if user.is_owner:
        if client:
            row = (await session.execute(select(Client).where(Client.name == client))).scalar_one_or_none()
        elif tg_id:
            row = (await session.execute(select(Client).where(Client.telegram_id == tg_id))).scalar_one_or_none()
        else:
            raise HTTPException(400, "Specify tg_id or client")
        if row is None:
            raise HTTPException(404, "Client not found")
        return row

    if user.role != "client":
        raise HTTPException(403, "Forbidden: role is not allowed")
    if tg_id is not None and tg_id != user.telegram_id:
        raise HTTPException(403, "Forbidden: tg_id does not match initData")
    row = (await session.execute(select(Client).where(Client.telegram_id == user.telegram_id))).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "Your Telegram account is not linked to a client")
    return row


async def client_language(session: AsyncSession, client: Client) -> str:
    """Язык PDF — язык пользователя Telegram, привязанного к клиенту."""
    if client.telegram_id:
        lang = (
            await session.execute(select(User.language).where(User.telegram_id == client.telegram_id))
        ).scalar_one_or_none()
        if lang:
            return lang
    return get_settings().default_language


@router.get("/client/deliveries")
async def get_deliveries(
    tg_id: int | None = Query(None),
    client: str | None = Query(None, description="Только для владельца"),
    date_from: dt.date | None = None,
    date_to: dt.date | None = None,
    user: AuthUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    c = await resolve_client(session, user, tg_id, client)
    rows = await client_deliveries(session, c.name, date_from, date_to)
    return {
        "client": c.name,
        "total_amount": money(sum((Decimal(r.amount or 0) for r in rows), Decimal(0))),
        "total_portions": sum(int(r.portions or 0) for r in rows),
        "items": [
            {
                "id": r.id,
                "date": r.date.isoformat(),
                "dish_name": r.dish_name,
                "portions": r.portions,
                "amount": money(r.amount),
            }
            for r in rows
        ],
    }


@router.get("/client/invoice")
async def get_invoice(
    tg_id: int | None = Query(None),
    client: str | None = Query(None),
    user: AuthUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    c = await resolve_client(session, user, tg_id, client)
    return await client_invoice(session, c.name)


async def _render_act(
    session: AsyncSession, client_name: str, lang: str, date_from: dt.date | None, date_to: dt.date | None
) -> bytes:
    rows = await client_deliveries(session, client_name, date_from, date_to)
    invoice = await client_invoice(session, client_name)
    return build_act_pdf(
        client_name=client_name, deliveries=rows, invoice=invoice, lang=lang, date_from=date_from, date_to=date_to
    )


def _pdf_response(pdf: bytes, client_name: str) -> Response:
    filename = f"act_{client_name}_{dt.date.today():%Y%m%d}.pdf"
    # RFC 5987: кириллица в имени файла
    disposition = f"attachment; filename=\"act.pdf\"; filename*=UTF-8''{quote(filename)}"
    return Response(content=pdf, media_type="application/pdf", headers={"Content-Disposition": disposition})


@router.get("/client/act-pdf")
async def get_act_pdf(
    request: Request,
    tg_id: int | None = Query(None),
    client: str | None = Query(None),
    date_from: dt.date | None = None,
    date_to: dt.date | None = None,
    token: str | None = Query(None, description="Одноразовый токен из /api/client/act-link"),
    session: AsyncSession = Depends(get_session),
) -> Response:
    if token:
        data = verify_download_token(token)
        client_name, lang = data["c"], data["l"]
        date_from = dt.date.fromisoformat(data["f"]) if data.get("f") else None
        date_to = dt.date.fromisoformat(data["t"]) if data.get("t") else None
    else:
        # Без токена middleware уже проверил initData — достаём пользователя обычным способом
        user = await get_current_user(request, session)
        c = await resolve_client(session, user, tg_id, client)
        client_name, lang = c.name, await client_language(session, c)
    pdf = await _render_act(session, client_name, lang, date_from, date_to)
    return _pdf_response(pdf, client_name)


class ActRequest(BaseModel):
    tg_id: int | None = None
    client: str | None = None
    date_from: dt.date | None = None
    date_to: dt.date | None = None


@router.post("/client/act-link")
async def post_act_link(
    body: ActRequest,
    request: Request,
    user: AuthUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """
    Telegram WebApp не умеет скачивать файл с заголовками авторизации, поэтому
    выдаём ссылку с токеном на 5 минут. Её открывает tg.downloadFile() / tg.openLink().
    """
    c = await resolve_client(session, user, body.tg_id, body.client)
    lang = await client_language(session, c)
    token = sign_download_token(
        user.telegram_id,
        c.name,
        lang,
        extra={
            "f": body.date_from.isoformat() if body.date_from else None,
            "t": body.date_to.isoformat() if body.date_to else None,
        },
    )
    # Telegram скачивает файл только по абсолютной HTTPS-ссылке
    base = get_settings().webapp_url or str(request.base_url).rstrip("/")
    return {"url": f"{base}/api/client/act-pdf?token={quote(token)}", "file_name": f"act_{dt.date.today():%Y%m%d}.pdf"}


@router.post("/client/act-send")
async def post_act_send(
    body: ActRequest,
    user: AuthUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Самый надёжный способ на телефоне: бот присылает PDF в личный чат."""
    from aiogram import Bot
    from aiogram.types import BufferedInputFile

    from bot.translations import t

    c = await resolve_client(session, user, body.tg_id, body.client)
    lang = await client_language(session, c)
    pdf = await _render_act(session, c.name, lang, body.date_from, body.date_to)

    bot = Bot(token=get_settings().bot_token)
    try:
        await bot.send_document(
            chat_id=user.telegram_id,
            document=BufferedInputFile(pdf, filename=f"act_{dt.date.today():%Y%m%d}.pdf"),
            caption=t(user.language, "act_caption", client=c.name),
        )
    except Exception as exc:
        log.exception("Failed to send act PDF: %s", exc)
        raise HTTPException(502, "Could not send the document via Telegram")
    finally:
        await bot.session.close()
    return {"ok": True}


# ------------------------------------------------------------------ Владелец

@router.get("/clients")
async def list_clients(
    user: AuthUser = Depends(require_roles("owner")),
    session: AsyncSession = Depends(get_session),
) -> list[dict]:
    rows = (await session.execute(select(Client).order_by(Client.name))).scalars().all()
    return [{"id": c.id, "name": c.name, "telegram_id": c.telegram_id, "linked": bool(c.telegram_id)} for c in rows]


class PaymentIn(BaseModel):
    client: str = Field(min_length=1, max_length=255)
    amount: Decimal = Field(gt=0)
    payment_type: str = Field("cash", pattern="^(cash|card|transfer)$")
    date: dt.date = Field(default_factory=dt.date.today)


class DeliveryIn(BaseModel):
    client: str = Field(min_length=1, max_length=255)
    dish_name: str = Field(min_length=1, max_length=255)
    portions: int = Field(gt=0)
    amount: Decimal = Field(ge=0)
    cost: Decimal = Field(Decimal("0"), ge=0)
    date: dt.date = Field(default_factory=dt.date.today)


async def _ensure_client(session: AsyncSession, name: str) -> Client:
    row = (await session.execute(select(Client).where(Client.name == name))).scalar_one_or_none()
    if row is None:
        raise HTTPException(400, "Client not found")
    return row


@router.post("/client/payment", status_code=201)
async def post_payment(
    body: PaymentIn,
    user: AuthUser = Depends(require_roles("owner")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    c = await _ensure_client(session, body.client)
    p = Payment(client_name=c.name, amount=body.amount, payment_type=body.payment_type, date=body.date)
    session.add(p)
    await session.commit()
    return {"id": p.id, **(await client_invoice(session, c.name))}


@router.post("/client/delivery", status_code=201)
async def post_delivery(
    body: DeliveryIn,
    user: AuthUser = Depends(require_roles("owner")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    c = await _ensure_client(session, body.client)
    d = ClientDelivery(
        client_name=c.name, dish_name=body.dish_name, portions=body.portions,
        amount=body.amount, cost=body.cost, date=body.date,
    )
    session.add(d)
    await session.commit()
    return {"id": d.id, **(await client_invoice(session, c.name))}
