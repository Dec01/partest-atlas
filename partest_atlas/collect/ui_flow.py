"""Связи «интерфейс → API» → артефакт `ui.flow`.

Идея. Во время UI-прогона мы и так кликаем по настоящему интерфейсу. Достаточно
подслушать две вещи — что сделал пользователь и какие запросы после этого ушли — и
связь «кнопка → эндпоинт» перестаёт быть ручной картой.

**Связь — эвристика, и она названа эвристикой.** Запрос относится к последнему действию,
если случился в пределах окна. У связи есть `confidence`: `direct`, если запрос ушёл
сразу, и `window`, если попал в хвост окна. Прятать эту разницу нельзя — карту читают как
факт, и связь, угаданная по времени, обязана выглядеть иначе, чем измеренная.

Нормализация обязательна. Строки таблиц и дни календаря получают идентификатор записи в
`data-testid`; без схлопывания каждая строка каждого прогона становится отдельным узлом,
и карта распухает до нечитаемости за неделю.

Здесь — ядро: нормализация и привязка. Инжект слушателя в страницу — тонкий слой сверху,
он зависит от браузерного драйвера, а правила связывания от него не зависят.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Mapping, Optional, Sequence
from urllib.parse import urlsplit

KIND = "ui.flow"

#: Окно атрибуции по умолчанию: столько ждём запроса после действия.
DEFAULT_WINDOW_MS = 2500

#: До этой границы связь считается прямой, дальше — попавшей в хвост окна.
DIRECT_MS = 600

_ID_RE = re.compile(r"/\d+(?=/|$)")
_UUID_RE = re.compile(
    r"/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}(?=/|$)", re.I
)

#: ISO-дату раньше идентификатора: иначе `day-2026-09-04` станет `day-2026-09-{id}`.
#: Lookahead ловит и хвост (`row-42`), и середину (`row-42-select`).
_TESTID_DATE_RE = re.compile(r"-\d{4}-\d{2}-\d{2}(?=-|$)")
_TESTID_ID_RE = re.compile(r"[-_]\d+(?=-|$)")


def norm_testid(testid: Optional[str]) -> Optional[str]:
    """Схлопывает динамический хвост: `task-row-32142` → `task-row-{id}`."""
    if not testid:
        return testid
    testid = _TESTID_DATE_RE.sub("-{date}", testid)
    return _TESTID_ID_RE.sub("-{id}", testid)


def norm_path(url: str) -> str:
    """`/api/v1/clients/42?x=1` → `/api/v1/clients/{id}`."""
    path = urlsplit(url or "").path.rstrip("/")
    path = _UUID_RE.sub("/{id}", path)
    return _ID_RE.sub("/{id}", path)


def confidence_for(gap_ms: float) -> str:
    """Насколько связи можно верить. Два значения, а не флаг: «сразу» и «в окне» —
    разные утверждения, и читающему надо их различать."""
    return "direct" if gap_ms <= DIRECT_MS else "window"


def attribute(
    actions: Sequence[Mapping[str, Any]],
    calls: Sequence[Mapping[str, Any]],
    *,
    window_ms: int = DEFAULT_WINDOW_MS,
    ignore_url: Sequence[str] = (),
    ignore_control: Sequence[str] = (),
) -> List[Dict[str, Any]]:
    """Связать запросы с действиями. Действия и запросы — с отметками `ts` в миллисекундах.

    Запрос относится к **последнему предшествующему** действию. Запрос без действия перед
    ним не относится ни к чему и выбрасывается: приписать его первому попавшемуся
    действию значило бы выдумать связь.
    """
    ordered = sorted(
        (a for a in actions if not _ignored(a.get("control") or a.get("text") or "", ignore_control)),
        key=lambda a: a.get("ts", 0),
    )
    out: List[Dict[str, Any]] = []
    for call in sorted(calls, key=lambda c: c.get("ts", 0)):
        url = str(call.get("url") or "")
        if _ignored(url, ignore_url):
            continue
        ts = call.get("ts", 0)
        previous = [a for a in ordered if a.get("ts", 0) <= ts]
        if not previous:
            continue
        action = previous[-1]
        gap = ts - action.get("ts", 0)
        if gap > window_ms:
            continue
        out.append(
            {
                "screen": action.get("screen") or "",
                "control": norm_testid(action.get("control")),
                "action": action.get("type") or "click",
                "method": str(call.get("method") or "GET").upper(),
                "path": norm_path(url),
                "status": call.get("status"),
                "gapMs": int(gap),
                "confidence": confidence_for(gap),
            }
        )
    return out


def _ignored(value: str, patterns: Sequence[str]) -> bool:
    return any(p and p in value for p in patterns)


def merge(links: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """Схлопнуть повторы, сохранив счётчик и **худшую** достоверность.

    Худшую, а не лучшую: если та же связь один раз наблюдалась прямо, а десять раз — по
    окну, честно сказать, что она в основном угадана. Выбрать лучшую значило бы
    повысить доверие к связи одним удачным наблюдением.
    """
    buckets: Dict[tuple, Dict[str, Any]] = {}
    for link in links:
        key = (link.get("screen"), link.get("control"), link.get("action"),
               link.get("method"), link.get("path"))
        row = buckets.get(key)
        if row is None:
            row = {
                "screen": key[0], "control": key[1], "action": key[2],
                "method": key[3], "path": key[4],
                "count": 0, "confidence": "direct",
            }
            buckets[key] = row
        row["count"] += 1
        if link.get("confidence") == "window":
            row["confidence"] = "window"
    return sorted(buckets.values(), key=lambda r: (r["screen"] or "", r["control"] or "", r["path"]))


def build(links: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    rows = merge(links)
    return {
        "links": rows,
        "stats": {
            "links": len(rows),
            "screens": len({r["screen"] for r in rows if r["screen"]}),
            "endpoints": len({(r["method"], r["path"]) for r in rows}),
            "direct": sum(1 for r in rows if r["confidence"] == "direct"),
        },
    }
