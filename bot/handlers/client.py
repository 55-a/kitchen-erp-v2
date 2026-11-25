"""
Команды клиента. Поставки/счёт/акт смотрятся в Mini App;
/act — прислать PDF акта сверки прямо в чат (на языке клиента).
"""
from __future__ import annotations

import datetime as dt

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, Message

from api.database import session_scope
from api.pdf_generator import build_act_pdf
from api.services import client_deliveries, client_invoice
from bot.translations import t
from bot.utils import effective_role, get_or_create_user, linked_client

router = Router(name="client")


@router.message(Command("act"))
async def cmd_act(message: Message) -> None:
    async with session_scope() as session:
        user = await get_or_create_user(session, message.from_user)
        lang = user.language
        if effective_role(user) != "client":
            await message.answer(t(lang, "role_denied"))
            return
        client = await linked_client(session, user.telegram_id)
        if client is None:
            await message.answer(t(lang, "client_not_linked", tg_id=user.telegram_id))
            return
        rows = await client_deliveries(session, client.name)
        invoice = await client_invoice(session, client.name)
        name = client.name

    pdf = build_act_pdf(client_name=name, deliveries=rows, invoice=invoice, lang=lang)
    await message.answer_document(
        BufferedInputFile(pdf, filename=f"act_{dt.date.today():%Y%m%d}.pdf"),
        caption=t(lang, "act_caption", client=name),
    )
