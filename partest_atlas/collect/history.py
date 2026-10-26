"""История прогонов → артефакт `runner.history`.

Единственный артефакт, который **накапливается между прогонами**. У остальных каждое
снятие заменяет предыдущее, а здесь предыдущие записи — и есть содержание: тренд виден
только по нескольким прогонам подряд.

Отсюда три свойства, которых у соседей нет.

Запись обязана быть атомарной **особенно сильно**. Прерванное снятие схемы стоит одного
повторного снятия; прерванная дописка истории теряет то, что уже нельзя пересобрать —
прогоны прошлых дней. Атомарность здесь даёт :mod:`partest_atlas.artifacts`, и именно
поэтому дописка идёт через него, а не через прямую запись файла.

Непонятый файл не трогается. Под именем истории может оказаться битый JSON, чужой
артефакт или история, записанная более новой схемой, — прочитать её нельзя, а
переписать поверх значит потерять прогоны. Запись прогона тогда пропускается с
предупреждением, файл остаётся как был: разбираться с ним — человеку или более новой
версии пакета, а не дописке.

Число записей ограничено. Без предела артефакт растёт линейно по числу прогонов и
однажды перестаёт открываться — а тренд по тысяче прогонов всё равно не читают.
Отбрасывается **начало**: старое, а не новое.

**Записи `runs` для владельца формата непрозрачны.** Их поля задаёт тот, кто дописывает
(сервер карты, конвейер, :func:`coverage_snapshot` для среза покрытия): пакет требует
только, чтобы запись была объектом и записывалась строгим JSON, и ни `validate`, ни
`read` не проверяют в ней ни одного поля. Поэтому чужие поля в записях не делают историю
непонятой, а новые поля у вызывающего не требуют новой версии пакета.

Публичный интерфейс — имена из ``__all__``.
"""

from __future__ import annotations

import itertools
import json
import math
import os
import stat
import warnings
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from partest_atlas import artifacts

KIND = "runner.history"

#: Сколько прогонов держим. Хватает на тренд и на сравнение с «как было до правки».
DEFAULT_LIMIT = 50

#: Что берём из сводки отчёта покрытия. Формат принадлежит `partest`, поэтому читаем
#: осторожно: нет ключа в срезе — отчёт его не писал либо записал не счётчик.
#: `exception` пишет и partest 2.6.0, `scored` — с 2.7.0: строки, по которым взято среднее.
COVERAGE_KEYS = ("avg", "full", "partial", "empty", "endpoints", "exception", "scored")

#: Счётчики сводки: в срез идёт неотрицательное целое не больше :data:`COUNT_LIMIT`.
COUNT_KEYS = COVERAGE_KEYS[1:]

#: Больше любого настоящего счётчика; меньше того, что ломает арифметику и запись.
COUNT_LIMIT = 10**12

#: Правило среднего у отчёта без `meta.avgRule` — так считали все выпуски до partest 2.7.0.
DEFAULT_AVG_RULE = 1

#: Сколько ключей `summary.depth` переносится и какой длины может быть имя.
DEPTH_MAX_KEYS = 16
DEPTH_MAX_NAME = 32

#: Потолок файла истории. Больше этого он не читается, и больше этого пакет его не пишет:
#: десятки записей столько не весят, а JSON в памяти раздувается в разы — 64 МиБ
#: обходились бы прогону в гигабайты.
MAX_BYTES = 16 * 1024 * 1024

#: Что посоветовать в предупреждении о пропуске.
_MOVE = "переместите или удалите файл, чтобы история снова писалась"
_UPGRADE = "обновите partest-atlas"
_BUSY = "файл может быть занят другим процессом; если повторяется — проверьте права"


class UnreadableHistory(UserWarning):
    """Файл истории не понят и не тронут; запись прогона пропущена."""


def _read_bounded(path: Path) -> Tuple[Optional[bytes], Optional[str], str]:
    """``(байты, причина, совет)``: проверка и чтение — по одному дескриптору.

    Обычный ли это файл, решает ``fstat`` открытого дескриптора, а не ``stat`` имени:
    между проверкой и чтением имя могло бы смениться. Ссылка не разыменовывается, где
    есть ``O_NOFOLLOW``; ``O_NONBLOCK`` не даёт повиснуть на канале. Читается не больше
    :data:`MAX_BYTES` + 1 байт. Файла нет — ``(None, None, "")``.
    """
    flags = (
        os.O_RDONLY
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        return None, None, ""
    except OSError as exc:
        # Windows не открывает каталог как файл — скажем, что это, а не «не читается».
        if os.path.isdir(path):
            return None, "не обычный файл", _MOVE
        return None, f"не читается ({type(exc).__name__})", _BUSY
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None, "не обычный файл", _MOVE
        chunks: List[bytes] = []
        left = MAX_BYTES + 1
        while left > 0:
            chunk = os.read(fd, min(left, 1 << 20))
            if not chunk:
                break
            chunks.append(chunk)
            left -= len(chunk)
    except OSError as exc:
        return None, f"не читается ({type(exc).__name__})", _BUSY
    finally:
        os.close(fd)
    raw = b"".join(chunks)
    if len(raw) > MAX_BYTES:
        return None, f"больше {MAX_BYTES} байт", _MOVE
    return raw, None, ""


def _load(path: Path) -> Tuple[List[Dict[str, Any]], Optional[str], str]:
    """``(записи, причина, совет)``. Причина ``None`` — файл понят, пуст или его нет."""
    raw, reason, advice = _read_bounded(path)
    if reason is not None:
        return [], reason, advice
    if not raw:
        # Нет файла или 0 байт (замена без fsync, оборванная питанием): терять нечего.
        return [], None, ""
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return [], "не JSON", _MOVE
    except MemoryError:
        return [], "не помещается в память при разборе", _MOVE
    if not isinstance(doc, dict):
        return [], "не объект JSON", _MOVE
    if doc.get("kind") != KIND:
        return [], f"вида {str(doc.get('kind'))[:40]!r}, а не {KIND!r}", _MOVE
    version = doc.get("version", artifacts.SCHEMA_VERSION)
    if isinstance(version, bool) or not isinstance(version, int):
        return [], "с версией схемы не целым числом", _MOVE
    if version > artifacts.SCHEMA_VERSION:
        return [], f"схемы {version}, новее известной {artifacts.SCHEMA_VERSION}", _UPGRADE
    runs = doc.get("runs")
    if runs is None:
        return [], None, ""
    if not isinstance(runs, list) or not all(isinstance(r, dict) for r in runs):
        return [], "с `runs` не списком записей", _MOVE
    return runs, None, ""


_SKIPS = itertools.count(1)


def _skip(path: Path, reason: str, advice: str = _MOVE) -> List[Dict[str, Any]]:
    """Предупредить и ничего не делать. Время и номер в тексте — чтобы фильтр
    предупреждений, показывающий одинаковое один раз, не скрыл повтор в долгоживущем
    процессе."""
    when = datetime.now().astimezone().isoformat(timespec="milliseconds")
    warnings.warn(
        f"{path}: файл истории {reason} — запись прогона пропущена, файл не тронут; "
        f"{advice} ({when}, пропуск №{next(_SKIPS)})",
        UnreadableHistory,
        stacklevel=3,
    )
    return []


def _strict_json(runs: List[Dict[str, Any]]) -> Optional[List[Dict[str, Any]]]:
    """*runs* без ``NaN``/бесконечностей, проверенные на запись строгим JSON в UTF-8;
    ``None`` — не сериализуются (глубокая вложенность, одиночный суррогат, чужой тип)."""
    try:
        safe = [_json_safe(r) for r in runs]
        json.dumps({"runs": safe}, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, RecursionError, UnicodeEncodeError, MemoryError):
        return None
    return safe


def read(path: Path) -> List[Dict[str, Any]]:
    """Прочитанная история. Нет файла, файл пуст или не понят — пустой список.

    Непонятый — тот же набор, что у :func:`append`, включая записи, которые не
    записываются строгим JSON; ``NaN`` и бесконечности в записях — ``null``. Проверка
    вида обязательна: без неё файл другого сборщика, оказавшийся под этим именем, был бы
    молча принят за историю и дописан.
    """
    runs, reason, _ = _load(Path(path))
    if reason is not None:
        return []
    return _strict_json(runs) or []


def trim(runs: Sequence[Mapping[str, Any]], limit: int = DEFAULT_LIMIT) -> List[Dict[str, Any]]:
    """Оставить последние *limit* записей. Отбрасывается начало — старое, не новое."""
    if limit <= 0:
        return []
    return [dict(r) for r in runs][-limit:]


def _written_size(header: Dict[str, Any], runs: List[Dict[str, Any]]) -> int:
    """Сколько байт займёт документ, как его пишет :mod:`artifacts` (``indent=1``), — с
    запасом: каждый перевод строки считается за два, как в текстовом режиме Windows."""
    text = json.dumps({**header, "runs": runs}, ensure_ascii=False, indent=1)
    return len(text.encode("utf-8")) + text.count("\n")


def _fit(header: Dict[str, Any], runs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Отбросить старейшие записи, пока документ не уложится в :data:`MAX_BYTES`.

    Иначе пакет сам записал бы файл, который потом навсегда отказывался бы дописывать.
    Одна последняя запись, не укладывающаяся и в одиночку, — ``ValueError``: это запись
    вызывающего.
    """
    if not runs or _written_size(header, runs) <= MAX_BYTES:
        return runs
    base = _written_size(header, [])
    sizes = [_written_size(header, [r]) - base + 4 for r in runs]
    total = base + sum(sizes)
    start = 0
    while start < len(runs) - 1 and total > MAX_BYTES:
        total -= sizes[start]
        start += 1
    runs = runs[start:]
    while len(runs) > 1 and _written_size(header, runs) > MAX_BYTES:
        runs = runs[1:]
    if _written_size(header, runs) > MAX_BYTES:
        raise ValueError(
            f"запись истории не укладывается в {MAX_BYTES} байт файла истории даже одна"
        )
    return runs


def append(
    path: Path,
    entry: Mapping[str, Any],
    *,
    limit: int = DEFAULT_LIMIT,
    scope: Optional[artifacts.Scope] = None,
    collector: str = "partest-atlas/history",
) -> List[Dict[str, Any]]:
    """Дописать запись о прогоне и вернуть историю целиком.

    Записи для пакета непрозрачны — их поля задаёт вызывающий, — и прежние переносятся
    как прочитаны, с одной правкой: ``NaN`` и бесконечности, в новой записи и в прежних,
    становятся ``null``, и файл всегда строгий JSON. Файл не растёт больше
    :data:`MAX_BYTES`: старейшие записи отбрасываются и сверх *limit*. ``limit <= 0``
    стирает историю: файл пишется без записей, возвращается ``[]``.

    **Непонятый файл не трогается.** Если файл есть, но это не обычный файл, он больше
    :data:`MAX_BYTES`, не читается, не JSON, не объект, другого вида, схемы новее
    известной (или с версией не целым числом), с ``runs`` не списком записей — или
    оставляемые прежние записи не сериализуются строгим JSON (глубокая вложенность,
    одиночный суррогат), — запись прогона пропускается: сначала
    :class:`UnreadableHistory` с причиной, советом и временем, затем возврат ``[]``, файл
    как был. Так же — если файл не удалось записать (``OSError``: занят другим
    процессом, нет места или прав): прежний файл цел. Исключения из-за файла нет. Пустой
    файл (0 байт) и ``runs``, которого нет или он ``null``, — пустая история.

    Новая запись — данные вызывающего, а не файла: если она не сериализуется строгим
    JSON (``datetime``, ``set``, произвольный объект, одиночный суррогат в строке,
    глубокая вложенность) или одна больше :data:`MAX_BYTES`, это его ошибка —
    ``TypeError`` или ``ValueError``, до записи; файл цел.
    """
    path = Path(path)
    previous, reason, advice = _load(path)
    if reason is not None:
        return _skip(path, reason, advice)
    new = _entry_json(entry)
    kept = _strict_json(trim([*previous, new], limit))
    if kept is None:
        return _skip(path, "с записями, которые не записываются строгим JSON")
    header = {
        "version": artifacts.SCHEMA_VERSION,
        "kind": KIND,
        "scope": (scope or artifacts.Scope.from_env()).as_dict(),
        "collected_at": "0000-00-00T00:00:00+00:00",
        "collector": collector,
    }
    runs = _fit(header, kept)
    try:
        artifacts.write(path, KIND, {"runs": runs}, scope=scope, collector=collector)
    except OSError as exc:
        # Файл занят другим процессом (Windows), нет места или прав: запись атомарна,
        # прежний файл цел, временный убран — пропуск, как у непонятого файла.
        return _skip(path, f"не записан ({type(exc).__name__})", _BUSY)
    return runs


def _entry_json(entry: Mapping[str, Any]) -> Dict[str, Any]:
    """Новая запись без ``NaN``/бесконечностей, проверенная на строгий JSON в UTF-8.

    Не сериализуется — исключение того же типа с пояснением: это ошибка вызывающего, и
    молча пропустить её значит потерять запись, не сказав почему. Слишком глубокая
    вложенность — ``ValueError``.
    """
    try:
        safe = _json_safe(dict(entry))
        json.dumps(safe, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except TypeError as exc:
        raise TypeError(f"запись истории не сериализуется в JSON: {exc}") from exc
    except (ValueError, RecursionError) as exc:
        raise ValueError(f"запись истории не сериализуется строгим JSON в UTF-8: {exc}") from exc
    return safe


def _json_safe(value: Any) -> Any:
    """*value* с ``null`` на месте ``NaN``/``Infinity``: строгий читатель их не примет."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value


def _number(value: Any) -> Any:
    """Конечное число как записано (не ``bool``); иначе ``None``."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return value if math.isfinite(float(value)) else None
    except OverflowError:
        return None


def _count(value: Any) -> Optional[int]:
    """Неотрицательное целое не больше :data:`COUNT_LIMIT` (не ``bool``); иначе ``None``."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 0 <= value <= COUNT_LIMIT else None


def _depth(depth: Mapping[Any, Any]) -> Dict[str, Any]:
    """Безопасная копия `summary.depth`.

    Имя — ASCII-идентификатор не длиннее :data:`DEPTH_MAX_NAME` и без ``__``, ключей не больше
    :data:`DEPTH_MAX_KEYS` (первые подходящие). Значение — конечное число как записано
    или ``null``; число, которое не конечно (``NaN``, бесконечность, целое за пределами
    ``float``), — ``null``. Прочее (строки, ``bool``, контейнеры) отбрасывается вместе с
    ключом: ключи переносятся вперёд-совместимо, но не произвольные данные.
    """
    out: Dict[str, Any] = {}
    for name, value in depth.items():
        if len(out) >= DEPTH_MAX_KEYS:
            break
        if not (
            isinstance(name, str)
            and name.isascii()
            and name.isidentifier()
            and len(name) <= DEPTH_MAX_NAME
            and "__" not in name
        ):
            continue
        if value is None:
            out[name] = None
        elif isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        else:
            out[name] = _number(value)
    return out


def _avg_rule(doc: Mapping[str, Any]) -> Optional[int]:
    """``meta.avgRule``: целое как записано; не целое — ``None``; нет ключа или нет самой
    ``meta`` — ``1``. ``meta``, которая есть, но не объект, — ``None``: о правиле такой
    отчёт ничего не говорит, и «1» было бы догадкой."""
    if "meta" not in doc:
        return DEFAULT_AVG_RULE
    meta = doc["meta"]
    if not isinstance(meta, Mapping):
        return None
    if "avgRule" not in meta:
        return DEFAULT_AVG_RULE
    rule = meta["avgRule"]
    if isinstance(rule, bool) or not isinstance(rule, int) or abs(rule) > COUNT_LIMIT:
        return None
    return rule


__all__ = [
    "COVERAGE_KEYS",
    "DEFAULT_LIMIT",
    "KIND",
    "MAX_BYTES",
    "UnreadableHistory",
    "append",
    "coverage_snapshot",
    "read",
    "trim",
]


def coverage_snapshot(document: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """Срез покрытия на момент прогона — для тренда по истории.

    Формат отчёта принадлежит `partest`, и читается он осторожно: берутся только
    известные ключи сводки, отсутствие любого из них — не ошибка. Артефакты переживают
    выпуски, и требовать от прошлогоднего отчёта сегодняшних полей значит развалить
    тренд ровно там, где он и нужен. Нет ключа в срезе — отчёт его не писал либо записал
    не то. Отчёт при этом не доверенный: ``document`` и ``summary``, не являющиеся
    словарём, читаются как отсутствие, а значения проверяются по типу — исключения срез
    не поднимает.

    - ``avg`` — конечное число не-``bool``, иначе ``null``. ``null`` — «не измерено»: так
      пишет partest с 2.7.0; ``0.0`` при ``endpoints: 0`` из отчёта постарше — тоже
      ``null``.
    - Счётчики (:data:`COUNT_KEYS`) — неотрицательные целые; кривое значение не пишется
      вовсе: у счётчика нет «не измерено», а ``null`` сменил бы тип ключа.
    - ``depth`` — безопасная копия ``summary.depth`` (:func:`_depth`); ``null`` —
      ``null``; не словарь или нет ключа — ключа нет.
    - ``avgRule`` — правило среднего, ``meta.avgRule``: целое как записано, в том числе
      неизвестное; не целое или ``meta`` не объект — ``null``, «правило неизвестно»; нет
      ключа или нет ``meta`` — ``1``, как считали все прежние выпуски. Пишется, если
      сводка дала хоть один ключ.
    """
    doc = document if isinstance(document, Mapping) else {}
    summary = doc.get("summary")
    if not isinstance(summary, Mapping):
        summary = {}
    snapshot: Dict[str, Any] = {}
    for key in COVERAGE_KEYS:
        if key not in summary:
            continue
        if key == "avg":
            snapshot["avg"] = _number(summary["avg"])
            continue
        value = _count(summary[key])
        if value is not None:
            snapshot[key] = value
    if snapshot.get("avg") == 0 and snapshot.get("endpoints") == 0:
        snapshot["avg"] = None
    if "depth" in summary:
        depth = summary["depth"]
        if depth is None:
            snapshot["depth"] = None
        elif isinstance(depth, Mapping):
            snapshot["depth"] = _depth(depth)
    if snapshot:
        snapshot["avgRule"] = _avg_rule(doc)
    return snapshot
