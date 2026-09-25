"""История прогонов → артефакт `runner.history`.

Единственный артефакт, который **накапливается между прогонами**. У остальных каждое
снятие заменяет предыдущее, а здесь предыдущие записи — и есть содержание: тренд виден
только по нескольким прогонам подряд.

Отсюда два свойства, которых у соседей нет.

Запись обязана быть атомарной **особенно сильно**. Прерванное снятие схемы стоит одного
повторного снятия; прерванная дописка истории теряет то, что уже нельзя пересобрать —
прогоны прошлых дней. Атомарность здесь даёт :mod:`partest_atlas.artifacts`, и именно
поэтому дописка идёт через него, а не через прямую запись файла.

Число записей ограничено. Без предела артефакт растёт линейно по числу прогонов и
однажды перестаёт открываться — а тренд по тысяче прогонов всё равно не читают.
Отбрасывается **начало**: старое, а не новое.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

from partest_atlas import artifacts

KIND = "runner.history"

#: Сколько прогонов держим. Хватает на тренд и на сравнение с «как было до правки».
DEFAULT_LIMIT = 50

#: Что берём из отчёта покрытия. Формат принадлежит `partest`, поэтому читаем
#: осторожно: отсутствие ключа не ошибка, а «эта версия отчёта его не писала».
COVERAGE_KEYS = ("avg", "full", "partial", "empty", "endpoints")


def read(path: Path) -> List[Dict[str, Any]]:
    """Прочитанная история. Чужой вид или отсутствие файла — пустой список.

    Проверка вида обязательна: без неё файл другого сборщика, оказавшийся под этим
    именем, был бы молча принят за историю и дописан.
    """
    doc = artifacts.read(path)
    return list(doc.get("runs") or []) if doc.get("kind") == KIND else []


def trim(runs: Sequence[Mapping[str, Any]], limit: int = DEFAULT_LIMIT) -> List[Dict[str, Any]]:
    """Оставить последние *limit* записей. Отбрасывается начало — старое, не новое."""
    if limit <= 0:
        return []
    return [dict(r) for r in runs][-limit:]


def append(
    path: Path,
    entry: Mapping[str, Any],
    *,
    limit: int = DEFAULT_LIMIT,
    scope: Optional[artifacts.Scope] = None,
    collector: str = "partest-atlas/history",
) -> List[Dict[str, Any]]:
    """Дописать запись о прогоне и вернуть историю целиком."""
    runs = trim([*read(path), dict(entry)], limit)
    artifacts.write(path, KIND, {"runs": runs}, scope=scope, collector=collector)
    return runs


def coverage_snapshot(document: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """Срез покрытия на момент прогона — для тренда по истории.

    Формат отчёта принадлежит `partest`, и читается он осторожно: берутся только
    известные ключи сводки, отсутствие любого из них — не ошибка. Артефакты переживают
    выпуски, и требовать от прошлогоднего отчёта сегодняшних полей значит развалить
    тренд ровно там, где он и нужен.
    """
    summary = (document or {}).get("summary") or {}
    return {key: summary.get(key) for key in COVERAGE_KEYS if key in summary}
