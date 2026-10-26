"""Объявленный контракт → артефакт `contract.openapi`.

Зачем. Сбор на прогоне знает только то, что видел: какие операции дёрнули тесты. Контракт
знает, что **объявлено** — какие поля обязательны, какие коды заявлены, какой контроллер
за путь отвечает. Разница между объявленным и увиденным и есть то, ради чего это
снимается.

Честно об ограничении: **описаний в контракте может не быть вовсе.** На одном боевом
стенде из семидесяти одной операции ни одна не несла ни `summary`, ни `description` —
генератор их не проставляет. Поля в артефакте всё равно есть и всё равно пустые: их
отсутствие — наблюдение о системе, а не повод притвориться, что описания собраны.

Разбор — чистая функция :func:`parse` над документом: сети в ней нет, и весь разбор
проверяется обычным тестом.

Откуда документ, решает проект, а не пакет: :func:`load_from_confpartest` берёт его из
`confpartest.swagger_files` — того же места, откуда покрытие считает partest, — а загружает
и разбирает его partest (`partest.openapi.resolve_swagger`): одна реализация на семейство.
Файл или адрес, объявленный проектом, читается в окружении проекта; сам пакет никуда не
ходит. В артефакт попадает, **откуда** взят документ (:func:`source_of`): адрес — через
`partest.redact.redact_url`.

Служб в `swagger_files` бывает несколько, а артефакт у области один: операции всех служб
лежат вместе с ключом `service`, по службе — запись в `services` (:func:`assemble`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple, Union

from partest_atlas import artifacts
from partest_atlas.artifacts import Scope
from partest_atlas.config import Config, load
from partest_atlas.lang import t

KIND = "contract.openapi"

METHODS = ("get", "post", "put", "patch", "delete", "head", "options")

#: Идентификатор в пути заменяется на `{id}`: иначе `/items/1` и `/items/2` выглядели бы
#: как разные операции, и карта распухала бы на каждый прогон.
_ID_RE = re.compile(r"\{[^}]*\}")

#: Сколько полей тела оставляем. Схемы бывают в сотни полей, и в карте их всё равно не
#: читают — а артефакт от них растёт на мегабайты.
FIELDS_LIMIT = 60

#: Глубина разворота `$ref`. Один уровень — всё, что нужно; глубже начинается рекурсия
#: по взаимным ссылкам, которая разворачивается бесконечно.
MAX_REF_DEPTH = 4


def norm_path(path: str) -> str:
    """Путь с обезличенными параметрами: `/items/{itemId}` → `/items/{id}`."""
    return _ID_RE.sub("{id}", path or "")


def _deref(schema: Optional[dict], schemas: Dict[str, dict], depth: int = 0) -> dict:
    """Развернуть `$ref` на один уровень: глубже не нужно и опасно."""
    ref = (schema or {}).get("$ref") or ""
    if isinstance(ref, str) and ref.startswith("#/components/schemas/") and depth < MAX_REF_DEPTH:
        return schemas.get(ref.rsplit("/", 1)[-1]) or {}
    return schema or {}


#: Потолки разбора. Спецификация — недоверенный ввод: якоря YAML разворачивают 669 байт в
#: сотни мегабайт, а длинная строка или чужой тип в поле дошли бы до артефакта как есть.
TEXT_LIMIT = 2000
PARAMS_LIMIT = 200
RESPONSES_LIMIT = 100
TAGS_LIMIT = 50


def _text(value: Any) -> str:
    """Строка с потолком длины; число — его `str`; `None`, список, словарь — пусто.
    `str()` контейнера не берётся: у списка, развёрнутого якорями YAML, это сотни мегабайт."""
    if isinstance(value, str):
        return value[:TEXT_LIMIT]
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)[:TEXT_LIMIT]
    return ""


def _dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _body(op: dict, schemas: Dict[str, dict]) -> Optional[dict]:
    """Тело запроса. ``None``, когда тела нет — пустой объект читался бы как «есть, но
    пустое», а это разные утверждения."""
    request_body = _dict(op.get("requestBody"))
    content = _dict(request_body.get("content"))
    media = _dict(content.get("application/json") or next(iter(content.values()), {}))
    raw = _dict(media.get("schema"))
    ref = raw.get("$ref")
    name = _text(ref).rsplit("/", 1)[-1] if isinstance(ref, str) else ""
    resolved = _dict(_deref(raw, schemas))
    fields = [_text(f) for f in list(_dict(resolved.get("properties")))[:FIELDS_LIMIT]]
    if not name and not fields:
        return None
    required = resolved.get("required")
    return {
        "schema": name or None,
        "required": bool(request_body.get("required")),
        "requiredFields": sorted(_text(f) for f in (required if isinstance(required, list) else [])
                                 if isinstance(f, str))[:FIELDS_LIMIT],
        "fields": fields,
    }


def parse(document: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Контракт → плоский список операций. Сети здесь нет."""
    schemas = _dict(_dict(document.get("components")).get("schemas"))
    out: List[Dict[str, Any]] = []
    for path, item in _dict(document.get("paths")).items():
        if not isinstance(item, dict) or not isinstance(path, str):
            continue
        path = path[:TEXT_LIMIT]
        # Параметры уровня пути принадлежат каждой операции под ним. Потерять их значит
        # объявить обязательный параметр необязательным.
        shared = item.get("parameters") if isinstance(item.get("parameters"), list) else []
        for method in METHODS:
            op = item.get(method)
            if not isinstance(op, dict):
                continue
            own = op.get("parameters") if isinstance(op.get("parameters"), list) else []
            params = [
                {"name": _text(p.get("name")), "in": _text(p.get("in")) or None,
                 "required": bool(p.get("required"))}
                for p in [*shared, *own]
                if isinstance(p, dict) and p.get("name")
            ][:PARAMS_LIMIT]
            tags = op.get("tags") if isinstance(op.get("tags"), list) else []
            operation_id = op.get("operationId")
            out.append(
                {
                    "method": method.upper(),
                    "path": norm_path(path),
                    "rawPath": path,
                    "operationId": _text(operation_id) if operation_id is not None else None,
                    "tags": [t[:TEXT_LIMIT] for t in tags if isinstance(t, str)][:TAGS_LIMIT],
                    "deprecated": bool(op.get("deprecated")),
                    # Описаний тут может не быть ни одного. Поле остаётся: на другом
                    # проекте они есть, и код не должен узнавать об этом заново.
                    "summary": _text(op.get("summary")),
                    "description": _text(op.get("description")),
                    "params": params,
                    "body": _body(op, schemas),
                    "responses": sorted(_text(k) for k in _dict(op.get("responses")))[:RESPONSES_LIMIT],
                }
            )
    return out


def summarize(operations: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Сводка рядом с операциями: по ней видно, что контракт вообще отдаёт.

    `withDescription` здесь не украшение — именно по нему видно, что описаний в
    контракте нет, и что искать их надо в другом источнике.
    """
    return {
        "operations": len(operations),
        "paths": len({op["path"] for op in operations}),
        "tags": sorted({t for op in operations for t in op["tags"]}),
        "deprecated": sum(1 for op in operations if op["deprecated"]),
        "withDescription": sum(1 for op in operations if op["summary"] or op["description"]),
    }


def info_of(document: Mapping[str, Any]) -> Dict[str, Optional[str]]:
    """`info.title` и `info.version` контракта — строкой или ``None``, если не объявлены."""
    raw = document.get("info")
    info = raw if isinstance(raw, Mapping) else {}

    def text(key: str) -> Optional[str]:
        value = info.get(key)
        return None if value is None or isinstance(value, (dict, list)) else _text(value)

    return {"title": text("title"), "version": text("version")}


def build(document: Dict[str, Any], *, source: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Содержание артефакта. `info` — всегда; `source` — если известно, откуда документ."""
    operations = parse(document)
    payload: Dict[str, Any] = {
        "operations": operations,
        "stats": summarize(operations),
        "info": info_of(document),
    }
    if source is not None:
        payload["source"] = source
    return payload


# --- источник из confpartest -------------------------------------------------------


class SourceError(RuntimeError):
    """Спецификацию взять неоткуда или не удалось: сообщение уже без секретов."""


#: Сколько ждать документ по адресу: на шаг (соединение, каждое чтение тела).
FETCH_TIMEOUT_SEC = 60.0

#: Предел, который `resolve_swagger` проверяет **между шагами** загрузки (между кусками
#: тела). Это не общий предел времени: разрешение имени (DNS) и каждый отдельный шаг он не
#: прерывает — шаг ограничен только :data:`FETCH_TIMEOUT_SEC`. Медленную отдачу он
#: обрывает, зависший DNS — нет.
FETCH_TOTAL_TIMEOUT_SEC = 180.0

#: Сколько переадресаций идти. Адрес спецификации объявлен проектом, долгой цепочки у него
#: не бывает; двадцать (умолчание httpx) — уже признак петли.
MAX_REDIRECTS = 5


def _redact_text(text: str) -> str:
    from partest.redact import redact_text  # noqa: PLC0415 — partest нужен только здесь

    return redact_text(text)


def _redact_url(url: str) -> str:
    from partest.redact import redact_url  # noqa: PLC0415

    return redact_url(url)


def source_of(service: str, kind: str, location: str) -> Dict[str, Any]:
    """Запись `source`: служба из `swagger_files`, вид и место — без секретов.

    Адрес — через `partest.redact.redact_url`: без учётных данных, строки запроса и якоря,
    а при сомнении (сырые `/ ? #` в пароле) — `scheme://<redacted>`. Локальный путь
    пишется **как объявлен** в `confpartest.py`, а не разрешённым (полный путь на машине
    сборки — чужой каталог пользователя), и через `redact_text`.
    """
    if kind == "url":
        return {"service": service, "type": "url", "location": _redact_url(location)}
    return {"service": service, "type": "local", "location": _redact_text(location)}


@dataclass(frozen=True)
class Loaded:
    """Документ одной службы и то, откуда он взят."""

    service: str
    document: Dict[str, Any]
    source: Dict[str, Any]


def _confpartest(root: Union[str, Path]) -> Any:
    """`confpartest.py` из *root* — и только он.

    `partest.load_confpartest` берёт модуль, уже импортированный, или первый найденный в
    `sys.path`. Чужой `confpartest` с тем же именем выполнился бы, а его контракт лёг бы в
    артефакт без единого предупреждения. Поэтому каталог проекта ставится в `sys.path`
    первым, а найденный модуль сверяется с ожидаемым файлом **до** импорта.
    """
    import importlib.util  # noqa: PLC0415
    import sys  # noqa: PLC0415

    from partest import load_confpartest  # noqa: PLC0415 — partest нужен только здесь
    from partest.conf import ConfpartestError  # noqa: PLC0415

    base = Path(root).resolve()
    expected = base / "confpartest.py"
    if not expected.is_file():
        raise SourceError(t(
            f"в {base} нет confpartest.py — укажите его каталог ключом --root",
            f"there is no confpartest.py in {base} — name its directory with --root",
        ))
    loaded = sys.modules.get("confpartest")
    if loaded is not None:
        origin = getattr(loaded, "__file__", None)
        if not origin or Path(origin).resolve() != expected:
            raise SourceError(t(
                f"в процессе уже загружен другой confpartest ({origin}) — нужен {expected}",
                f"another confpartest is already loaded in the process ({origin}) — "
                f"{expected} is needed",
            ))
    else:
        text = str(base)
        while text in sys.path:
            sys.path.remove(text)
        sys.path.insert(0, text)
        try:
            spec = importlib.util.find_spec("confpartest")
        except Exception:  # pragma: no cover - сломанный родительский пакет
            spec = None
        origin = getattr(spec, "origin", None)
        if not origin or Path(origin).resolve() != expected:
            raise SourceError(t(
                f"под именем confpartest найден чужой файл ({origin}) вместо {expected} — "
                "не импортирую",
                f"the name confpartest resolves to a foreign file ({origin}) instead of "
                f"{expected} — not importing it",
            ))
    try:
        conf = load_confpartest(project_root=base)
    except ConfpartestError as exc:
        raise SourceError(t(
            f"confpartest.py не загружен из {base}: {_redact_text(str(exc))}",
            f"confpartest.py was not loaded from {base}: {_redact_text(str(exc))}",
        )) from None
    origin = getattr(conf, "__file__", None)
    if not origin or Path(origin).resolve() != expected:
        raise SourceError(t(
            f"загружен не тот confpartest: {origin} вместо {expected}",
            f"the wrong confpartest was loaded: {origin} instead of {expected}",
        ))
    return conf


def _entries(conf: Any) -> Dict[str, Any]:
    """`swagger_files` — «служба → запись», разобранная грамматикой partest
    (`partest.conf.parse_swagger_entry`): одно прочтение формы на семейство."""
    from partest.conf import ConfpartestError, parse_swagger_entry  # noqa: PLC0415

    swagger = getattr(conf, "swagger_files", None)
    if not isinstance(swagger, Mapping) or not swagger:
        raise SourceError(t(
            "в confpartest.py нет swagger_files или он пуст — "
            "ожидалось {'имя': ['local'|'url', путь_или_адрес]}",
            "confpartest.py has no swagger_files or it is empty — "
            "expected {'name': ['local'|'url', path_or_url]}",
        ))
    out: Dict[str, Any] = {}
    for name, entry in swagger.items():
        shown = _redact_text(str(name))
        try:
            out[str(name)] = parse_swagger_entry(entry)
        except ConfpartestError as exc:
            # Сообщение partest места не цитирует; имя службы — через redact на всякий случай.
            raise SourceError(f"swagger_files[{shown!r}]: {_redact_text(str(exc))}") from None
    return out


def _resolved_from(location: str, placed: str, base: Path, notes: List[str], shown: str) -> Optional[str]:
    """Откуда взят относительный локальный путь: ``cwd`` или ``confpartest``. Абсолютный или
    адрес — ``None``. Если файл с этим путём есть и от текущего каталога, и от каталога
    `confpartest.py`, и это разные файлы с разным содержимым, — заметка: карта и покрытие
    partest читают первый, а человек мог ждать второй."""
    rel = Path(location)
    if rel.is_absolute():
        return None
    in_cwd = (Path.cwd() / rel).resolve()
    in_conf = (base / rel).resolve()
    chosen = Path(placed).resolve()
    if in_cwd != in_conf and in_cwd.is_file() and in_conf.is_file():
        try:
            differ = in_cwd.read_bytes() != in_conf.read_bytes()
        except OSError:
            differ = True
        if differ:
            notes.append(t(
                f"{shown}: файл есть и от текущего каталога, и от каталога confpartest.py, и они "
                "различаются — взят от текущего (правило partest)",
                f"{shown}: the file exists both from the current directory and from the "
                "directory of confpartest.py, and they differ — the current one is taken "
                "(the partest rule)",
            ))
    if chosen == in_cwd:
        return "cwd"
    if chosen == in_conf:
        return "confpartest"
    return None


def _resolve(entry: Any, base: Path, shown: str, notes: List[str]) -> Tuple[Dict[str, Any], Optional[str]]:
    """Документ записи — `partest.openapi.resolve_swagger` с потолками размера и времени — и
    откуда взят локальный относительный путь (:func:`_resolved_from`)."""
    from partest.conf import resolve_entry_location  # noqa: PLC0415
    from partest.openapi import OpenApiResolveError, resolve_swagger  # noqa: PLC0415

    # Относительный локальный путь — по правилу partest (как у его покрытия): от текущего
    # каталога, если там есть файл, иначе от каталога `confpartest.py`.
    placed = resolve_entry_location((entry.kind, entry.location), conf_dir=base)
    resolved_from = (
        _resolved_from(entry.location, placed.location, base, notes, shown)
        if entry.kind == "local" else None
    )
    try:
        document = resolve_swagger(
            (placed.kind, placed.location),
            project_root=base,
            timeout=FETCH_TIMEOUT_SEC,
            total_timeout=FETCH_TOTAL_TIMEOUT_SEC,
            max_redirects=MAX_REDIRECTS,
        )
    except OpenApiResolveError as exc:
        # partest уже убрал из сообщения учётные данные и строку запроса; redact — вторым
        # слоем. Путь к файлу в сообщении — машины сборки, в артефакт он не попадает.
        raise SourceError(t(
            f"{shown}: спецификация не получена — {_redact_text(str(exc))}",
            f"{shown}: the specification was not obtained — {_redact_text(str(exc))}",
        )) from None
    if not isinstance(document, dict) or ("openapi" not in document and "swagger" not in document):
        raise SourceError(t(
            f"{shown}: не похоже на OpenAPI — нет ключа openapi или swagger",
            f"{shown}: does not look like OpenAPI — no openapi or swagger key",
        ))
    return document, resolved_from


def _load(
    root: Union[str, Path], service: Optional[str], notes: List[str]
) -> Tuple[List[str], List[Loaded]]:
    """``(службы swagger_files по порядку, загруженные)`` — см. :func:`load_from_confpartest`."""
    conf = _confpartest(root)
    base = Path(getattr(conf, "__file__")).resolve().parent
    entries = _entries(conf)
    if service is not None and service not in entries:
        raise SourceError(t(
            f"службы {_redact_text(service)!r} нет в swagger_files; есть: "
            f"{', '.join(_redact_text(n) for n in entries)}",
            f"service {_redact_text(service)!r} is not in swagger_files; known: "
            f"{', '.join(_redact_text(n) for n in entries)}",
        ))
    names = [service] if service is not None else list(entries)
    out: List[Loaded] = []
    for name in names:
        entry = entries[name]
        source = source_of(name, entry.kind, entry.location)
        document, resolved_from = _resolve(
            entry, base, f"{source['service']} ({source['location']})", notes)
        if resolved_from is not None:
            source["resolvedFrom"] = resolved_from
        out.append(Loaded(name, document, source))
    return list(entries), out


def load_from_confpartest(
    root: Union[str, Path], *, service: Optional[str] = None, notes: Optional[List[str]] = None
) -> List[Loaded]:
    """Спецификации, объявленные в `confpartest.swagger_files` проекта в *root*.

    Без *service* — все службы, в порядке объявления; с *service* — только она. Загрузка и
    разбор — `partest.openapi.resolve_swagger` (под политикой TLS проекта, с потолком
    размера, времени шага, времени между шагами и числа переадресаций). Любая неудача —
    :class:`SourceError` без секретов, и ни одна служба тогда не возвращается: частичный
    контракт выглядел бы как полный.

    Локальный относительный путь — по правилу partest
    (`partest.conf.resolve_entry_location`): сначала от текущего каталога, затем от каталога
    `confpartest.py`; откуда взят — `source.resolvedFrom` (`cwd` или `confpartest`).

    `confpartest.py` импортируется (это код проекта), но **не так, как при прогоне pytest**:
    нет `conftest.py`, `pythonpath` из `pytest.ini`, `env =` из pytest-env и `.env`, который
    подгружает conftest. Адрес, собранный из переменной окружения, берётся из окружения
    этого вызова.
    """
    return _load(root, service, notes if notes is not None else [])[1]


# --- несколько служб в одном артефакте ------------------------------------------------


def _service_entry(loaded: Loaded) -> Dict[str, Any]:
    operations = [{**op, "service": loaded.service} for op in parse(loaded.document)]
    return {
        "service": loaded.service,
        "info": info_of(loaded.document),
        "source": loaded.source,
        "stats": summarize(operations),
        # Когда снята именно эта служба: при `--service` остальные несут своё прежнее время.
        "collectedAt": artifacts._now(),
        "_operations": operations,
    }


def assemble(services: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Содержание артефакта из служб: операции всех служб с ключом `service`, `services`
    — по записи на службу (`service`, `info`, `source`, `stats`, `collectedAt`), общая
    `stats`. `info` и `source` верхнего уровня — только когда служба одна: у нескольких их
    нет, и читатель берёт их из `services`."""
    operations = [op for s in services for op in s["_operations"]]
    payload: Dict[str, Any] = {
        "operations": operations,
        "stats": summarize(operations),
        "services": [{k: v for k, v in s.items() if k != "_operations"} for s in services],
    }
    if len(services) == 1:
        payload["info"] = services[0]["info"]
        payload["source"] = services[0]["source"]
    return payload


def _kept_services(
    path: Path, scope: Scope, replaced: str, declared: List[str], notes: List[str]
) -> List[Dict[str, Any]]:
    """Службы из прежнего артефакта, кроме *replaced*, и только те, что ещё объявлены в
    `swagger_files` (*declared*): убранная из `confpartest.py` служба из артефакта уходит.
    Имя службы — только строка, повторы схлопнуты (первая запись). Непонятый файл не даёт
    ничего (с заметкой); файл новее схемы — отказ."""
    found = artifacts.inspect(path, KIND, scope=scope)
    if found.state == artifacts.MISSING:
        return []
    if found.state == artifacts.NEWER:
        raise SourceError(t(
            f"{path.name} записан схемой новее известной — не перезаписываю",
            f"{path.name} was written by a newer schema — not overwriting it",
        ))
    if not found.usable:
        notes.append(t(
            f"прежний {path.name} не понят ({found.state}) — другие службы из него не сохранены",
            f"the previous {path.name} was not understood ({found.state}) — other services "
            "from it were not kept",
        ))
        return []
    doc = found.doc
    rows = doc.get("services")
    operations = [op for op in doc.get("operations") or [] if isinstance(op, dict)]
    if not isinstance(rows, list):
        # Артефакт одной службы, снятый до `services`: служба — из `source.service`.
        source = doc.get("source") if isinstance(doc.get("source"), dict) else {}
        single = source.get("service")
        if not isinstance(single, str) or not single:
            if operations:
                notes.append(t(
                    f"в прежнем {path.name} операции без службы — заменены",
                    f"operations in the previous {path.name} name no service — replaced",
                ))
            return []
        rows = [{"service": single, "info": doc.get("info"), "source": source,
                 "stats": doc.get("stats"), "collectedAt": doc.get("collected_at")}]
        operations = [{**op, "service": single} for op in operations]
    allowed = set(declared) - {replaced}
    by_service: Dict[str, List[Dict[str, Any]]] = {}
    for op in operations:
        name = op.get("service")
        if isinstance(name, str) and name in allowed:
            by_service.setdefault(name, []).append(op)
    kept: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        name = row.get("service") if isinstance(row, dict) else None
        if not isinstance(name, str) or name not in allowed or name in kept:
            continue
        kept[name] = {**row, "_operations": by_service.get(name, [])}
    return list(kept.values())


def write(
    config: Optional[Config] = None,
    *,
    root: Optional[Union[str, Path]] = None,
    service: Optional[str] = None,
    scope: Optional[Scope] = None,
    notes: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Взять спецификации из `confpartest.py` и положить артефакт. Возвращает записанное
    (уже замаскированное) содержание.

    Без *service* — все службы `swagger_files`, артефакт пишется заново. С *service* —
    только она: её операции и запись в `services` заменяются, остальные службы прежнего
    артефакта той же области сохраняются, если они ещё объявлены в `swagger_files`. Порядок
    служб и операций — порядок `swagger_files`. *root* — каталог `confpartest.py`; по
    умолчанию — каталог `atlas.yaml` (или текущий). *scope* — область, если не та, что в
    окружении. *notes* — сюда дописываются заметки (прежний файл не понят, операций 0…).

    Два одновременных вызова с разными *service* одной области не согласованы: дописка без
    блокировки, и одна из служб потеряется. Службы одной области собираются одним вызовом
    без *service* или последовательно.
    """
    cfg = config or load()
    where = scope or cfg.scope
    path = where.path(cfg.artifacts_dir, KIND)
    sink: List[str] = notes if notes is not None else []
    declared, loaded = _load(root or cfg.root, service, sink)
    fresh = [_service_entry(x) for x in loaded]
    for entry in fresh:
        if not entry["_operations"]:
            sink.append(t(
                f"{entry['service']}: операций 0 — в спецификации нет путей",
                f"{entry['service']}: 0 operations — the specification has no paths",
            ))
    services = fresh if service is None else _kept_services(path, where, service, declared, sink) + fresh
    order = {name: i for i, name in enumerate(declared)}
    services.sort(key=lambda s: order.get(s["service"], len(order)))
    return artifacts.write(path, KIND, assemble(services), scope=where,
                           collector="partest-atlas/openapi")
