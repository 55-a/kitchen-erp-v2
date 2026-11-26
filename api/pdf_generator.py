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




def _who_owes(lang: str, balance: Decimal, client_name: str, date: dt.date, kind: str) -> str:
    """Фраза о долге простыми словами (kind: open — на начало периода, close — итог)."""
    company = t(lang, "pdf_company")
    d = f"{date:%d.%m.%Y}"
    if balance > 0:
        return t(lang, f"act_{kind}_debt", date=d, client=client_name, company=company, amount=fmt_money(balance))
    if balance < 0:
        return t(lang, f"act_{kind}_over", date=d, client=client_name, company=company, amount=fmt_money(-balance))
    return t(lang, f"act_{kind}_zero", date=d, client=client_name, company=company)


def build_act_pdf(*, statement: dict, lang: str) -> bytes:
    """
    Акт сверки «как в 1С», но простыми словами (без «дебет/кредит/сальдо»):
    шапка → долг на начало → все поставки и оплаты по датам с долгом после каждой →
    итоги за период → итоговая фраза «кто кому должен» → подписи сторон.
    statement — результат services.client_statement().
    """
    st = statement
    client_name = st["client"]
    company = t(lang, "pdf_company")
    pdf = ActPDF(lang)
    pdf.alias_nb_pages()
    pdf.add_page()
    w = pdf.w - pdf.l_margin - pdf.r_margin

    # ---------- Шапка: фирменная полоса, название, стороны, период ----------
    gradient_band(pdf, 0, 0, pdf.w, 5, VIOLET, PINK)
    pdf.set_y(14)
    pdf.set_text_color(*VIOLET)
    pdf.set_font("dejavu", "B", 9)
    pdf.cell(w / 2, 5, f"{company.upper()} ✦")
    pdf.set_text_color(*MUTED)
    pdf.set_font("dejavu", "", 9)
    pdf.cell(w / 2, 5, t(lang, "act_made", date=f"{dt.date.today():%d.%m.%Y}"), align="R",
             new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    pdf.ln(3)
    pdf.set_text_color(*INK)
    pdf.set_font("dejavu", "B", 22)
    pdf.cell(w, 11, t(lang, "act_title"), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_font("dejavu", "", 11.5)
    pdf.multi_cell(w, 6.5, t(lang, "act_between", company=company, client=client_name),
                   new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_text_color(*MUTED)
    pdf.set_font("dejavu", "", 10)
    pdf.cell(w, 6, t(lang, "act_period", date_from=f"{st['date_from']:%d.%m.%Y}", date_to=f"{st['date_to']:%d.%m.%Y}"),
             new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(2)
    pdf.set_font("dejavu", "", 9.5)
    pdf.multi_cell(w, 5.2, t(lang, "act_intro", company=company, client=client_name),
                   new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    # ---------- Долг на начало периода ----------
    pdf.ln(4)
    y = pdf.get_y()
    pdf.set_fill_color(*LILAC_BG)
    pdf.rect(pdf.l_margin, y, w, 11, style="F", round_corners=True, corner_radius=3)
    pdf.set_xy(pdf.l_margin + 5, y)
    pdf.set_text_color(*INK)
    pdf.set_font("dejavu", "B", 10)
    pdf.cell(w - 10, 11, "① " + _who_owes(lang, st["opening"], client_name, st["date_from"], "open"))
    pdf.set_y(y + 11 + 5)

    # ---------- Таблица: каждая поставка и оплата + долг после неё ----------
    if st["rows"]:
        pdf.set_font("dejavu", "", 8.5)
        pdf.set_text_color(*INK)
        pdf.set_fill_color(*WHITE)               # чётные строки без «протекания» цвета
        with pdf.table(
            col_widths=(8, 25, 66, 26, 26, 31),          # сумма = ширине страницы (182 мм), дата в одну строку
            text_align=("CENTER", "LEFT", "LEFT", "RIGHT", "RIGHT", "RIGHT"),
            line_height=6.2,
            padding=(0.8, 1.6),
            borders_layout="HORIZONTAL_LINES",
            headings_style=FontFace(emphasis="BOLD", color=WHITE, fill_color=INK),
            cell_fill_color=ZEBRA,
            cell_fill_mode="ROWS",
            first_row_as_headings=True,
        ) as table:
            head = table.row()
            for key in ("act_col_no", "act_col_date", "act_col_what", "act_col_delivered", "act_col_paid", "act_col_balance"):
                head.cell(t(lang, key))
            pay_style = FontFace(color=MINT)
            for i, r in enumerate(st["rows"], start=1):
                row = table.row()
                row.cell(str(i))
                row.cell(f"{r['date']:%d.%m.%Y}")
                if r["kind"] == "delivery":
                    row.cell(t(lang, "act_row_delivery", dish=r["dish"], portions=r["portions"]))
                    row.cell(fmt_money(r["delivered"]))
                    row.cell("")
                else:
                    row.cell(t(lang, "act_row_payment", type=t(lang, f"pay_{r['payment_type']}")), style=pay_style)
                    row.cell("")
                    row.cell(fmt_money(r["paid"]), style=pay_style)
                row.cell(fmt_money(r["balance"]))
            total_style = FontFace(emphasis="BOLD", color=VIOLET, fill_color=LILAC_BG)
            total = table.row()
            total.cell(t(lang, "act_totals", portions=st["portions"]), colspan=3, style=total_style)
            total.cell(fmt_money(st["delivered"]), style=total_style)
            total.cell(fmt_money(st["paid"]), style=total_style)
            total.cell(fmt_money(st["closing"]), style=total_style)
    else:
        pdf.set_font("dejavu", "", 10)
        pdf.set_text_color(*MUTED)
        pdf.cell(w, 12, t(lang, "act_no_ops"), align="C", new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    # ---------- Итог: кто кому должен ----------
    if pdf.get_y() > pdf.h - 95:
        pdf.add_page()
    pdf.ln(6)
    y = pdf.get_y()
    closing = st["closing"]
    accent = RED if closing > 0 else MINT
    bg = PINK_BG if closing > 0 else MINT_BG
    pdf.set_fill_color(*bg)
    pdf.rect(pdf.l_margin, y, w, 18, style="F", round_corners=True, corner_radius=4)
    pdf.set_fill_color(*accent)
    pdf.rect(pdf.l_margin, y, 2.5, 18, style="F")
    pdf.set_xy(pdf.l_margin + 7, y + 1)
    pdf.set_text_color(*accent)
    pdf.set_font("dejavu", "B", 12)
    text = "② " + _who_owes(lang, closing, client_name, st["date_to"], "close")
    size = 12
    while pdf.get_string_width(text) > w - 12 and size > 8:
        size -= 0.5
        pdf.set_font("dejavu", "B", size)
    pdf.cell(w - 12, 16, text)
    pdf.set_y(y + 18 + 4)

    # Как читать акт — для тех, кто не бухгалтер
    pdf.set_text_color(*MUTED)
    pdf.set_font("dejavu", "", 8.5)
    pdf.multi_cell(w, 4.6, t(lang, "act_howto"), new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    # ---------- Подписи сторон ----------
    if pdf.get_y() > pdf.h - 62:
        pdf.add_page()
    pdf.ln(8)
    y = pdf.get_y()
    col_w = (w - 10) / 2
    for i, name in enumerate((company, client_name)):
        sx = pdf.l_margin + i * (col_w + 10)
        pdf.set_xy(sx, y)
        pdf.set_text_color(*INK)
        pdf.set_font("dejavu", "B", 10)
        pdf.cell(col_w, 6, t(lang, "act_sign_from", name=name), new_x=XPos.LEFT, new_y=YPos.NEXT)
        pdf.set_font("dejavu", "", 8.5)
        pdf.set_text_color(*MUTED)
        for key in ("act_sign_role", "act_sign_name", "act_sign_signature"):
            pdf.ln(4)
            pdf.set_x(sx)
            label = t(lang, key)
            pdf.cell(24, 6, label)
            pdf.set_draw_color(203, 213, 225)
            pdf.line(sx + 24, pdf.get_y() + 5, sx + col_w, pdf.get_y() + 5)
            pdf.ln(2)
        pdf.ln(4)
        pdf.set_x(sx)
        pdf.cell(col_w, 6, t(lang, "act_sign_stamp"))
    pdf.set_text_color(*INK)

    return bytes(pdf.output())


# ================================================================ Книга техкарт и меню недели

def _image(pdf: FPDF, data: bytes | None, x: float, y: float, w: float, h: float, label: str) -> None:
    """Фото блюда в рамке; без фото — цветная плашка с первыми буквами названия."""
    import io

    if data:
        try:
            with pdf.local_context():
                pdf.image(io.BytesIO(data), x=x, y=y, w=w, h=h, keep_aspect_ratio=False)
            return
        except Exception as exc:  # битое фото не должно ломать весь PDF
            log.warning("Bad recipe photo: %s", exc)
    gradient_band(pdf, x, y, w, h, (224, 231, 255), (252, 231, 243), steps=40)
    pdf.set_text_color(*VIOLET)
    pdf.set_font("dejavu", "B", 16)
    pdf.set_xy(x, y)
    words = [w for w in label.replace("(", " ").replace(")", " ").split() if w[:1].isalpha()]
    letters = "".join(word[:1] for word in words[:2]).upper() or "•"
    pdf.cell(w, h, letters, align="C")


def _num(v) -> str:
    """1.500 → «1,5», 0.0333 → «0,033»: до 3 знаков, без лишних нулей, запятая как в акте."""
    d = Decimal(str(v)).quantize(Decimal("0.001")).normalize()
    return f"{d:f}".replace(".", ",")


def build_recipes_pdf(recipes: list, photos: dict[int, bytes], lang: str) -> bytes:
    """Все техкарты: фото, категория, выход, срок годности, себестоимость порции, состав на выход и на порцию."""
    from api.units import UnitMismatch, convert

    pdf = ActPDF(lang)
    pdf.alias_nb_pages()
    pdf.add_page()
    w = pdf.w - pdf.l_margin - pdf.r_margin
    gradient_band(pdf, 0, 0, pdf.w, 5, VIOLET, PINK)
    pdf.set_y(14)
    pdf.set_text_color(*INK)
    pdf.set_font("dejavu", "B", 22)
    pdf.cell(w, 11, t(lang, "pdf_recipes_title"), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_font("dejavu", "", 10)
    pdf.set_text_color(*MUTED)
    pdf.cell(w, 6, f"{t(lang, 'pdf_company')} · {dt.date.today():%d.%m.%Y} · {len(recipes)}", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(4)

    for r in recipes:
        rows = []
        cost_base = Decimal("0")
        for ing in r.ingredients:
            p = ing.product
            try:
                cost_base += convert(Decimal(ing.quantity), ing.unit, p.unit) * Decimal(p.price_per_unit or 0)
            except UnitMismatch:
                pass
            per_portion = Decimal(ing.quantity) / Decimal(r.base_yield or 1)
            rows.append((p.name, f"{_num(ing.quantity)} {ing.unit}", f"{_num(per_portion)} {ing.unit}"))
        block_h = 52 + 6.2 * max(len(rows), 1)
        if pdf.get_y() + min(block_h, 120) > pdf.h - 20:
            pdf.add_page()
        y = pdf.get_y()
        # Фото + заголовок
        _image(pdf, photos.get(r.id), pdf.l_margin, y, 52, 39, r.name)
        x = pdf.l_margin + 57
        pdf.set_xy(x, y)
        pdf.set_text_color(*INK)
        pdf.set_font("dejavu", "B", 14)
        pdf.multi_cell(w - 57, 7, r.name, new_x=XPos.LEFT, new_y=YPos.NEXT, align="L")
        pdf.set_font("dejavu", "", 9)
        pdf.set_text_color(*MUTED)
        cpp = cost_base / Decimal(r.base_yield or 1)
        for line in (
            f"{t(lang, 'rcat_' + (r.category or 'main'))} · {t(lang, 'pdf_version', n=r.version or 1)}",
            t(lang, "pdf_yield_shelf", n=_num(r.base_yield), h=r.shelf_life_hours or 24),
            t(lang, "pdf_cost_portion", amount=fmt_money(cpp.quantize(Decimal('1')))),
        ):
            pdf.set_x(x)
            pdf.cell(w - 57, 5.5, line, new_x=XPos.LEFT, new_y=YPos.NEXT)
        pdf.set_y(max(pdf.get_y(), y + 39) + 3)
        # Состав
        if rows:
            pdf.set_font("dejavu", "", 8.8)
            pdf.set_text_color(*INK)
            pdf.set_fill_color(*WHITE)
            with pdf.table(
                col_widths=(100, 41, 41),
                text_align=("LEFT", "RIGHT", "RIGHT"),
                line_height=6,
                padding=(0.6, 2),
                borders_layout="HORIZONTAL_LINES",
                headings_style=FontFace(emphasis="BOLD", color=WHITE, fill_color=INK),
                cell_fill_color=ZEBRA,
                cell_fill_mode="ROWS",
            ) as table:
                head = table.row()
                head.cell(t(lang, "pdf_dish_ingredient"))
                head.cell(t(lang, "pdf_per_base", n=_num(r.base_yield)))
                head.cell(t(lang, "pdf_per_portion"))
                for name, base, portion in rows:
                    row = table.row()
                    row.cell(name)
                    row.cell(base)
                    row.cell(portion)
        pdf.ln(7)
    return bytes(pdf.output())


def build_menu_pdf(monday: dt.date, days: list[dict], recipes: dict, photos: dict[int, bytes], lang: str) -> bytes:
    """Меню недели для клиентов и кухни: дни пн–пт, блюда с фото. Без цен и состава."""
    pdf = ActPDF(lang)
    pdf.alias_nb_pages()
    pdf.add_page()
    w = pdf.w - pdf.l_margin - pdf.r_margin
    band_h = 40
    gradient_band(pdf, 0, 0, pdf.w, band_h, VIOLET, PINK)
    pdf.set_text_color(*WHITE)
    pdf.set_font("dejavu", "B", 24)
    pdf.set_xy(pdf.l_margin, 11)
    pdf.cell(w, 11, t(lang, "pdf_menu_title"))
    pdf.set_font("dejavu", "", 11)
    pdf.set_xy(pdf.l_margin, 24)
    friday = monday + dt.timedelta(days=4)
    pdf.cell(w, 7, f"{t(lang, 'pdf_company')} · {monday:%d.%m} — {friday:%d.%m.%Y}")
    pdf.set_y(band_h + 8)

    for day in days:
        if not day["items"]:
            continue
        d = dt.date.fromisoformat(day["date"])
        # Уникальные блюда дня (одно блюдо для разных клиентов показываем один раз)
        seen, dishes = set(), []
        for it in day["items"]:
            if it["recipe_id"] not in seen:
                seen.add(it["recipe_id"])
                dishes.append(recipes.get(it["recipe_id"]))
        dishes = [r for r in dishes if r is not None]
        need_h = 12 + 30 * ((len(dishes) + 1) // 2)
        if pdf.get_y() + need_h > pdf.h - 20:
            pdf.add_page()
        pdf.set_text_color(*VIOLET)
        pdf.set_font("dejavu", "B", 13)
        pdf.cell(w, 8, f"{t(lang, 'wd_' + str(d.weekday()))} · {d:%d.%m}", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.ln(1)
        col_w = (w - 6) / 2
        for i, r in enumerate(dishes):
            col = i % 2
            if col == 0 and i:
                pdf.ln(30)
            x = pdf.l_margin + col * (col_w + 6)
            y = pdf.get_y()
            pdf.set_fill_color(*LILAC_BG)
            pdf.rect(x, y, col_w, 26, style="F", round_corners=True, corner_radius=4)
            _image(pdf, photos.get(r.id), x + 2, y + 2, 32, 22, r.name)
            pdf.set_xy(x + 37, y + 4)
            pdf.set_text_color(*INK)
            pdf.set_font("dejavu", "B", 10.5)
            pdf.multi_cell(col_w - 40, 5.2, r.name, new_x=XPos.LEFT, new_y=YPos.NEXT, max_line_height=5.2, align="L")
            pdf.set_x(x + 37)
            pdf.set_font("dejavu", "", 8.5)
            pdf.set_text_color(*MUTED)
            pdf.cell(col_w - 40, 5, t(lang, "rcat_" + (r.category or "main")))
            pdf.set_xy(pdf.l_margin, y)
        pdf.ln(32)
    return bytes(pdf.output())
