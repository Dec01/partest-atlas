"""Контракт артефактов: версионированные JSON рядом с прогоном.

Каждый сборщик пишет один файл со своей схемой. Версия и вид несутся в самом файле, чтобы
потребитель мог честно сказать «артефакт старого формата», а не упасть на отсутствующем
поле.

Файлы дописываются, а не перезаписываются вслепую: прогон может идти в несколько процессов
(xdist), и каждый воркер добавляет свои записи.

Область действия
----------------
Артефакт принадлежит паре **(система, контур)**. Система — самостоятельный продукт
заказчика (интернет-магазин, учётная система, склад), контур — среда, в которой она
развёрнута (тест, препрод). Раскладка на диске повторяет эту пару::

    atlas/<система>/<контур>/db_schema.json

Артефакты первой версии, у которых области действия нет, читаются как ``default/default`` —
переносить старые данные не требуется.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

SCHEMA_VERSION = 2

#: Виды артефактов. Значение — имя файла; вид пишется внутрь и проверяется при чтении.
KINDS: Dict[str, str] = {
    "db.schema": "db_schema.json",
    "db.effects": "db_effects.json",
    "bus.flow": "bus_flow.json",
    "containers.logs": "container_logs.json",
    "contract.openapi": "openapi.json",
    "specs.requirements": "specs.json",
    "ui.flow": "ui_flow.json",
    "load.profile": "load.json",
    "runner.history": "runs.json",
}

DEFAULT_SCOPE_KEY = "default"


@dataclass(frozen=True)
class Scope:
    """Кому принадлежит артефакт: система и контур.

    Значения по умолчанию делают однопроектный случай неотличимым от прежнего: проект,
    который про системы и контуры ничего не знает, продолжает работать.
    """

    system: str = DEFAULT_SCOPE_KEY
    env: str = DEFAULT_SCOPE_KEY

    @classmethod
    def from_env(cls) -> "Scope":
        return cls(
            system=os.getenv("ATLAS_SYSTEM") or DEFAULT_SCOPE_KEY,
            env=os.getenv("ATLAS_ENV") or DEFAULT_SCOPE_KEY,
        )

    @classmethod
    def from_doc(cls, doc: Dict[str, Any]) -> "Scope":
        raw = doc.get("scope") or {}
        return cls(
            system=str(raw.get("system") or DEFAULT_SCOPE_KEY),
            env=str(raw.get("env") or DEFAULT_SCOPE_KEY),
        )

    def as_dict(self) -> Dict[str, str]:
        return {"system": self.system, "env": self.env}

    def dir(self, artifacts_dir: Path) -> Path:
        return Path(artifacts_dir) / self.system / self.env

    def path(self, artifacts_dir: Path, kind: str) -> Path:
        return self.dir(artifacts_dir) / filename(kind)


def filename(kind: str) -> str:
    """Имя файла для вида артефакта. Неизвестный вид — ошибка на месте, а не позже."""
    try:
        return KINDS[kind]
    except KeyError:
        raise ValueError(f"неизвестный вид артефакта: {kind!r}") from None


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _atomic_write(path: Path, data: Dict[str, Any]) -> None:
    """Пишем через временный файл: параллельные воркеры не должны увидеть полузаписанный
    JSON, а прерванный прогон — оставить битый артефакт."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def read(path: Path) -> Dict[str, Any]:
    """Прочитать артефакт. Отсутствующий или битый файл — пустой словарь, не исключение:
    карта должна подниматься и на проекте, где ещё ничего не собрано."""
    path = Path(path)
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def write(
    path: Path,
    kind: str,
    payload: Dict[str, Any],
    *,
    scope: Optional[Scope] = None,
    collector: str = "",
) -> None:
    _atomic_write(
        Path(path),
        {
            "version": SCHEMA_VERSION,
            "kind": kind,
            "scope": (scope or Scope.from_env()).as_dict(),
            "collected_at": _now(),
            "collector": collector,
            **payload,
        },
    )


def validate(doc: Dict[str, Any]) -> List[str]:
    """Проверить документ артефакта на соответствие контракту. Пустой список — сходится.

    Зачем это здесь, а не у потребителя. Формат читает **чужая программа**, и она не
    обязана повторять правила: повторённое правило расходится молча, и расхождение
    обнаруживается не там, где допущено. Один источник — здесь.

    Отдельно от :func:`read`, который снисходителен нарочно: карта должна подниматься на
    проекте, где ещё ничего не собрано, поэтому битый файл он отдаёт пустым словарём.
    Снисходительность чтения и строгость проверки — разные задачи, и смешивать их нельзя:
    первая держит карту на ногах, вторая ловит того, кто пишет артефакты мимо контракта.

    Возвращает список человекочитаемых расхождений, ничего не поднимает и не печатает:
    во что обходится расхождение, решает вызывающий.
    """
    problems: List[str] = []

    if not isinstance(doc, dict):
        return [f"ожидался объект, получено {type(doc).__name__}"]
    if not doc:
        return ["документ пуст: файла нет, он нечитаем или записан не нами"]

    version = doc.get("version")
    if "version" not in doc:
        problems.append("нет ключа 'version'")
    elif not isinstance(version, int) or isinstance(version, bool):
        problems.append(f"version: ожидалось целое, получено {version!r}")
    elif version > SCHEMA_VERSION:
        problems.append(
            f"version={version} новее известной схемы {SCHEMA_VERSION}: "
            "артефакт писал кто-то новее нас, читать его вслепую нельзя"
        )

    kind = doc.get("kind")
    if "kind" not in doc:
        problems.append("нет ключа 'kind'")
    elif kind not in KINDS:
        problems.append(f"kind={kind!r} — не из набора {sorted(KINDS)}")

    scope = doc.get("scope")
    if "scope" not in doc:
        problems.append("нет ключа 'scope'")
    elif not isinstance(scope, dict):
        problems.append(f"scope: ожидался объект, получено {type(scope).__name__}")
    else:
        for field in ("system", "env"):
            if not isinstance(scope.get(field), str) or not scope.get(field):
                problems.append(f"scope.{field}: ожидалась непустая строка, получено {scope.get(field)!r}")

    if "collected_at" not in doc:
        problems.append("нет ключа 'collected_at'")
    elif not isinstance(doc["collected_at"], str) or not doc["collected_at"]:
        problems.append(f"collected_at: ожидалась непустая строка, получено {doc['collected_at']!r}")

    # `collector` может быть пустой строкой — сборщик не обязан называться. Но ключ
    # обязан быть: его отсутствие и пустое имя отвечают на разные вопросы.
    if "collector" not in doc:
        problems.append("нет ключа 'collector'")
    elif not isinstance(doc["collector"], str):
        problems.append(f"collector: ожидалась строка, получено {type(doc['collector']).__name__}")

    return problems


def append_records(
    path: Path,
    kind: str,
    key: str,
    records: List[dict],
    *,
    scope: Optional[Scope] = None,
    collector: str = "",
) -> None:
    """Дописать записи в артефакт-список, схлопывая точные повторы.

    Существующее содержимое берётся, только если вид совпал: иначе файл от другого сборщика
    молча слился бы с новым.
    """
    if not records:
        return
    path = Path(path)
    doc = read(path)
    existing: List[dict] = list(doc.get(key) or []) if doc.get("kind") == kind else []
    seen = {json.dumps(r, sort_keys=True, ensure_ascii=False) for r in existing}
    for record in records:
        signature = json.dumps(record, sort_keys=True, ensure_ascii=False)
        if signature not in seen:
            seen.add(signature)
            existing.append(record)
    write(path, kind, {key: existing}, scope=scope, collector=collector)
