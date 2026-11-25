"""
Узбекский: латиница → кириллица (o'zbekcha → ўзбекча).

Кириллическая версия интерфейса (код языка "oz", как на узбекских сайтах) не переводится
вручную, а строится из латинской автоматически — так две версии никогда не разойдутся.
Тот же алгоритм повторён в static/js/i18n.js для Mini App.

Не трогаем: {плейсхолдеры}, HTML-теги, /команды, @ники, ссылки и названия брендов.
"""
from __future__ import annotations

import re

# Куски текста, которые нельзя транслитерировать
_PROTECT = re.compile(
    r"(\{[^}]*\}|<[^>]+>|https?://\S+|/[a-z_]+|@\w+"
    r"|\b(?:Kuxnya|NVGR|Telegram|Excel|CSV|PDF|Gemini|Mini App|ID|UZS|FOYDALI FOOD GROUP|NO NAMELA)\b)"
)
_APOSTROPHES = re.compile(r"[ʻʼ‘’`´]")

_SINGLE = {
    "a": "а", "b": "б", "d": "д", "f": "ф", "g": "г", "h": "ҳ", "i": "и", "j": "ж", "k": "к",
    "l": "л", "m": "м", "n": "н", "o": "о", "p": "п", "q": "қ", "r": "р", "s": "с", "t": "т",
    "u": "у", "v": "в", "x": "х", "y": "й", "z": "з", "c": "с", "w": "в",
}
_Y_PAIRS = {"a": "я", "o": "ё", "u": "ю", "e": "е"}   # ya → я, yo → ё, yu → ю, ye → е


def _case(src: str, out: str) -> str:
    """Регистр результата — по первой букве источника (Sh → Ш, sh → ш)."""
    return out.upper() if src[:1].isupper() else out


def _translit_chunk(text: str) -> str:
    text = _APOSTROPHES.sub("'", text)
    # o' и g' — отдельные буквы, обрабатываем первыми (yo'q → йўқ, а не ёқ)
    text = re.sub(r"([Oo])'", lambda m: _case(m.group(1), "ў"), text)
    text = re.sub(r"([Gg])'", lambda m: _case(m.group(1), "ғ"), text)

    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        low = ch.lower()
        nxt = text[i + 1].lower() if i + 1 < n else ""
        prev_is_letter = i > 0 and text[i - 1].isalpha()

        if low == "s" and nxt == "h":
            out.append(_case(ch, "ш")); i += 2; continue
        if low == "c" and nxt == "h":
            out.append(_case(ch, "ч")); i += 2; continue
        if low == "y" and nxt in _Y_PAIRS:
            out.append(_case(ch, _Y_PAIRS[nxt])); i += 2; continue
        if low == "e":
            # В начале слова — «э» (eng → энг), внутри слова — «е»
            out.append(_case(ch, "е" if prev_is_letter else "э")); i += 1; continue
        if ch == "'":
            out.append("ъ"); i += 1; continue      # tutuq belgisi: ma'lumot → маълумот
        if low in _SINGLE:
            out.append(_case(ch, _SINGLE[low])); i += 1; continue
        out.append(ch); i += 1                     # цифры, знаки, эмодзи, уже кириллица
    return "".join(out)


def to_cyrillic(text: str) -> str:
    """Транслитерирует строку, сохраняя плейсхолдеры, теги, команды и бренды."""
    parts = _PROTECT.split(text)
    # split с группой: нечётные элементы — защищённые куски
    return "".join(p if i % 2 else _translit_chunk(p) for i, p in enumerate(parts))
