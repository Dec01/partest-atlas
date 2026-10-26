"""Язык сообщений: русский или английский.

Пакет писался по-русски, и всё, что он печатает и возвращает человеку, было только
по-русски. Английский нужен тем, кто читает вывод команды без знания языка, — проекту на
другом языке и английской странице документации, которая показывает этот вывод.

Язык выбирается **один раз на вызов команды**: ключ ``--lang`` важнее локали, локаль
важнее умолчания. Умолчание — русский: локаль ``C``/``POSIX`` (так бывает на конвейере и
в контейнере) или неопределимая не переключает вывод тех, кто уже на него опирается.

Строки живут парой прямо там, где печатаются, — ``t("по-русски", "in English")``. Отдельный
каталог сообщений расходился бы с кодом молча, а пару видно при каждой правке.

Код, который зовёт функции пакета сам, язык не выбирает и получает русский, как раньше:
переключает его только команда ``partest-atlas`` и только на время своего вызова.

**Модуль внутренний и не потокобезопасный.** Язык — одно значение на процесс: вызов
:func:`set_lang` (или ``cli.main``) в одном потоке меняет тексты ``validate``, ``config`` и
адаптеров во всех остальных. Веб-процессу (сервер карты) звать его нельзя. В публичный
интерфейс пакета модуль не входит.
"""

from __future__ import annotations

import locale
import os
from typing import Mapping, Optional

RU = "ru"
EN = "en"
SUPPORTED = (RU, EN)
DEFAULT = RU

_current = DEFAULT


def current() -> str:
    return _current


def set_lang(code: str) -> None:
    """Выбрать язык. Неизвестный код — ошибка на месте, а не тихий русский."""
    global _current
    if code not in SUPPORTED:
        raise ValueError(f"unknown language {code!r}; known: {', '.join(SUPPORTED)}")
    _current = code


def t(ru: str, en: str) -> str:
    """Строка на текущем языке."""
    return en if _current == EN else ru


def from_locale_name(name: Optional[str]) -> str:
    """Язык по имени локали: ``ru_RU.UTF-8`` и ``Russian_Russia`` — русский, ``C`` и пусто —
    умолчание, любой другой язык — английский."""
    low = (name or "").strip().lower()
    if not low or low.split(".", 1)[0] in {"c", "posix"}:
        return DEFAULT
    if low.startswith("ru"):
        return RU
    return EN


def detect(environ: Optional[Mapping[str, str]] = None) -> str:
    """Язык по локали: ``LC_ALL``, ``LC_MESSAGES``, ``LANG`` — первая непустая, как у
    POSIX; без них — локаль процесса (на Windows переменных обычно нет)."""
    env = os.environ if environ is None else environ
    for name in ("LC_ALL", "LC_MESSAGES", "LANG"):
        value = env.get(name)
        if value:
            return from_locale_name(value)
    if environ is not None:
        return DEFAULT
    try:
        return from_locale_name(locale.getlocale()[0])
    except (ValueError, TypeError):  # pragma: no cover - зависит от платформы
        return DEFAULT
