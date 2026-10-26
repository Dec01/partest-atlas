"""Контракт артефактов: версионированные JSON рядом с прогоном.

Каждый сборщик пишет один файл со своей схемой. Версия и вид несутся в самом файле, чтобы
потребитель мог честно сказать «артефакт старого формата», а не упасть на отсутствующем
поле.

Списки записей дописываются, а не перезаписываются вслепую: повторный прогон добавляет
своё к прежнему. Запись атомарна (полузаписанный файл никто не увидит), но дописывание —
чтение, слияние и замена **без блокировки**: два процесса, дописывающие один файл
одновременно, потеряют записи одного из них. Поэтому под pytest-xdist плагины `db_effects`
и `bus_flow` пишут только с контроллера; `ui_flow` пишет с каждого воркера целиком
(`write`), и под xdist в файле остаются связи последнего из них.

Область действия
----------------
Артефакт принадлежит паре **(система, контур)**. Система — самостоятельный продукт
заказчика (интернет-магазин, учётная система, склад), контур — среда, в которой она
развёрнута (тест, препрод). Раскладка на диске повторяет эту пару::

    atlas/<система>/<контур>/db_schema.json

Область в файле обязана совпадать с каталогом, в котором файл лежит, а вид — с именем
файла: :func:`validate` с ``kind=``/``scope=`` и :func:`inspect` это сверяют.

Версия 1
--------
Артефакты первой версии (без области действия) **не проходят** :func:`validate`: у них нет
``scope``, и схема у них старше. Их снимают заново агентом — переносить и править руками
не нужно. :meth:`Scope.from_doc` читает отсутствующую область как ``default/default``, но
это только чтение шапки, а не обещание, что такой файл годится как данные.

Публичный интерфейс
-------------------
Модуль — контракт формата: имена из ``__all__`` меняются только с записью в
``CHANGELOG.md``, формат файла — только с повышением :data:`SCHEMA_VERSION`.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from dataclasses import field as _field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from partest_atlas.lang import t

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

#: Ключ полезной нагрузки у каждого вида: под ним лежат записи, их число — размер
#: артефакта. Часть контракта: потребитель не держит свою таблицу. `load.profile` здесь
#: нет — формат вида не согласован, и его пока никто не пишет.
PAYLOAD_KEYS: Dict[str, str] = {
    "db.schema": "databases",
    "db.effects": "records",
    "bus.flow": "records",
    "containers.logs": "containers",
    "contract.openapi": "operations",
    "specs.requirements": "requirements",
    "ui.flow": "links",
    "runner.history": "runs",
}

#: Состояния артефакта у :func:`inspect` — машинные коды.
OK, MISSING, BROKEN, NEWER, INVALID = "ok", "missing", "broken", "newer", "invalid"
STATES = (OK, MISSING, BROKEN, NEWER, INVALID)

#: Переменная окружения с меткой прогона, если её задаёт тот, кто запускает прогон.
RUN_ID_ENV = "ATLAS_RUN_ID"

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


def _collected_at(value: Any) -> str:
    """Время съёмки для шапки: строка как есть, `datetime` — ISO, ничего — сейчас."""
    if value is None:
        return _now()
    if isinstance(value, datetime):
        stamp = value if value.tzinfo else value.astimezone()
        return stamp.isoformat(timespec="seconds")
    if isinstance(value, str) and value.strip():
        return value
    raise ValueError(t(f"collected_at={value!r}: ожидалась непустая строка или datetime",
                       f"collected_at={value!r}: expected a non-empty string or a datetime"))


def write(
    path: Path,
    kind: str,
    payload: Dict[str, Any],
    *,
    scope: Optional[Scope] = None,
    collector: str = "",
    collected_at: Any = None,
) -> None:
    """Записать артефакт атомарно.

    *collected_at* — время съёмки, если оно не «сейчас»: детерминированный набор (демо,
    тест) или наблюдение, снятое раньше записи. Строка пишется как есть, `datetime` — в
    ISO 8601; без него — текущее время с часовым поясом.
    """
    _atomic_write(
        Path(path),
        {
            "version": SCHEMA_VERSION,
            "kind": kind,
            "scope": (scope or Scope.from_env()).as_dict(),
            "collected_at": _collected_at(collected_at),
            "collector": collector,
            **payload,
        },
    )


_run: Dict[str, str] = {}


def run_label() -> Dict[str, str]:
    """Метка текущего прогона: ``{"id": …, "startedAt": …}``, одна на процесс.

    Пишется в каждую запись `db.effects` и `bus.flow`: без неё эффект не связать с
    прогоном, у связи нет возраста, а после очистки отчётов структура снова равна
    последнему прогону. ``id`` — из ``ATLAS_RUN_ID``, если её задал тот, кто запускает
    прогон (конвейер, сервер карты), иначе случайный. Значение непрозрачно: сравнивать
    можно только на равенство.
    """
    if not _run:
        import secrets  # noqa: PLC0415

        given = (os.getenv(RUN_ID_ENV) or "").strip()[:64]
        _run["id"] = given or secrets.token_hex(6)
        _run["startedAt"] = _now()
    return dict(_run)


def validate(
    doc: Dict[str, Any], *, kind: Optional[str] = None, scope: Optional[Scope] = None
) -> List[str]:
    """Проверить документ артефакта на соответствие контракту. Пустой список — сходится.

    Зачем это здесь, а не у потребителя. Формат читает **чужая программа**, и она не
    обязана повторять правила: повторённое правило расходится молча, и расхождение
    обнаруживается не там, где допущено. Один источник — здесь.

    Отдельно от :func:`read`, который снисходителен нарочно: карта должна подниматься на
    проекте, где ещё ничего не собрано, поэтому битый файл он отдаёт пустым словарём.
    Снисходительность чтения и строгость проверки — разные задачи, и смешивать их нельзя:
    первая держит карту на ногах, вторая ловит того, кто пишет артефакты мимо контракта.

    *kind* — вид, которого ждут по имени файла; *scope* — область по каталогу, в котором
    файл лежит. Переданы — сверяются с документом: файл схемы под именем эффектов и файл
    чужого контура в этом каталоге — расхождения, а не годные данные.

    Возвращает список человекочитаемых расхождений, ничего не поднимает и не печатает:
    во что обходится расхождение, решает вызывающий. Тексты — для человека, сверять их
    машиной не следует; машинное решение — :func:`inspect`.
    """
    problems: List[str] = []

    if not isinstance(doc, dict):
        return [t(f"ожидался объект, получено {type(doc).__name__}",
                  f"expected an object, got {type(doc).__name__}")]
    if not doc:
        return [t("документ пуст: файла нет, он нечитаем или записан не нами",
                  "empty document: the file is missing, unreadable or not written by us")]

    def missing(key: str) -> str:
        return t(f"нет ключа '{key}'", f"no '{key}' key")

    def not_text(key: str, value: Any) -> str:
        return t(f"{key}: ожидалась непустая строка, получено {value!r}",
                 f"{key}: expected a non-empty string, got {value!r}")

    version = doc.get("version")
    if "version" not in doc:
        problems.append(missing("version"))
    elif not isinstance(version, int) or isinstance(version, bool):
        problems.append(t(f"version: ожидалось целое, получено {version!r}",
                          f"version: expected an integer, got {version!r}"))
    elif version > SCHEMA_VERSION:
        problems.append(t(
            f"version={version} новее известной схемы {SCHEMA_VERSION}: "
            "артефакт писал кто-то новее нас, читать его вслепую нельзя",
            f"version={version} is newer than the known schema {SCHEMA_VERSION}: "
            "a newer writer made this artifact, reading it blindly is unsafe",
        ))
    elif version < SCHEMA_VERSION:
        problems.append(t(
            f"version={version} старше схемы {SCHEMA_VERSION}: снимите артефакт заново "
            "агентом — править старые файлы руками не нужно",
            f"version={version} is older than schema {SCHEMA_VERSION}: collect the artifact "
            "again with the agent — old files need no manual editing",
        ))

    doc_kind = doc.get("kind")
    if "kind" not in doc:
        problems.append(missing("kind"))
    elif doc_kind not in KINDS:
        problems.append(t(f"kind={doc_kind!r} — не из набора {sorted(KINDS)}",
                          f"kind={doc_kind!r} is not one of {sorted(KINDS)}"))
    elif kind is not None and doc_kind != kind:
        problems.append(t(
            f"kind={doc_kind!r} лежит в файле вида {kind!r} ({filename(kind)})",
            f"kind={doc_kind!r} is in the file of kind {kind!r} ({filename(kind)})",
        ))

    doc_scope = doc.get("scope")
    if "scope" not in doc:
        problems.append(missing("scope"))
    elif not isinstance(doc_scope, dict):
        problems.append(t(f"scope: ожидался объект, получено {type(doc_scope).__name__}",
                          f"scope: expected an object, got {type(doc_scope).__name__}"))
    else:
        bad = False
        for part in ("system", "env"):
            if not isinstance(doc_scope.get(part), str) or not doc_scope.get(part):
                problems.append(not_text(f"scope.{part}", doc_scope.get(part)))
                bad = True
        if not bad and scope is not None and Scope.from_doc(doc) != scope:
            problems.append(t(
                f"в файле scope={doc_scope['system']}/{doc_scope['env']}, а лежит он в "
                f"{scope.system}/{scope.env}",
                f"the file says scope={doc_scope['system']}/{doc_scope['env']}, but it lies "
                f"in {scope.system}/{scope.env}",
            ))

    if "collected_at" not in doc:
        problems.append(missing("collected_at"))
    elif not isinstance(doc["collected_at"], str) or not doc["collected_at"]:
        problems.append(not_text("collected_at", doc["collected_at"]))

    # `collector` может быть пустой строкой — сборщик не обязан называться. Но ключ
    # обязан быть: его отсутствие и пустое имя отвечают на разные вопросы.
    if "collector" not in doc:
        problems.append(missing("collector"))
    elif not isinstance(doc["collector"], str):
        problems.append(t(f"collector: ожидалась строка, получено {type(doc['collector']).__name__}",
                          f"collector: expected a string, got {type(doc['collector']).__name__}"))

    return problems


def append_records(
    path: Path,
    kind: str,
    key: str,
    records: List[dict],
    *,
    scope: Optional[Scope] = None,
    collector: str = "",
    collected_at: Any = None,
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
    write(path, kind, {key: existing}, scope=scope, collector=collector,
          collected_at=collected_at)


def discover_scopes(artifacts_dir: Path) -> List[Scope]:
    """Области, для которых на диске лежит хотя бы один артефакт известного вида.

    Раскладка — ``<artifacts_dir>/<система>/<контур>/<файл>``; порядок — по именам
    каталогов. Каталог без единого файла известного вида областью не считается.
    """
    known = set(KINDS.values())
    base = Path(artifacts_dir)
    out: List[Scope] = []
    if not base.is_dir():
        return out
    for system in sorted(p for p in base.iterdir() if p.is_dir()):
        for env in sorted(p for p in system.iterdir() if p.is_dir()):
            if any((env / name).is_file() for name in known):
                out.append(Scope(system=system.name, env=env.name))
    return out


@dataclass
class Inspection:
    """Решение об одном артефакте: состояние машинным кодом и почему.

    ``state`` — одно из :data:`STATES`:

    ``ok``       прочитан, сходится с контрактом; данные — в :attr:`doc`;
    ``missing``  файла нет — законное состояние проекта, который этого ещё не собирал;
    ``broken``   файл есть, но не разбирается как JSON-объект (или пуст);
    ``newer``    схема новее известной — читать вслепую нельзя;
    ``invalid``  контракт не сходится (в том числе схема 1, чужой вид, чужая область).

    :attr:`doc` заполнен только при ``ok``: строить что-то из файла, который владелец
    формата не понимает, нельзя.
    """

    kind: str
    path: Path
    state: str
    problems: List[str] = _field(default_factory=list)
    warnings: List[str] = _field(default_factory=list)
    version: Any = None
    collected_at: str = ""
    collector: str = ""
    #: Число записей под :data:`PAYLOAD_KEYS`; ``None`` — ключа нет или это не список.
    records: Optional[int] = None
    doc: Dict[str, Any] = _field(default_factory=dict)

    @property
    def usable(self) -> bool:
        return self.state == OK


def inspect(path: Path, kind: str, *, scope: Optional[Scope] = None) -> Inspection:
    """Прочитать артефакт вида *kind* и вынести решение о нём.

    *scope* — область по каталогу, в котором файл лежит; передана — сверяется с файлом
    (:func:`validate`). Предупреждение, не расхождение: нет ключа полезной нагрузки — шапка
    сходится, а данных нет.
    """
    path = Path(path)
    if not path.is_file():
        return Inspection(kind, path, MISSING)
    doc = read(path)
    if not isinstance(doc, dict) or not doc:
        return Inspection(kind, path, BROKEN, problems=[t(
            "файл есть, но не разбирается как JSON-объект (или пуст)",
            "the file exists but does not parse as a JSON object (or is empty)",
        )])
    found = Inspection(
        kind, path, OK,
        version=doc.get("version"),
        collected_at=str(doc.get("collected_at") or ""),
        collector=str(doc.get("collector") or ""),
    )
    problems = validate(doc, kind=kind, scope=scope)
    version = doc.get("version")
    if isinstance(version, int) and not isinstance(version, bool) and version > SCHEMA_VERSION:
        found.state, found.problems = NEWER, problems
        return found
    if problems:
        found.state, found.problems = INVALID, problems
        return found
    key = PAYLOAD_KEYS.get(kind)
    value = doc.get(key) if key else None
    if key is not None and key not in doc:
        found.warnings.append(t(
            f"нет ключа полезной нагрузки {key!r} — шапка сходится, но данных нет",
            f"no payload key {key!r} — the header conforms, but there is no data",
        ))
    found.records = len(value) if isinstance(value, (list, dict)) else None
    found.doc = doc
    return found


def scope_of(path: Path) -> Optional[Scope]:
    """Область по раскладке ``…/<система>/<контур>/<файл>`` — если имя файла известного
    вида. Иначе ``None``: по пути не сказать, чья это область."""
    path = Path(path)
    if path.name not in KINDS.values() or len(path.parts) < 3:
        return None
    return Scope(system=path.parent.parent.name, env=path.parent.name)


def kind_of(path: Path) -> Optional[str]:
    """Вид по имени файла; ``None`` — имя не из контракта."""
    name = Path(path).name
    return next((k for k, v in KINDS.items() if v == name), None)


__all__ = [
    "DEFAULT_SCOPE_KEY",
    "Inspection",
    "KINDS",
    "PAYLOAD_KEYS",
    "RUN_ID_ENV",
    "SCHEMA_VERSION",
    "STATES",
    "Scope",
    "append_records",
    "discover_scopes",
    "filename",
    "inspect",
    "kind_of",
    "read",
    "run_label",
    "scope_of",
    "validate",
    "write",
]
