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
import re
import tempfile
from dataclasses import dataclass
from dataclasses import field as _field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

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

#: Через `fullmatch`: у `re.match` с `$` проходил бы и хвостовой перевод строки (`"qa\n"`).
_SCOPE_PART_RE = re.compile(r"[A-Za-z0-9._-]+")

SCOPE_RULE_RU = ("имя области — латиница, цифры, `.`, `_`, `-`; не `.` и не `..`, без `..` "
                 "внутри")
SCOPE_RULE_EN = ("a scope name is Latin letters, digits, `.`, `_`, `-`; not `.` or `..`, no "
                 "`..` inside")


def is_unc(raw: Any) -> bool:
    """Путь — общий ресурс сети (UNC), в любом написании разделителей: `\\\\host\\share`,
    `//host/share`, `/\\host\\share`, `\\/host/share`. Решает `PureWindowsPath` после
    приведения разделителей, а не начало строки: смешанные разделители обходили проверку."""
    from pathlib import PureWindowsPath  # noqa: PLC0415

    text = str(raw).strip().replace("/", "\\")
    drive = PureWindowsPath(text).drive
    return drive.startswith("\\\\") or text.startswith("\\\\")


def _refuse_unc(path: Any, what: str) -> None:
    if is_unc(path):
        raise ValueError(t(f"{what}: пути UNC (общий ресурс сети) не принимаются",
                           f"{what}: UNC paths (network shares) are not accepted"))


def valid_scope_part(value: Any) -> bool:
    """Годится ли *value* именем системы или контура — одно правило для агента и читателя."""
    return (
        isinstance(value, str)
        and _SCOPE_PART_RE.fullmatch(value) is not None
        and value not in (".", "..")
        and ".." not in value
    )


@dataclass(frozen=True)
class Scope:
    """Кому принадлежит артефакт: система и контур.

    Значения по умолчанию делают однопроектный случай неотличимым от прежнего: проект,
    который про системы и контуры ничего не знает, продолжает работать.

    **Имя области — имя каталога, и правило его — здесь, у владельца формата**
    (:func:`valid_scope_part`): латиница, цифры, ``.``, ``_``, ``-``; не ``.``, не ``..`` и
    без ``..`` внутри. Другое имя — ``ValueError`` при создании: такой артефакт читающая
    программа не покажет, а ``..`` или разделитель вывели бы запись за каталог артефактов.
    """

    system: str = DEFAULT_SCOPE_KEY
    env: str = DEFAULT_SCOPE_KEY

    def __post_init__(self) -> None:
        for part, value in (("system", self.system), ("env", self.env)):
            if not valid_scope_part(value):
                # Значение не показывается: им бывает что угодно из чужого окружения.
                raise ValueError(t(
                    f"scope.{part}: {SCOPE_RULE_RU}",
                    f"scope.{part}: {SCOPE_RULE_EN}",
                ))

    @classmethod
    def from_env(cls) -> "Scope":
        """Область из ``ATLAS_SYSTEM``/``ATLAS_ENV``. Негодное имя — ``ValueError`` с именем
        переменной (не значением)."""
        parts = {}
        for part, name in (("system", "ATLAS_SYSTEM"), ("env", "ATLAS_ENV")):
            value = os.getenv(name) or DEFAULT_SCOPE_KEY
            if not valid_scope_part(value):
                raise ValueError(t(f"{name}: {SCOPE_RULE_RU}", f"{name}: {SCOPE_RULE_EN}"))
            parts[part] = value
        return cls(**parts)

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
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}


def read_strict(path: Path) -> Any:
    """Строгое чтение — для :func:`inspect` и команд `check`/`show`: UTF-8 с BOM или без,
    `NaN`/`Infinity` — не JSON (их не примет `JSON.parse` читающей программы). Не
    разбирается — ``{}``. Терпимый :func:`read` оставлен тем, кому надо подняться на
    проекте, где ничего не собрано; решать «сходится ли» по нему нельзя."""
    def refuse(token: str) -> Any:
        raise ValueError(token)

    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"), parse_constant=refuse)
    except Exception:
        return {}


#: Поля-идентификаторы: значения пришли из спецификации, каталога базы или брокера и
#: связывают артефакты между собой. Маскировать их нельзя — `/users/{id}/password:reset`
#: превращался в `password:***`, и путь контракта переставал совпадать с вызванным. Как
#: есть остаётся только **строка** без `=`, `;`, `@` и пробелов (:func:`_keeps_identifier`);
#: список и словарь под таким ключом (`found.table` из журналов) и строка с этими символами
#: (`/app;jsessionid=…`) маскируются обычным образом. `nodeid` сюда не входит: его
#: параметры пишет тест, и он маскируется.
IDENTIFIER_KEYS = frozenset({
    "method", "path", "rawPath", "table", "schema", "name", "column", "topic", "service",
})

#: Сколько узлов и какой глубины проходит маскировка одного артефакта. Больше — отказ до
#: записи: бомба якорей YAML в 669 байт разворачивалась в артефакт на 289 МБ.
REDACT_MAX_NODES = 500_000
REDACT_MAX_DEPTH = 64


class PayloadTooLarge(ValueError):
    """Полезная нагрузка больше потолка маскировки — артефакт не пишется."""


def _secret_keys() -> frozenset:
    from partest.redact import DEFAULT_SENSITIVE_BODY_KEYS  # noqa: PLC0415

    return frozenset(re.sub(r"[^a-z0-9]", "", k.lower()) for k in DEFAULT_SENSITIVE_BODY_KEYS)


#: Окончания секретного ключа — по образцу `partest.redact` (окончания заголовков): после
#: снятия регистра и разделителей `db_password`, `x-api-key`, `refreshToken`, `PG_DSN`,
#: `aws_credentials` — секретные. Точного совпадения со списком мало: `db_password` его не
#: давал.
SECRET_KEY_ENDINGS = (
    "password", "passwd", "pwd", "secret", "token", "apikey", "cookie", "session", "dsn",
    "credential", "credentials", "privatekey",
)


def is_secret_key(key: Any) -> bool:
    """Секретный ли ключ. После снятия регистра и разделителей: есть в
    `partest.redact.DEFAULT_SENSITIVE_BODY_KEYS`, оканчивается на одно из
    :data:`SECRET_KEY_ENDINGS` или содержит `password` где угодно (`passwordHash`) — ложное
    срабатывание здесь дешевле утечки."""
    if not isinstance(key, str):
        return False
    norm = re.sub(r"[^a-z0-9]", "", key.lower())
    if not norm:
        return False
    return norm in _secret_keys() or norm.endswith(SECRET_KEY_ENDINGS) or "password" in norm


#: Символы, при которых строка под ключом-идентификатором всё же идёт в маскировку: в пути
#: и имени таблицы их не бывает, а в `;jsessionid=…`, `relation "token=…"` и `user@host` —
#: бывают.
_UNSAFE_IDENTIFIER = re.compile(r"[=;@\s]")


def _keeps_identifier(value: Any) -> bool:
    """Строка под ключом-идентификатором остаётся как есть, только если в ней нет `=`, `;`,
    `@` и пробельных символов."""
    return isinstance(value, str) and _UNSAFE_IDENTIFIER.search(value) is None


def redact_payload(value: Any) -> Any:
    """Копия *value* для артефакта — через `partest.redact`.

    - строка — `redact_text`; ключ словаря — тоже, а совпавшие после маскировки ключи не
      сливаются молча: второй получает суффикс ` #2`, ` #3`…;
    - значение под секретным ключом (:func:`is_secret_key`) — целиком `REDACTED`;
    - под ключом из :data:`IDENTIFIER_KEYS` — как есть;
    - числа, ``None``, булевы — как есть.

    Больше :data:`REDACT_MAX_NODES` узлов или глубже :data:`REDACT_MAX_DEPTH` —
    :class:`PayloadTooLarge`.
    """
    from partest.redact import REDACTED, redact_text  # noqa: PLC0415 — только при записи

    budget = [REDACT_MAX_NODES]

    def spend(depth: int) -> None:
        budget[0] -= 1
        if budget[0] < 0 or depth > REDACT_MAX_DEPTH:
            raise PayloadTooLarge(t(
                f"полезная нагрузка больше потолка ({REDACT_MAX_NODES} узлов, глубина "
                f"{REDACT_MAX_DEPTH}) — артефакт не записан",
                f"the payload exceeds the limit ({REDACT_MAX_NODES} nodes, depth "
                f"{REDACT_MAX_DEPTH}) — the artifact was not written",
            ))

    def walk(item: Any, depth: int) -> Any:
        spend(depth)
        if isinstance(item, str):
            return redact_text(item)
        if isinstance(item, Mapping):
            out: Dict[Any, Any] = {}
            for key, val in item.items():
                if isinstance(key, str) and key in IDENTIFIER_KEYS and _keeps_identifier(val):
                    # Только строка и только «чистая»: список или словарь под таким ключом
                    # (`found.table` из журналов) идёт общим проходом — его ключи пришли из
                    # чужого текста.
                    spend(depth + 1)
                    new_key, new_val = key, val
                elif is_secret_key(key) and not isinstance(val, (bool, int, float)):
                    # Число под секретным ключом — счётчик (`found.table.user_session: 3`), а
                    # не секрет; строка, список, словарь — целиком.
                    new_key, new_val = key, (REDACTED if val not in (None, "") else val)
                else:
                    new_key = redact_text(key) if isinstance(key, str) else key
                    new_val = walk(val, depth + 1)
                if new_key in out:
                    n = 2
                    while f"{new_key} #{n}" in out:
                        n += 1
                    new_key = f"{new_key} #{n}"
                out[new_key] = new_val
            return out
        if isinstance(item, (list, tuple)):
            return [walk(v, depth + 1) for v in item]
        return item

    return walk(value, 0)


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
    redact: bool = True,
) -> Dict[str, Any]:
    """Записать артефакт атомарно.

    *collected_at* — время съёмки, если оно не «сейчас»: детерминированный набор (демо,
    тест) или наблюдение, снятое раньше записи. Строка пишется как есть, `datetime` — в
    ISO 8601; без него — текущее время с часовым поясом.

    **Всё, что попадает в артефакт, проходит `partest.redact`** (красная линия 6): каждая
    строка полезной нагрузки — значения и ключи — через `redact_text`
    (:func:`redact_payload`). Имена таблиц, путей и топиков она не меняет; меняет то, что
    выглядит секретом (`password=…`, `Bearer …`, `user:pw@host`). *redact* ``False`` —
    только для уже замаскированного (``append_records``).
    """
    if redact:
        payload = redact_payload(payload)
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
    # Возвращается записанное — уже замаскированное: печатать надо его, а не исходное.
    return payload


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

        # Только `[A-Za-z0-9._:-]`, прочее — `_`, не длиннее 64: значение уходит в каждую
        # запись и в сравнения, а приходит из чужого окружения.
        given = re.sub(r"[^A-Za-z0-9._:-]", "_", (os.getenv(RUN_ID_ENV) or "").strip())[:64]
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
            elif not valid_scope_part(doc_scope[part]):
                problems.append(t(f"scope.{part}: {SCOPE_RULE_RU}", f"scope.{part}: {SCOPE_RULE_EN}"))
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


def _run_id(record: Mapping[str, Any]) -> Optional[str]:
    run = record.get("run")
    ident = run.get("id") if isinstance(run, Mapping) else None
    return ident if isinstance(ident, str) and ident else None


def _started(record: Mapping[str, Any]) -> Optional[datetime]:
    """Начало прогона записи. Чужой файл — недоверенный ввод: любое исключение разбора
    (`OverflowError` у `0001-…` с поясом, `OSError` у `9999-…` на Windows) и время из
    будущего (позже, чем через сутки от сейчас) — «без метки»."""
    run = record.get("run")
    stamp = run.get("startedAt") if isinstance(run, Mapping) else None
    if not isinstance(stamp, str):
        return None
    try:
        when = datetime.fromisoformat(stamp)
        when = when if when.tzinfo else when.astimezone()
        if when > datetime.now(timezone.utc) + timedelta(days=1):
            return None
        when.astimezone(timezone.utc)  # переполнение на краях календаря — здесь, а не в сортировке
    except Exception:
        return None
    return when


def _link(record: Mapping[str, Any]) -> tuple:
    """Связь, которую запись описывает: тест, таблица или топик, фаза."""
    target = record.get("topic") if "topic" in record else (record.get("schema"), record.get("table"))
    # Каждое поле — сериализованной строкой: чужая запись с `{"nodeid": ["x"]}` иначе давала
    # нехешируемый ключ и навсегда блокировала запись файла.
    return tuple(json.dumps(part, sort_keys=True, default=str)
                 for part in (record.get("nodeid"), target, record.get("phase")))


def keep_last_runs(records: List[dict], keep_runs: int) -> List[dict]:
    """Записи последних *keep_runs* прогонов **плюс** последняя запись каждой связи. ``0`` —
    все.

    **Прогон — это `run.id`.** Свежесть прогона — наибольший `run.startedAt` среди его
    записей (время с поясом, не текст). Записи без метки — с непонятным или будущим
    временем, без `id` — считаются одним прогоном старше любого помеченного.

    Сверх последних прогонов остаётся последняя запись на каждую тройку (`nodeid`,
    `схема.таблица` или топик, `phase`): иначе двадцать отладочных `pytest -k` вытеснили
    бы связи всех остальных тестов, а связь теста с таблицей — то, что читатель строит.
    Порядок оставшихся не меняется.
    """
    if keep_runs <= 0:
        return list(records)
    oldest = datetime.min.replace(tzinfo=timezone.utc)
    fresh: Dict[Optional[str], datetime] = {}
    for record in records:
        ident = _run_id(record)
        when = _started(record) or oldest if ident is not None else oldest
        fresh[ident] = max(fresh.get(ident, oldest), when, key=lambda d: d.astimezone(timezone.utc))
    order = sorted(fresh, key=lambda i: (fresh[i].astimezone(timezone.utc), i is not None),
                   reverse=True)
    kept_runs = set(order[:keep_runs])
    last_of_link: Dict[tuple, int] = {}
    for index, record in enumerate(records):
        last_of_link[_link(record)] = index
    keep_index = set(last_of_link.values())
    return [r for i, r in enumerate(records) if _run_id(r) in kept_runs or i in keep_index]


def append_records(
    path: Path,
    kind: str,
    key: str,
    records: List[dict],
    *,
    scope: Optional[Scope] = None,
    collector: str = "",
    collected_at: Any = None,
    keep_runs: int = 0,
) -> None:
    """Дописать записи в артефакт-список, схлопывая точные повторы.

    Существующее содержимое берётся, только если вид совпал: иначе файл от другого сборщика
    молча слился бы с новым. *keep_runs* больше нуля — после дописки остаются записи
    последних стольких прогонов (:func:`keep_last_runs`): с меткой прогона записи разных
    прогонов не схлопываются, и без предела файл растёт с каждым прогоном.
    """
    if not records:
        return
    path = Path(path)
    doc = read(path)
    prior = doc.get(key) if isinstance(doc, dict) and doc.get("kind") == kind else None
    # Чужой файл — недоверенный ввод: из прежнего берутся только записи-словари.
    existing: List[dict] = [r for r in prior if isinstance(r, dict)] if isinstance(prior, list) else []
    seen = {json.dumps(r, sort_keys=True, ensure_ascii=False) for r in existing}
    # Маскировка — до сравнения: прежние записи уже прошли её при записи.
    for record in redact_payload(list(records)):
        signature = json.dumps(record, sort_keys=True, ensure_ascii=False)
        if signature not in seen:
            seen.add(signature)
            existing.append(record)
    if keep_runs and keep_runs > 0:
        existing = keep_last_runs(existing, keep_runs)
    write(path, kind, {key: existing}, scope=scope, collector=collector,
          collected_at=collected_at, redact=False)


def discover_scopes(artifacts_dir: Path) -> List[Scope]:
    """Области, для которых на диске лежит хотя бы один артефакт известного вида.

    Раскладка — ``<artifacts_dir>/<система>/<контур>/<файл>``; порядок — по именам
    каталогов. Каталог без единого файла известного вида областью не считается.
    """
    _refuse_unc(artifacts_dir, "artifacts_dir")
    known = set(KINDS.values())
    base = Path(artifacts_dir)
    out: List[Scope] = []
    if not base.is_dir():
        return out
    for system in sorted(p for p in base.iterdir() if p.is_dir() and valid_scope_part(p.name)):
        for env in sorted(p for p in system.iterdir() if p.is_dir() and valid_scope_part(p.name)):
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
    filename(kind)  # неизвестный вид — ValueError всегда, а не только на найденном файле
    _refuse_unc(path, "inspect")
    path = Path(path)
    if not path.is_file():
        return Inspection(kind, path, MISSING)
    doc = read_strict(path)
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
    system, env = path.parent.parent.name, path.parent.name
    if not (valid_scope_part(system) and valid_scope_part(env)):
        return None
    return Scope(system=system, env=env)


def kind_of(path: Path) -> Optional[str]:
    """Вид по имени файла; ``None`` — имя не из контракта."""
    name = Path(path).name
    return next((k for k, v in KINDS.items() if v == name), None)


__all__ = [
    "DEFAULT_SCOPE_KEY",
    "SCOPE_RULE_EN",
    "SCOPE_RULE_RU",
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
    "IDENTIFIER_KEYS",
    "PayloadTooLarge",
    "inspect",
    "SECRET_KEY_ENDINGS",
    "is_secret_key",
    "is_unc",
    "keep_last_runs",
    "redact_payload",
    "kind_of",
    "read",
    "read_strict",
    "run_label",
    "scope_of",
    "valid_scope_part",
    "validate",
    "write",
]
