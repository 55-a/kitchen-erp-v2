"""
Сумма прописью для акта сверки — как в 1С: «Один миллион восемьдесят восемь тысяч триста сум 00 тийин».
Языки: ru, uz (латиница), oz (кириллица — из uz), en, zh.
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

# ---------------------------------------------------------------- русский

_RU_UNITS_M = ["", "один", "два", "три", "четыре", "пять", "шесть", "семь", "восемь", "девять"]
_RU_UNITS_F = ["", "одна", "две", "три", "четыре", "пять", "шесть", "семь", "восемь", "девять"]
_RU_TEENS = ["десять", "одиннадцать", "двенадцать", "тринадцать", "четырнадцать", "пятнадцать",
             "шестнадцать", "семнадцать", "восемнадцать", "девятнадцать"]
_RU_TENS = ["", "", "двадцать", "тридцать", "сорок", "пятьдесят", "шестьдесят", "семьдесят", "восемьдесят", "девяносто"]
_RU_HUNDREDS = ["", "сто", "двести", "триста", "четыреста", "пятьсот", "шестьсот", "семьсот", "восемьсот", "девятьсот"]
# (одна, две-четыре, пять+, женский род)
_RU_SCALES = [None, ("тысяча", "тысячи", "тысяч", True), ("миллион", "миллиона", "миллионов", False),
              ("миллиард", "миллиарда", "миллиардов", False)]


def _ru_plural(n: int, forms: tuple[str, str, str]) -> str:
    if 11 <= n % 100 <= 14:
        return forms[2]
    if n % 10 == 1:
        return forms[0]
    if 2 <= n % 10 <= 4:
        return forms[1]
    return forms[2]


def _ru_triad(n: int, feminine: bool) -> list[str]:
    words = [_RU_HUNDREDS[n // 100]]
    rest = n % 100
    if 10 <= rest <= 19:
        words.append(_RU_TEENS[rest - 10])
    else:
        words.append(_RU_TENS[rest // 10])
        words.append((_RU_UNITS_F if feminine else _RU_UNITS_M)[rest % 10])
    return [w for w in words if w]


def _ru(n: int) -> str:
    if n == 0:
        return "ноль"
    words: list[str] = []
    for i in range(len(_RU_SCALES) - 1, -1, -1):
        triad = (n // 1000 ** i) % 1000
        if not triad:
            continue
        scale = _RU_SCALES[i]
        words += _ru_triad(triad, bool(scale and scale[3]))
        if scale:
            words.append(_ru_plural(triad, scale[:3]))
    return " ".join(words)


# ---------------------------------------------------------------- o'zbekcha (lotin)

_UZ_UNITS = ["", "bir", "ikki", "uch", "to'rt", "besh", "olti", "yetti", "sakkiz", "to'qqiz"]
_UZ_TENS = ["", "o'n", "yigirma", "o'ttiz", "qirq", "ellik", "oltmish", "yetmish", "sakson", "to'qson"]
_UZ_SCALES = ["", "ming", "million", "milliard"]


def _uz(n: int) -> str:
    if n == 0:
        return "nol"
    words: list[str] = []
    for i in range(len(_UZ_SCALES) - 1, -1, -1):
        triad = (n // 1000 ** i) % 1000
        if not triad:
            continue
        if triad // 100:
            words += [_UZ_UNITS[triad // 100], "yuz"]
        words += [w for w in (_UZ_TENS[triad % 100 // 10], _UZ_UNITS[triad % 10]) if w]
        if _UZ_SCALES[i]:
            words.append(_UZ_SCALES[i])
    return " ".join(words)


# ---------------------------------------------------------------- English

_EN_UNITS = ["", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve",
             "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen"]
_EN_TENS = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]
_EN_SCALES = ["", "thousand", "million", "billion"]


def _en_triad(n: int) -> list[str]:
    words = []
    if n // 100:
        words += [_EN_UNITS[n // 100], "hundred"]
    rest = n % 100
    if rest < 20:
        if rest:
            words.append(_EN_UNITS[rest])
    else:
        words.append(_EN_TENS[rest // 10] + (f"-{_EN_UNITS[rest % 10]}" if rest % 10 else ""))
    return words


def _en(n: int) -> str:
    if n == 0:
        return "zero"
    words: list[str] = []
    for i in range(len(_EN_SCALES) - 1, -1, -1):
        triad = (n // 1000 ** i) % 1000
        if triad:
            words += _en_triad(triad) + ([_EN_SCALES[i]] if _EN_SCALES[i] else [])
    return " ".join(words)


# ---------------------------------------------------------------- 中文

_ZH_DIGITS = "零一二三四五六七八九"
_ZH_SMALL = ["", "十", "百", "千"]
_ZH_BIG = ["", "万", "亿"]


def _zh_section(n: int) -> str:
    """0 < n < 10000 → 一千零八"""
    out, zero = "", False
    for pos in range(3, -1, -1):
        d = (n // 10 ** pos) % 10
        if d == 0:
            zero = bool(out)
            continue
        if zero:
            out += "零"
            zero = False
        out += _ZH_DIGITS[d] + _ZH_SMALL[pos]
    return out


def _zh(n: int) -> str:
    if n == 0:
        return "零"
    out, need_zero = "", False
    for i in range(len(_ZH_BIG) - 1, -1, -1):
        sec = (n // 10000 ** i) % 10000
        if sec == 0:
            need_zero = bool(out)
            continue
        if out and (need_zero or sec < 1000):
            out += "零"
        out += _zh_section(sec) + _ZH_BIG[i]
        need_zero = False
    return out[1:] if out.startswith("一十") else out   # 一十五 → 十五


# ---------------------------------------------------------------- общая функция

def amount_in_words(amount, lang: str) -> str:
    """Сумма прописью с валютой: целые сумы словами, тийины цифрами (как в 1С)."""
    value = Decimal(str(amount or 0)).copy_abs().quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    whole, tiyin = int(value), int((value - int(value)) * 100)
    if lang == "ru":
        text = f"{_ru(whole)} сум {tiyin:02d} тийин"
    elif lang in ("uz", "oz"):
        text = f"{_uz(whole)} so'm {tiyin:02d} tiyin"
    elif lang == "zh":
        return f"{_zh(whole)}苏姆" + (f"{tiyin:02d}提因" if tiyin else "整")
    else:
        text = f"{_en(whole)} soum {tiyin:02d} tiyin"
    text = text[0].upper() + text[1:]
    if lang == "oz":
        from bot.uz_cyrillic import to_cyrillic

        text = to_cyrillic(text)
    return text
