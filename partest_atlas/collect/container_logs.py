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

import re
from collections import Counter
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set

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


def build(per_container: Mapping[str, Dict[str, Dict[str, int]]]) -> Dict[str, Any]:
    """Содержание артефакта: что нашлось у каждого контейнера, и сводка."""
    containers = [
        {"name": name, "found": found}
        for name, found in sorted(per_container.items())
        if found
    ]
    return {
        "containers": containers,
        "stats": {
            "containers": len(containers),
            "tables": len({v for c in containers for v in (c["found"].get("table") or {})}),
            "topics": len({v for c in containers for v in (c["found"].get("topic") or {})}),
        },
    }
