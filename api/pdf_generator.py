"""
Генерация PDF акта сверки через fpdf2 — в современном стиле:
градиентная шапка, карточки итогов, «вайб-строка» о балансе, полосатая таблица.

Шрифты:
- DejaVuSans (+Bold) — латиница, кириллица, узбекский, символы ✦ ✓ ★ ♥;
- WenQuanYi Micro Hei — китайский (подключается как запасной шрифт, fallback).
В Docker-образе они ставятся пакетами fonts-dejavu-core и fonts-wqy-microhei.
Можно положить свои файлы в каталог FONT_DIR.
"""
from __future__ import annotations

import datetime as dt
import logging
from decimal import Decimal
from pathlib import Path

from fpdf import FPDF
from fpdf.enums import XPos, YPos
from fpdf.fonts import FontFace

from api.config import get_settings
from bot.translations import t

log = logging.getLogger(__name__)

_DEJAVU_REGULAR = ["DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"]
_DEJAVU_BOLD = ["DejaVuSans-Bold.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"]
# Китайский шрифт ОБЯЗАТЕЛЬНО с TrueType-контурами (glyf). Noto CJK (OTF/TTC с CFF-контурами)
# fpdf2 2.8 встраивает неверно — в PDF вместо иероглифов видны цифры. Поэтому WenQuanYi Micro Hei.
_CJK = [
    "wqy-microhei.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "NotoSansSC-Regular.ttf",            # TTF-версия с Google Fonts тоже подходит
]

# ---------- Палитра акта ----------
INK = (15, 23, 42)          # почти чёрный текст
MUTED = (100, 116, 139)     # серый для подписей
VIOLET = (99, 102, 241)
PURPLE = (139, 92, 246)
PINK = (236, 72, 153)
MINT = (16, 185, 129)
RED = (225, 29, 72)
LILAC_BG = (238, 242, 255)
MINT_BG = (236, 253, 245)
PINK_BG = (253, 242, 248)
ZEBRA = (245, 243, 255)
WHITE = (255, 255, 255)


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


def _mix(c1: tuple, c2: tuple, k: float) -> tuple:
    """Цвет между c1 и c2 (k от 0 до 1)."""
    return tuple(round(a + (b - a) * k) for a, b in zip(c1, c2))


def gradient_band(pdf: FPDF, x: float, y: float, w: float, h: float, c1: tuple, c2: tuple, steps: int = 140) -> None:
    """Горизонтальный градиент тонкими полосками — отображается одинаково в любом просмотрщике PDF."""
    step = w / steps
    for i in range(steps):
        pdf.set_fill_color(*_mix(c1, c2, i / max(steps - 1, 1)))
        pdf.rect(x + i * step, y, step + 0.25, h, style="F")


class ActPDF(FPDF):
    """PDF с юникод-шрифтами, тонкой градиентной полосой на страницах 2+ и нумерацией."""

    def __init__(self, lang: str):
        super().__init__(orientation="P", unit="mm", format="A4")
        self.lang = lang
        self.set_margins(14, 14, 14)
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
            log.warning("Китайский шрифт (wqy-microhei) не найден — иероглифы не будут отображаться")
            return
        try:
            # В wqy-microhei.ttc первый шрифт коллекции — Micro Hei, он и нужен
            self.add_font("cjk", "", str(cjk))
            # Символы, которых нет в DejaVu (иероглифы), берутся из китайского шрифта
            self.set_fallback_fonts(["cjk"], exact_match=False)
        except Exception as exc:  # шрифт битый/не поддерживается — PDF всё равно создаём
            log.warning("Не удалось подключить CJK-шрифт %s: %s", cjk, exc)

    def header(self) -> None:
        # На второй и следующих страницах — тонкая градиентная полоска вместо большой шапки
        if self.page_no() > 1:
            gradient_band(self, 0, 0, self.w, 4, VIOLET, PINK)
            self.set_y(12)

    def footer(self) -> None:
        self.set_y(-13)
        self.set_font("dejavu", "", 8)
        self.set_text_color(*MUTED)
        self.cell(0, 6, f"{t(self.lang, 'pdf_company')} ✦ {t(self.lang, 'pdf_footer')} · {self.page_no()}/{{nb}}", align="C")
        self.set_text_color(*INK)


def _pill(pdf: FPDF, x: float, y: float, text: str, *, fill: tuple, color: tuple,
          size: float = 8.5, opacity: float = 1.0) -> float:
    """Округлая «таблетка» с текстом; возвращает её ширину."""
    pdf.set_font("dejavu", "B", size)
    w = pdf.get_string_width(text) + 8
    with pdf.local_context(fill_opacity=opacity):
        pdf.set_fill_color(*fill)
        pdf.rect(x, y, w, 7, style="F", round_corners=True, corner_radius=3.5)
    pdf.set_text_color(*color)
    pdf.set_xy(x, y)
    pdf.cell(w, 7, text, align="C")
    return w


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
    content_w = pdf.w - pdf.l_margin - pdf.r_margin

    # ---------- Период ----------
    dates = sorted(d.date for d in deliveries)
    start = date_from or (dates[0] if dates else None)
    end = date_to or (dates[-1] if dates else None)
    period = f"{start:%d.%m.%Y} — {end:%d.%m.%Y}" if start and end else t(lang, "pdf_all_time")

    # ---------- Шапка: градиент фиолетовый → розовый + декоративные круги ----------
    band_h = 62
    gradient_band(pdf, 0, 0, pdf.w, band_h, VIOLET, PINK)
    with pdf.local_context(fill_opacity=0.12):
        pdf.set_fill_color(*WHITE)
        pdf.circle(x=pdf.w - 18, y=6, radius=34, style="F")
        pdf.circle(x=pdf.w - 58, y=band_h - 4, radius=14, style="F")

    _pill(pdf, pdf.l_margin, 12, f"{t(lang, 'pdf_company').upper()} ✦", fill=WHITE, color=WHITE, opacity=0.22)

    pdf.set_text_color(*WHITE)
    pdf.set_font("dejavu", "B", 30)
    pdf.set_xy(pdf.l_margin, 22)
    pdf.cell(content_w, 13, t(lang, "pdf_hero"))
    pdf.set_font("dejavu", "", 12)
    pdf.set_xy(pdf.l_margin, 36)
    pdf.cell(content_w, 7, f"{t(lang, 'pdf_company')}  ×  {client_name}")

    x = pdf.l_margin
    x += _pill(pdf, x, 47, f"{t(lang, 'pdf_period')}: {period}", fill=WHITE, color=WHITE, opacity=0.22) + 3
    _pill(pdf, x, 47, f"{t(lang, 'pdf_generated')}: {dt.date.today():%d.%m.%Y}", fill=WHITE, color=WHITE, opacity=0.22)

    # ---------- Три карточки: начислено / оплачено / долг или переплата ----------
    charged = Decimal(str(invoice.get("charged", 0) or 0))
    paid = Decimal(str(invoice.get("paid", 0) or 0))
    debt = Decimal(str(invoice.get("debt", 0) or 0))
    if debt > 0:
        third = (t(lang, "pdf_debt"), debt, PINK_BG, RED)
    elif debt < 0:
        third = (t(lang, "pdf_overpaid"), -debt, MINT_BG, MINT)
    else:
        third = (t(lang, "pdf_debt"), Decimal("0"), MINT_BG, MINT)
    cards = (
        (t(lang, "pdf_total"), charged, LILAC_BG, VIOLET),
        (t(lang, "pdf_paid"), paid, MINT_BG, MINT),
        third,
    )
    gap = 5
    card_w = (content_w - gap * 2) / 3
    card_y = band_h + 8
    for i, (label, value, bg, accent) in enumerate(cards):
        cx = pdf.l_margin + i * (card_w + gap)
        pdf.set_fill_color(*bg)
        pdf.rect(cx, card_y, card_w, 31, style="F", round_corners=True, corner_radius=5)
        pdf.set_fill_color(*accent)                       # цветная точка-акцент
        pdf.rect(cx + 6, card_y + 6.5, 3, 3, style="F", round_corners=True, corner_radius=1.5)
        pdf.set_text_color(*MUTED)
        pdf.set_font("dejavu", "B", 7.5)
        pdf.set_xy(cx + 11, card_y + 5)
        pdf.cell(card_w - 14, 5, label.upper())
        text = fmt_money(value)
        pdf.set_text_color(*accent)
        pdf.set_font("dejavu", "B", 15 if len(text) < 12 else 13)
        pdf.set_xy(cx + 6, card_y + 13)
        pdf.cell(card_w - 12, 9, text)
        pdf.set_text_color(*MUTED)
        pdf.set_font("dejavu", "", 8)
        pdf.set_xy(cx + 6, card_y + 22)
        pdf.cell(card_w - 12, 5, t(lang, "pdf_currency"))

    # ---------- «Вайб-строка»: человеческим языком, что с балансом ----------
    vibe_y = card_y + 31 + 6
    if debt > 0:
        vibe = t(lang, "pdf_vibe_debt", amount=fmt_money(debt))
    elif debt < 0:
        vibe = t(lang, "pdf_vibe_over", amount=fmt_money(-debt))
    else:
        vibe = t(lang, "pdf_vibe_zero")
    pdf.set_fill_color(*INK)
    pdf.rect(pdf.l_margin, vibe_y, content_w, 13, style="F", round_corners=True, corner_radius=6.5)
    gradient_band(pdf, pdf.l_margin + 5, vibe_y + 4.5, 4, 4, PURPLE, PINK, steps=4)
    pdf.set_text_color(*WHITE)
    # Длинные языки (узбекский) не помещаются в 10.5pt — уменьшаем шрифт, пока строка не влезет
    size = 10.5
    pdf.set_font("dejavu", "B", size)
    while pdf.get_string_width(vibe) > content_w - 18 and size > 7:
        size -= 0.5
        pdf.set_font("dejavu", "B", size)
    pdf.set_xy(pdf.l_margin + 12, vibe_y)
    pdf.cell(content_w - 16, 13, vibe)

    # ---------- Таблица поставок ----------
    pdf.set_y(vibe_y + 13 + 8)
    portions_total = sum(int(d.portions or 0) for d in deliveries)
    pdf.set_text_color(*INK)
    pdf.set_font("dejavu", "B", 14)
    pdf.cell(70, 8, t(lang, "pdf_deliveries_title"))
    pdf.set_font("dejavu", "", 9)
    pdf.set_text_color(*MUTED)
    pdf.cell(content_w - 70, 8, t(lang, "pdf_stats_line", count=len(deliveries), portions=portions_total),
             align="R", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(2)

    period_total = Decimal("0")
    if deliveries:
        pdf.set_text_color(*INK)
        pdf.set_font("dejavu", "", 9)
        # Чётные строки берут текущий цвет заливки — сбрасываем его на белый,
        # иначе туда «протекает» последний цвет градиента (розовый)
        pdf.set_fill_color(*WHITE)
        with pdf.table(
            col_widths=(26, 96, 24, 36),
            text_align=("LEFT", "LEFT", "RIGHT", "RIGHT"),
            line_height=7.5,
            padding=(0.8, 3),
            borders_layout="NONE",
            headings_style=FontFace(emphasis="BOLD", color=WHITE, fill_color=INK),
            cell_fill_color=ZEBRA,
            cell_fill_mode="ROWS",
            first_row_as_headings=True,
        ) as table:
            head = table.row()
            for key in ("pdf_date", "pdf_dish", "pdf_portions", "pdf_amount"):
                head.cell(t(lang, key))
            for d in sorted(deliveries, key=lambda x: (x.date, x.id or 0)):   # хронологический порядок
                row = table.row()
                row.cell(d.date.strftime("%d.%m.%Y"))
                row.cell(d.dish_name)
                row.cell(str(d.portions))
                row.cell(fmt_money(d.amount))
                period_total += Decimal(d.amount or 0)
            total_style = FontFace(emphasis="BOLD", color=VIOLET, fill_color=LILAC_BG)
            total = table.row()
            total.cell(f"{t(lang, 'pdf_total_period')} ✦", colspan=2, style=total_style)
            total.cell(str(portions_total), style=total_style)
            total.cell(fmt_money(period_total), style=total_style)
    else:
        y = pdf.get_y()
        pdf.set_fill_color(*LILAC_BG)
        pdf.rect(pdf.l_margin, y, content_w, 16, style="F", round_corners=True, corner_radius=5)
        pdf.set_xy(pdf.l_margin, y)
        pdf.set_font("dejavu", "", 10)
        pdf.set_text_color(*MUTED)
        pdf.cell(content_w, 16, t(lang, "pdf_no_data"), align="C", new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    # ---------- Подписи сторон: две карточки с линией подписи ----------
    if pdf.get_y() > pdf.h - 62:
        pdf.add_page()
    pdf.ln(10)
    y = pdf.get_y()
    col_w = (content_w - 8) / 2
    for i, (title, name) in enumerate(
        ((t(lang, "pdf_supplier"), t(lang, "pdf_company")), (t(lang, "pdf_client"), client_name))
    ):
        sx = pdf.l_margin + i * (col_w + 8)
        pdf.set_draw_color(*PURPLE)
        pdf.set_line_width(0.4)
        pdf.rect(sx, y, col_w, 36, style="D", round_corners=True, corner_radius=5)
        pdf.set_xy(sx + 6, y + 5)
        pdf.set_font("dejavu", "B", 7.5)
        pdf.set_text_color(*MUTED)
        pdf.cell(col_w - 12, 5, title.upper())
        pdf.set_xy(sx + 6, y + 11)
        pdf.set_font("dejavu", "B", 11)
        pdf.set_text_color(*INK)
        pdf.cell(col_w - 12, 6, name)
        pdf.set_draw_color(203, 213, 225)
        pdf.set_line_width(0.3)
        pdf.line(sx + 6, y + 28, sx + col_w - 6, y + 28)
        pdf.set_xy(sx + 6, y + 29)
        pdf.set_font("dejavu", "", 7.5)
        pdf.set_text_color(*MUTED)
        pdf.cell(col_w - 12, 5, t(lang, "pdf_signature"))
    pdf.set_text_color(*INK)

    return bytes(pdf.output())
