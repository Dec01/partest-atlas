"""Связи из логов контейнеров → артефакт `containers.logs`.

Зачем. Карта «какой контейнер что обслуживает» размечается руками и остаётся разметкой
«по тому, что видно снаружи». Логи знают это точно и **без всякой инструментации**:
приложение само печатает, какую таблицу задело и какое событие разобрало.

Что удаётся достать, не поднимая уровень логирования (проверено на живом стенде):
имя таблицы из текста ошибки, класс слушателя — а значит топик, который контейнер
разбирает, — и сокращённые имена логгеров, по которым видны доменные сервисы внутри.

Ни один из следов не требует включать отладочный уровень: на общем стенде уровень
логирования не наш, и правила, которые его требуют, в конфигурации есть, но выключены.

**Правила — в `atlas.yaml`, а не здесь.** У каждого приложения свой формат логов, и
зашитый набор образцов означал бы, что второй проект требует правки пакета.

Разбор — чистые функции :func:`scan_text` и :func:`listener_to_topic`: логи приходят
строкой, и весь разбор проверяется без доступа к контейнерам.
"""

from __future__ import annotations

import importlib
import re
from collections import Counter
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set

from partest_atlas import artifacts
from partest_atlas.artifacts import Scope
from partest_atlas.config import Config, load
from partest_atlas.lang import t

KIND = "containers.logs"

DEFAULT_TAIL = 2000

#: Хвосты имён классов-слушателей, которые не входят в имя топика.
_LISTENER_SUFFIX = re.compile(r"(Listener|Consumer|Handler)$")

#: Всё, кроме букв и цифр: имя класса и имя топика пишут по-разному, а состав слов
#: совпадает.
_NOT_ALNUM = re.compile(r"[^a-z0-9]")


@dataclass(frozen=True)
class Rule:
    """Одно правило поиска в логах. Приходит из конфигурации целиком."""

    kind: str
    pattern: str
    enabled: bool = True
    #: Оставлять только значения из известного словаря. Без этого правило «слово с
    #: точками — это топик» тащит из логов каждое имя java-класса.
    known_only: bool = False
    value_pattern: str = ""
    note: str = ""

    def compiled(self) -> "re.Pattern[str]":
        return re.compile(self.pattern)

    def accepts(self, value: str) -> bool:
        return not self.value_pattern or bool(re.match(self.value_pattern, value))


def rules_from(section: Mapping[str, Any]) -> List[Rule]:
    """Правила из раздела конфигурации. Выключенные и беспустые отбрасываются здесь,
    чтобы разбор не проверял это на каждой строке."""
    out: List[Rule] = []
    for raw in section.get("rules") or []:
        rule = Rule(
            kind=raw.get("kind", "other"),
            pattern=raw.get("pattern", ""),
            enabled=raw.get("enabled", True) is not False,
            known_only=bool(raw.get("known_only")),
            value_pattern=raw.get("value_pattern", ""),
            note=raw.get("note", ""),
        )
        if rule.enabled and rule.pattern:
            out.append(rule)
    return out


def listener_to_topic(name: str, topics: Sequence[str]) -> Optional[str]:
    """`ExcelImportRequestedListener` → `excel.import.requested`, если такой топик есть.

    Сопоставление по буквам без разделителей: имя класса и имя топика пишут по-разному,
    но состав слов совпадает. Сверяется **со списком настоящих топиков** — угадывать
    имя, которого нет, значило бы записать в карту связь, которой не существует.
    """
    stem = _LISTENER_SUFFIX.sub("", name or "")
    key = _NOT_ALNUM.sub("", stem.lower())
    if not key:
        return None
    for topic in topics:
        if _NOT_ALNUM.sub("", topic.lower()) == key:
            return topic
    return None


def scan_text(
    text: str,
    rules: Sequence[Rule],
    known: Optional[Mapping[str, Set[str]]] = None,
) -> Dict[str, Dict[str, int]]:
    """Применить правила к тексту логов и посчитать совпадения по видам."""
    known = known or {}
    hits: Dict[str, Counter] = {}

    for rule in rules:
        found = rule.compiled().findall(text or "")
        if not found:
            continue
        bucket = hits.setdefault(rule.kind, Counter())
        vocab = known.get(rule.kind) if rule.known_only else None
        for raw in found:
            value = (raw[0] if isinstance(raw, tuple) else raw).strip()
            if not value or not rule.accepts(value):
                continue
            if vocab is not None and value not in vocab:
                continue
            bucket[value] += 1

    # Слушатели переводятся в топики отдельным шагом: это **вывод**, а не находка.
    # Нерасшифрованные не хранятся — в логах их десятки, и все они внутренние классы
    # фреймворка, к делу отношения не имеющие.
    listeners = hits.pop("listener", Counter())
    topics = sorted(known.get("topic") or ())
    for name, count in listeners.items():
        topic = listener_to_topic(name, topics)
        if topic:
            hits.setdefault("topic", Counter())[topic] += count

    return {kind: dict(sorted(counter.items())) for kind, counter in sorted(hits.items()) if counter}


class SourceNotConfigured(RuntimeError):
    """Источник журналов не описан либо описан неверно."""


def open_source(section: Mapping[str, Any]):
    """Источник журналов так, как назвал его проект. ``None``, если не назвал.

    ``source: "модуль:функция"`` — функция, отдающая пары «имя контейнера → текст
    журнала». Откуда она их берёт, пакету знать не положено и не нужно: у одного проекта
    это панель управления контейнерами, у другого `docker logs`, у третьего файл или
    журнал системы. Сборщику нужен **текст**, а не инструмент.

    Зашить сюда конкретный инструмент значило бы, что второй проект требует правки
    пакета. Ровно та протечка, из-за которой соединение с базой и читатель шины тоже
    приходят из конфигурации.
    """
    target = section.get("source")
    if not target:
        return None
    module_name, _, attr = str(target).partition(":")
    if not attr:
        raise SourceNotConfigured(t(
            f"container_logs.source={target!r}: ожидалось 'модуль:функция'",
            f"container_logs.source={target!r}: expected 'module:function'",
        ))
    try:
        module = importlib.import_module(module_name)
    except Exception as exc:
        raise SourceNotConfigured(t(
            f"container_logs.source={target!r} не импортируется: {exc}",
            f"container_logs.source={target!r} cannot be imported: {exc}",
        )) from exc
    factory = getattr(module, attr, None)
    if factory is None:
        raise SourceNotConfigured(t(
            f"container_logs.source={target!r}: в {module_name} нет {attr!r}",
            f"container_logs.source={target!r}: {module_name} has no {attr!r}",
        ))
    return factory


def known_values(schema_doc: Mapping[str, Any], bus_doc: Mapping[str, Any]) -> Dict[str, Set[str]]:
    """Словари, по которым отсеиваются ложные совпадения.

    Таблицы — из снятой схемы, топики — из того, что реально есть на брокере. Оба берутся
    из **уже снятых артефактов**: сборщик логов не ходит ни в базу, ни в шину, а опирается
    на наблюдения соседей. Их отсутствие не ошибка — просто словарь будет пуст, и правила
    с `known_only` ничего не дадут.
    """
    tables: Set[str] = set()
    for database in schema_doc.get("databases") or []:
        for table in database.get("tables") or []:
            if table.get("name"):
                tables.add(str(table["name"]))
    topics = {
        str(record["topic"])
        for record in (bus_doc.get("records") or [])
        if record.get("topic")
    }
    return {"table": tables, "topic": topics}


def count_lines(text: Optional[str]) -> int:
    """Строк в тексте журнала: последняя без перевода строки тоже строка."""
    if not text:
        return 0
    return text.count("\n") + (0 if text.endswith("\n") else 1)


def build(
    per_container: Mapping[str, Dict[str, Dict[str, int]]],
    logs: Optional[Sequence[Mapping[str, Any]]] = None,
) -> Dict[str, Any]:
    """Содержание артефакта: что нашлось у каждого контейнера, и сводка.

    `containers` — только контейнеры с находками, как раньше. *logs* — что прочитано:
    ``{name, read, lines}`` на каждый контейнер, который назвал источник. Без этого
    «журнал не прочитан» и «прочитан, связей нет» в артефакте неотличимы: в обоих
    случаях контейнера в `containers` просто нет.
    """
    containers = [
        {"name": name, "found": found}
        for name, found in sorted(per_container.items())
        if found
    ]
    stats: Dict[str, Any] = {
        "containers": len(containers),
        "tables": len({v for c in containers for v in (c["found"].get("table") or {})}),
        "topics": len({v for c in containers for v in (c["found"].get("topic") or {})}),
    }
    payload: Dict[str, Any] = {"containers": containers, "stats": stats}
    if logs is not None:
        rows = sorted(
            ({"name": str(r["name"]), "read": bool(r.get("read")), "lines": int(r.get("lines") or 0)}
             for r in logs),
            key=lambda r: r["name"],
        )
        payload["logs"] = rows
        stats["read"] = sum(1 for r in rows if r["read"])
        stats["unread"] = sum(1 for r in rows if not r["read"])
        stats["lines"] = sum(r["lines"] for r in rows)
    return payload


def collect(config: Optional[Config] = None, *, scope: Optional[Scope] = None) -> Dict[str, Any]:
    """Прочитать журналы из источника проекта и слепить содержание артефакта.

    Только при объявленном `container_logs.source`: без него — :class:`SourceNotConfigured`,
    а не пустой артефакт. Пустой артефакт выглядел бы как «журналы прочитаны, связей нет»,
    хотя их никто не читал. Так же без правил: искать в журналах было бы нечего.

    Словари для правил `known_only` — из артефактов `db.schema` и `bus.flow` той же
    области, если они уже сняты. Журнал ``None`` у контейнера — «не прочитан»: в `logs`
    он записан с ``read: false``.
    """
    cfg = config or load()
    where = scope or cfg.scope
    section = cfg.section("container_logs")
    factory = open_source(section)
    if factory is None:
        raise SourceNotConfigured(t(
            "container_logs.source не объявлен в atlas.yaml — журналы читать нечем; "
            "назовите функцию проекта: source: 'модуль:функция'",
            "container_logs.source is not declared in atlas.yaml — nothing to read logs with; "
            "name a project function: source: 'module:function'",
        ))
    rules = rules_from(section)
    if not rules:
        raise SourceNotConfigured(t(
            "container_logs.rules пуст или все правила выключены — искать в журналах нечего",
            "container_logs.rules is empty or every rule is disabled — nothing to look for",
        ))
    logs = factory()
    if not isinstance(logs, Mapping):
        raise SourceNotConfigured(t(
            f"container_logs.source вернул {type(logs).__name__}, "
            "ожидался словарь «имя контейнера → текст журнала»",
            f"container_logs.source returned {type(logs).__name__}, "
            "expected a mapping 'container name -> log text'",
        ))
    known = known_values(
        artifacts.read(where.path(cfg.artifacts_dir, "db.schema")),
        artifacts.read(where.path(cfg.artifacts_dir, "bus.flow")),
    )
    per_container: Dict[str, Dict[str, Dict[str, int]]] = {}
    read_rows: List[Dict[str, Any]] = []
    for name, text in logs.items():
        if isinstance(text, bytes):
            text = text.decode("utf-8", errors="replace")
        if text is not None and not isinstance(text, str):
            raise SourceNotConfigured(t(
                f"container_logs.source: журнал контейнера {name!r} — {type(text).__name__}, "
                "ожидался текст",
                f"container_logs.source: the log of container {name!r} is "
                f"{type(text).__name__}, expected text",
            ))
        # `None` — источник назвал контейнер, но журнал не достал: «не прочитан», а не
        # «пусто».
        read_rows.append({"name": str(name), "read": text is not None, "lines": count_lines(text)})
        per_container[str(name)] = scan_text(text or "", rules, known)
    return build(per_container, read_rows)


def write(config: Optional[Config] = None, *, scope: Optional[Scope] = None) -> Dict[str, Any]:
    """Снять связи из журналов и положить артефакт. Возвращает содержание."""
    cfg = config or load()
    where = scope or cfg.scope
    payload = collect(cfg, scope=where)
    artifacts.write(
        where.path(cfg.artifacts_dir, KIND),
        KIND,
        payload,
        scope=where,
        collector="partest-atlas/container_logs",
    )
    return payload
