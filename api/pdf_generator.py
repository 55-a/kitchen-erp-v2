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
BLACK = (0, 0, 0)


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


class FormalActPDF(ActPDF):
    """Акт в строгой форме 1С: без цветных полос, внизу — номер страницы."""

    def header(self) -> None:
        if self.page_no() > 1:
            self.set_y(10)

    def footer(self) -> None:
        self.set_y(-12)
        self.set_font("dejavu", "", 7.5)
        self.set_text_color(*MUTED)
        self.cell(0, 5, f"{self.page_no()}/{{nb}}", align="R")
        self.set_text_color(*BLACK)


def build_act_pdf(*, statement: dict, lang: str) -> bytes:
    """
    Акт сверки в форме 1С — как привыкли бухгалтеры: «Мы, нижеподписавшиеся…», строгая таблица
    с рамками, долг на начало, все поставки и оплаты по датам, итоги, долг на конец, сумма
    прописью и подписи сторон. Слова простые: «Поставили / Оплатили / Долг» вместо
    «Дебет / Кредит / Сальдо». statement — результат services.client_statement().
    """
    from api.num_words import amount_in_words

    st = statement
    client_name = st["client"]
    company = get_settings().company_legal_name or t(lang, "pdf_company")
    d_from, d_to = f"{st['date_from']:%d.%m.%Y}", f"{st['date_to']:%d.%m.%Y}"
    pdf = FormalActPDF(lang)
    pdf.alias_nb_pages()
    pdf.add_page()
    w = pdf.w - pdf.l_margin - pdf.r_margin
    pdf.set_text_color(*BLACK)
    pdf.set_draw_color(*BLACK)
    pdf.set_line_width(0.2)

    # ---------- Шапка: название, город и дата, стороны ----------
    pdf.set_font("dejavu", "B", 13)
    pdf.cell(w, 8, t(lang, "act1c_title"), align="C", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_font("dejavu", "B", 9)
    pdf.cell(w / 2, 6, t(lang, "act1c_city"))
    pdf.set_font("dejavu", "", 9)
    pdf.cell(w / 2, 6, f"{dt.date.today():%d.%m.%Y}", align="R", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(2)
    pdf.cell(w, 5, t(lang, "act1c_we"), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    for name in (company, client_name):
        pdf.set_font("dejavu", "", 9)
        pdf.cell(30, 7, t(lang, "act1c_side"))
        pdf.set_font("dejavu", "B", 10.5)
        pdf.cell(w - 30, 7, name, border="B", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(2)
    pdf.set_font("dejavu", "", 9)
    pdf.multi_cell(w, 5, align="L", text=t(lang, "act1c_intro", date_from=d_from, date_to=d_to), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(3)

    # ---------- Таблица: долг на начало → операции → итоги → долг на конец ----------
    bold = FontFace(emphasis="BOLD")
    shaded = FontFace(emphasis="BOLD", fill_color=(235, 235, 235))
    pdf.set_font("dejavu", "", 8.5)
    with pdf.table(
        col_widths=(78, 22, 28, 28, 26),                       # сумма = ширине страницы (182 мм)
        text_align=("LEFT", "CENTER", "RIGHT", "RIGHT", "RIGHT"),
        line_height=4.6,
        padding=(0.4, 1.4),
        borders_layout="ALL",
        headings_style=shaded,
        first_row_as_headings=True,
    ) as table:
        head = table.row()
        for key in ("act1c_col_doc", "act1c_col_date", "act_col_delivered", "act_col_paid", "act1c_col_debt"):
            head.cell(t(lang, key), align="CENTER")

        row = table.row()
        row.cell(t(lang, "act1c_debt_on", date=d_from), style=bold, colspan=2)
        row.cell("")
        row.cell("")
        row.cell(fmt_money(st["opening"]), style=bold)

        if not st["rows"]:
            table.row().cell(t(lang, "act_no_ops"), colspan=5, align="CENTER")
        for r in st["rows"]:
            row = table.row()
            if r["kind"] == "delivery":
                row.cell(t(lang, "act_row_delivery", dish=r["dish"], portions=r["portions"]))
                row.cell(f"{r['date']:%d.%m.%Y}")
                row.cell(fmt_money(r["delivered"]))
                row.cell("")
            else:
                row.cell(t(lang, "act_row_payment", type=t(lang, f"pay_{r['payment_type']}")))
                row.cell(f"{r['date']:%d.%m.%Y}")
                row.cell("")
                row.cell(fmt_money(r["paid"]))
            row.cell(fmt_money(r["balance"]))

        row = table.row()
        row.cell(t(lang, "act1c_totals", date_from=d_from, date_to=d_to, portions=st["portions"]), style=shaded, colspan=2)
        row.cell(fmt_money(st["delivered"]), style=shaded)
        row.cell(fmt_money(st["paid"]), style=shaded)
        row.cell("", style=shaded)

        row = table.row()
        row.cell(t(lang, "act1c_debt_on", date=d_to), style=shaded, colspan=2)
        row.cell("", style=shaded)
        row.cell("", style=shaded)
        row.cell(fmt_money(st["closing"]), style=shaded)

    # ---------- Итог сверки: сумма цифрами и прописью ----------
    if pdf.get_y() > pdf.h - 70:
        pdf.add_page()
    pdf.ln(5)
    closing = st["closing"]
    params = {"date": d_to, "client": client_name, "company": company,
              "amount": fmt_money(abs(closing)), "words": amount_in_words(abs(closing), lang)}
    key = "act1c_result_debt" if closing > 0 else "act1c_result_over" if closing < 0 else "act1c_result_zero"
    pdf.set_font("dejavu", "B", 9.5)
    pdf.multi_cell(w, 5.2, t(lang, key, **params), align="L", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(1.5)
    pdf.set_font("dejavu", "", 8)
    pdf.set_text_color(*MUTED)
    pdf.multi_cell(w, 4.4, align="L", text=t(lang, "act1c_note", company=company, client=client_name), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_text_color(*BLACK)

    # ---------- Подписи сторон ----------
    if pdf.get_y() > pdf.h - 48:
        pdf.add_page()
    pdf.ln(10)
    y = pdf.get_y()
    col_w = (w - 16) / 2
    for i, name in enumerate((company, client_name)):
        sx = pdf.l_margin + i * (col_w + 16)
        pdf.set_xy(sx, y)
        pdf.set_font("dejavu", "B", 9.5)
        pdf.multi_cell(col_w, 5, name, new_x=XPos.LEFT, new_y=YPos.NEXT)
        pdf.ln(10)
        line_y = pdf.get_y()
        pdf.line(sx, line_y, sx + col_w, line_y)
        pdf.set_xy(sx, line_y + 0.5)
        pdf.set_font("dejavu", "", 7.5)
        pdf.set_text_color(*MUTED)
        pdf.cell(col_w, 4, t(lang, "act1c_sign_hint"), align="C", new_x=XPos.LEFT, new_y=YPos.NEXT)
        pdf.ln(2)
        pdf.set_x(sx)
        pdf.cell(col_w, 4, t(lang, "act_sign_stamp"))
        pdf.set_text_color(*BLACK)

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
