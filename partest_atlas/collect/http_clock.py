"""Часы HTTP-запросов: когда прогон в последний раз просил что-нибудь записать.

Зачем. Сборщик потока шины отличает «запись сделал сам API» от «запись сделал потребитель
по событию». Признак — тишина: если после события тест больше **не просил ничего
записать**, а таблица изменилась, писал не API.

Почему считаются отдельно пишущие запросы. Первая версия смотрела на любой запрос, и на
живом прогоне это сразу развалилось: тест публикует задачу и дальше **опрашивает её
статус**, то есть HTTP не смолкает никогда и тишина не наступает. Опрос — это `GET`, он
ничего не пишет. Граница проходит по последнему запросу с изменяющим методом.

Момент берётся у самих транспортов, патчем одной точки отправки. Ни тела, ни заголовков,
ни адресов не читаем: нужны метод и отметка времени. Это дешевле разбора логов и не
зависит от того, как проект оформил свой клиент.

**Патч ставится только по явному вызову** :func:`install` и снимается :func:`uninstall`.
Библиотека, которая правит чужие классы, обязана уметь вернуть их обратно: иначе её
нельзя выключить в том же процессе, а прогон, включивший сбор однажды, платит за него до
конца жизни процесса.

Признак не доказательство: стенд бывает общий, и записать могла чужая сессия. Поэтому
рядом всегда лежат счётчики запросов — по ним видно, насколько выводу можно верить.

Про разрешение часов. `time.time()` идёт шагами — на Windows около 15 мс, — и несколько
запросов подряд получают одинаковую отметку. Выводу это не мешает: между событием шины и
последней записью лежат **секунды выдержки**, а не миллисекунды. Но сравнивать по этим
часам события внутри одного шага нельзя, и строить на них порядок запросов — тоже.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, List, Tuple

#: Методы, после которых в базе могло что-то поменяться по вине самого API.
WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

_lock = threading.Lock()
_last: float = 0.0
_last_write: float = 0.0
_count: int = 0
_writes: int = 0
_installed: List[str] = []
_patched: List[Tuple[type, str, Any]] = []


def _tick(method: str) -> None:
    global _last, _last_write, _count, _writes
    now = time.time()
    with _lock:
        _last = now
        _count += 1
        if (method or "").upper() in WRITE_METHODS:
            _last_write = now
            _writes += 1


def _method_of(*args: Any, **kwargs: Any) -> str:
    """Метод запроса, если его видно. Не видно — считаем безопасным.

    Обе библиотеки передают объект запроса первым позиционным аргументом, но полагаться
    на это жёстко нельзя: часы не должны ронять прогон, если сигнатура окажется иной.
    """
    obj = args[0] if args else kwargs.get("request")
    return str(getattr(obj, "method", "") or "")


def last() -> float:
    """Время последнего запроса; `0.0` — запросов не было."""
    with _lock:
        return _last


def last_write() -> float:
    """Время последнего запроса с изменяющим методом."""
    with _lock:
        return _last_write


def counters() -> Dict[str, int]:
    """Сколько запросов видели. Лежит рядом с выводом, чтобы было видно, чему верить."""
    with _lock:
        return {"requests": _count, "writes": _writes}


def reset() -> None:
    """Обнулить перед куском работы: интересна активность внутри него."""
    global _last, _last_write, _count, _writes
    with _lock:
        _last = _last_write = 0.0
        _count = _writes = 0


def installed() -> List[str]:
    return list(_installed)


def install() -> List[str]:
    """Пропатчить доступные транспорты. Возвращает, что удалось пропатчить.

    Повторный вызов ничего не добавляет: две обёртки вокруг одной точки считали бы
    каждый запрос дважды.
    """
    try:
        import httpx  # noqa: PLC0415 — зависимость не обязательна
    except ImportError:
        pass
    else:
        _wrap(httpx.Client, "send", "httpx")
        _wrap(httpx.AsyncClient, "send", "httpx-async")

    try:
        import requests  # noqa: PLC0415
    except ImportError:
        pass
    else:
        _wrap(requests.Session, "send", "requests")

    return list(_installed)


def uninstall() -> None:
    """Вернуть транспорты как были. Без этого сбор нельзя выключить в том же процессе."""
    while _patched:
        cls, name, original = _patched.pop()
        setattr(cls, name, original)
    _installed.clear()


def _wrap(cls: type, name: str, label: str) -> None:
    original = getattr(cls, name, None)
    if original is None or getattr(original, "_atlas_clock", False):
        return

    if label.endswith("-async"):
        async def wrapper(self, *a, **kw):  # type: ignore[no-untyped-def]
            _tick(_method_of(*a, **kw))
            return await original(self, *a, **kw)
    else:
        def wrapper(self, *a, **kw):  # type: ignore[no-untyped-def]
            _tick(_method_of(*a, **kw))
            return original(self, *a, **kw)

    wrapper._atlas_clock = True  # type: ignore[attr-defined]
    wrapper.__name__ = getattr(original, "__name__", name)
    wrapper.__doc__ = getattr(original, "__doc__", None)
    setattr(cls, name, wrapper)
    _patched.append((cls, name, original))
    _installed.append(label)
