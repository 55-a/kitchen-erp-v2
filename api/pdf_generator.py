"""
Генерация PDF акта сверки через fpdf2.

Шрифты:
- DejaVuSans (+Bold) — латиница, кириллица, узбекский;
- NotoSansCJK — китайский (подключается как запасной шрифт, fallback).
В Docker-образе они ставятся пакетами fonts-dejavu-core и fonts-noto-cjk.
Можно положить свои файлы в каталог FONT_DIR.
"""
from __future__ import annotations

import datetime as dt
import logging
from decimal import Decimal
from pathlib import Path

from fpdf import FPDF
from fpdf.enums import XPos, YPos

from api.config import get_settings
from bot.translations import t

log = logging.getLogger(__name__)

_DEJAVU_REGULAR = ["DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"]
_DEJAVU_BOLD = ["DejaVuSans-Bold.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"]
_CJK = [
    "NotoSansSC-Regular.ttf",
    "NotoSansSC-Regular.otf",
    "NotoSansCJKsc-Regular.otf",
    "NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
]


def _find_font(candidates: list[str]) -> Path | None:
    """Ищет шрифт сначала в FONT_DIR и ./fonts, затем по абсолютным системным путям."""
    font_dir = get_settings().font_dir
    search_dirs = [Path(font_dir)] if font_dir else []
    search_dirs.append(Path(__file__).resolve().parent.parent / "fonts")
    for name in candidates:
        path = Path(name)
        if path.is_absolute():
            if path.exists():
                return path
            continue
        for d in search_dirs:
            if (d / name).exists():
                return d / name
    return None


def fmt_money(value) -> str:
    """15800000.5 → '15 800 000,50' (копейки показываем, только если они есть)."""
    v = Decimal(str(value or 0)).quantize(Decimal("0.01"))
    whole, frac = f"{v:.2f}".split(".")
    sign = "-" if whole.startswith("-") else ""
    whole = whole.lstrip("-")
    grouped = " ".join([whole[max(i - 3, 0):i] for i in range(len(whole), 0, -3)][::-1])
    return f"{sign}{grouped}" + (f",{frac}" if frac != "00" else "")


class ActPDF(FPDF):
    """PDF с подключёнными юникод-шрифтами и нумерацией страниц."""

    def __init__(self, lang: str):
        super().__init__(orientation="P", unit="mm", format="A4")
        self.lang = lang
        self.set_margins(15, 15, 15)
        self.set_auto_page_break(auto=True, margin=18)
        self._setup_fonts()

    def _setup_fonts(self) -> None:
        regular = _find_font(_DEJAVU_REGULAR)
        if regular is None:
            raise RuntimeError("Шрифт DejaVuSans.ttf не найден: установите fonts-dejavu-core или задайте FONT_DIR")
        self.add_font("dejavu", "", str(regular))
        bold = _find_font(_DEJAVU_BOLD)
        self.add_font("dejavu", "B", str(bold or regular))

        cjk = _find_font(_CJK)
        if cjk is None:
            log.warning("Шрифт NotoSansCJK не найден — китайские символы не будут отображаться")
            return
        try:
            if cjk.suffix.lower() == ".ttc":
                # .ttc — коллекция шрифтов; индекс 2 в NotoSansCJK-Regular.ttc — упрощённый китайский (SC)
                try:
                    self.add_font("notocjk", "", str(cjk), collection_font_number=2)
                except TypeError:  # старый fpdf2 без поддержки коллекций
                    self.add_font("notocjk", "", str(cjk))
            else:
                self.add_font("notocjk", "", str(cjk))
            # Символы, которых нет в DejaVu (иероглифы), берутся из Noto CJK
            self.set_fallback_fonts(["notocjk"], exact_match=False)
        except Exception as exc:  # шрифт битый/не поддерживается — PDF всё равно создаём
            log.warning("Не удалось подключить CJK-шрифт %s: %s", cjk, exc)

    def footer(self) -> None:
        self.set_y(-12)
        self.set_font("dejavu", "", 8)
        self.set_text_color(130)
        self.cell(0, 6, f"{t(self.lang, 'pdf_company')} · {self.page_no()}/{{nb}}", align="C")
        self.set_text_color(0)


def build_act_pdf(
    *,
    client_name: str,
    deliveries: list,
    invoice: dict,
    lang: str,
    date_from: dt.date | None = None,
    date_to: dt.date | None = None,
) -> bytes:
    """
    Строит акт сверки.
    deliveries — объекты ClientDelivery (date, dish_name, portions, amount),
    invoice    — результат services.client_invoice (charged, paid, debt).
    """
    pdf = ActPDF(lang)
    pdf.alias_nb_pages()
    pdf.add_page()

    # ---- Шапка
    pdf.set_font("dejavu", "B", 16)
    pdf.cell(0, 9, t(lang, "pdf_company"), new_x=XPos.LMARGIN, new_y=YPos.NEXT, align="C")
    pdf.set_font("dejavu", "B", 12)
    pdf.multi_cell(0, 7, t(lang, "pdf_title"), new_x=XPos.LMARGIN, new_y=YPos.NEXT, align="C")
    pdf.ln(4)

    # ---- Период
    dates = sorted(d.date for d in deliveries)
    start = date_from or (dates[0] if dates else None)
    end = date_to or (dates[-1] if dates else None)
    period = f"{start:%d.%m.%Y} — {end:%d.%m.%Y}" if start and end else t(lang, "pdf_all_time")

    pdf.set_font("dejavu", "", 10)
    for label, value in (
        (t(lang, "pdf_client"), client_name),
        (t(lang, "pdf_period"), period),
        (t(lang, "pdf_generated"), dt.date.today().strftime("%d.%m.%Y")),
    ):
        pdf.set_font("dejavu", "B", 10)
        pdf.cell(40, 6, f"{label}:")
        pdf.set_font("dejavu", "", 10)
        pdf.cell(0, 6, str(value), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(4)

    # ---- Таблица поставок (заголовок повторяется на каждой странице)
    period_total = Decimal("0")
    period_portions = 0
    if deliveries:
        pdf.set_font("dejavu", "", 9)
        with pdf.table(
            col_widths=(28, 92, 25, 35),
            text_align=("CENTER", "LEFT", "RIGHT", "RIGHT"),
            line_height=6,
            first_row_as_headings=True,
        ) as table:
            head = table.row()
            for key in ("pdf_date", "pdf_dish", "pdf_portions", "pdf_amount"):
                head.cell(t(lang, key))
            # Хронологический порядок в акте
            for d in sorted(deliveries, key=lambda x: (x.date, x.id or 0)):
                row = table.row()
                row.cell(d.date.strftime("%d.%m.%Y"))
                row.cell(d.dish_name)
                row.cell(str(d.portions))
                row.cell(fmt_money(d.amount))
                period_total += Decimal(d.amount or 0)
                period_portions += int(d.portions or 0)
            total_row = table.row()
            total_row.cell(t(lang, "pdf_total"), colspan=2)
            total_row.cell(str(period_portions))
            total_row.cell(fmt_money(period_total))
    else:
        pdf.set_font("dejavu", "", 10)
        pdf.cell(0, 8, t(lang, "pdf_no_data"), new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    # ---- Итоги взаиморасчётов (накопительно)
    pdf.ln(6)
    for key, value, bold in (
        ("pdf_total", invoice.get("charged", 0), False),
        ("pdf_paid", invoice.get("paid", 0), False),
        ("pdf_debt", invoice.get("debt", 0), True),
    ):
        pdf.set_font("dejavu", "B" if bold else "", 11)
        pdf.cell(130, 7, t(lang, key) + ":", align="R")
        pdf.cell(50, 7, fmt_money(value), align="R", new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    # ---- Подписи сторон
    if pdf.get_y() > pdf.h - 60:
        pdf.add_page()
    pdf.ln(16)
    y = pdf.get_y()
    col_w = (pdf.w - pdf.l_margin - pdf.r_margin - 10) / 2
    for i, (title, name) in enumerate(
        ((t(lang, "pdf_supplier"), t(lang, "pdf_company")), (t(lang, "pdf_client"), client_name))
    ):
        x = pdf.l_margin + i * (col_w + 10)
        pdf.set_xy(x, y)
        pdf.set_font("dejavu", "B", 10)
        pdf.cell(col_w, 6, title, new_x=XPos.LEFT, new_y=YPos.NEXT)
        pdf.set_font("dejavu", "", 10)
        pdf.cell(col_w, 6, name, new_x=XPos.LEFT, new_y=YPos.NEXT)
        pdf.ln(12)
        pdf.set_x(x)
        pdf.cell(col_w, 0, "", border="T", new_x=XPos.LEFT, new_y=YPos.NEXT)
        pdf.set_x(x)
        pdf.set_font("dejavu", "", 8)
        pdf.cell(col_w, 5, t(lang, "pdf_signature"))

    return bytes(pdf.output())
