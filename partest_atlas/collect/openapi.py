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
from typing import Any, Dict, List, Mapping, Optional, Union

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
    if ref.startswith("#/components/schemas/") and depth < MAX_REF_DEPTH:
        return schemas.get(ref.rsplit("/", 1)[-1]) or {}
    return schema or {}


def _body(op: dict, schemas: Dict[str, dict]) -> Optional[dict]:
    """Тело запроса. ``None``, когда тела нет — пустой объект читался бы как «есть, но
    пустое», а это разные утверждения."""
    request_body = op.get("requestBody") or {}
    content = request_body.get("content") or {}
    media = content.get("application/json") or next(iter(content.values()), {})
    raw = (media or {}).get("schema") or {}
    name = (raw.get("$ref") or "").rsplit("/", 1)[-1]
    resolved = _deref(raw, schemas)
    fields = list(resolved.get("properties") or {})
    if not name and not fields:
        return None
    return {
        "schema": name or None,
        "required": bool(request_body.get("required")),
        "requiredFields": sorted(resolved.get("required") or []),
        "fields": fields[:FIELDS_LIMIT],
    }


def parse(document: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Контракт → плоский список операций. Сети здесь нет."""
    schemas = (document.get("components") or {}).get("schemas") or {}
    out: List[Dict[str, Any]] = []
    for path, item in (document.get("paths") or {}).items():
        if not isinstance(item, dict):
            continue
        # Параметры уровня пути принадлежат каждой операции под ним. Потерять их значит
        # объявить обязательный параметр необязательным.
        shared = item.get("parameters") or []
        for method in METHODS:
            op = item.get(method)
            if not isinstance(op, dict):
                continue
            params = [
                {"name": p.get("name"), "in": p.get("in"), "required": bool(p.get("required"))}
                for p in [*shared, *(op.get("parameters") or [])]
                if p.get("name")
            ]
            out.append(
                {
                    "method": method.upper(),
                    "path": norm_path(path),
                    "rawPath": path,
                    "operationId": op.get("operationId"),
                    "tags": list(op.get("tags") or []),
                    "deprecated": bool(op.get("deprecated")),
                    # Описаний тут может не быть ни одного. Поле остаётся: на другом
                    # проекте они есть, и код не должен узнавать об этом заново.
                    "summary": op.get("summary") or "",
                    "description": op.get("description") or "",
                    "params": params,
                    "body": _body(op, schemas),
                    "responses": sorted((op.get("responses") or {}).keys()),
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
        return None if value is None or isinstance(value, (dict, list)) else str(value)

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


def _local_place(location: str, base: Path) -> str:
    """Где лежит локальная запись — по правилу partest: путь, который существует от текущего
    каталога, берётся оттуда; иначе — от каталога `confpartest.py`. Абсолютный — как есть.

    TODO(partest 2.8): заменить на публичную функцию partest, разрешающую относительное
    место записи по этому правилу, когда она появится в дереве partest_client; это
    единственная точка вызова.
    """
    path = Path(location)
    if path.is_absolute() or path.exists():
        return str(path.resolve())
    return str((base / path).resolve())


def _resolve(entry: Any, base: Path, shown: str) -> Dict[str, Any]:
    """Документ записи — `partest.openapi.resolve_swagger` с потолками размера и времени."""
    from partest.openapi import OpenApiResolveError, resolve_swagger  # noqa: PLC0415

    location = entry.location if entry.kind == "url" else _local_place(entry.location, base)
    try:
        document = resolve_swagger(
            (entry.kind, location),
            project_root=base,
            timeout=FETCH_TIMEOUT_SEC,
            total_timeout=FETCH_TOTAL_TIMEOUT_SEC,
            max_redirects=MAX_REDIRECTS,
        )
    except (OpenApiResolveError, OSError, ValueError) as exc:
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
    return document


def load_from_confpartest(
    root: Union[str, Path], *, service: Optional[str] = None
) -> List[Loaded]:
    """Спецификации, объявленные в `confpartest.swagger_files` проекта в *root*.

    Без *service* — все службы, в порядке объявления; с *service* — только она. Загрузка и
    разбор — `partest.openapi.resolve_swagger` (под политикой TLS проекта, с потолком
    размера, времени шага, времени между шагами и числа переадресаций). Любая неудача — :class:`SourceError` без секретов, и ни одна служба
    тогда не возвращается: частичный контракт выглядел бы как полный.

    Локальный относительный путь — по правилу partest: сначала от текущего каталога, затем
    от каталога `confpartest.py` (:func:`_local_place`).

    `confpartest.py` импортируется (это код проекта), но **не так, как при прогоне pytest**:
    нет `conftest.py`, `pythonpath` из `pytest.ini`, `env =` из pytest-env и `.env`, который
    подгружает conftest. Адрес, собранный из переменной окружения, берётся из окружения
    этого вызова.
    """
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
        document = _resolve(entry, base, f"{source['service']} ({source['location']})")
        out.append(Loaded(name, document, source))
    return out


# --- несколько служб в одном артефакте ------------------------------------------------


def _service_entry(loaded: Loaded) -> Dict[str, Any]:
    operations = [{**op, "service": loaded.service} for op in parse(loaded.document)]
    return {
        "service": loaded.service,
        "info": info_of(loaded.document),
        "source": loaded.source,
        "stats": summarize(operations),
        "_operations": operations,
    }


def assemble(services: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Содержание артефакта из служб: операции всех служб с ключом `service`, `services`
    — по записи на службу (`service`, `info`, `source`, `stats`), общая `stats`. `info` и
    `source` верхнего уровня — только когда служба одна: у нескольких их нет, и читатель
    берёт их из `services`."""
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


def _kept_services(path: Path, scope: Scope, replaced: str, notes: List[str]) -> List[Dict[str, Any]]:
    """Службы из прежнего артефакта, кроме *replaced* — чтобы `--service` обновлял свою, не
    затирая остальные. Непонятый файл не даёт ничего (с заметкой); файл новее схемы — отказ:
    переписать его значило бы потерять то, чего этот пакет не понимает."""
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
    if not isinstance(rows, list):
        # Артефакт одной службы, снятый до `services`: служба — из `source.service`.
        single = (doc.get("source") or {}).get("service") if isinstance(doc.get("source"), dict) else None
        if not single:
            if doc.get("operations"):
                notes.append(t(
                    f"в прежнем {path.name} операции без службы — заменены",
                    f"operations in the previous {path.name} name no service — replaced",
                ))
            return []
        rows = [{"service": single, "info": doc.get("info"), "source": doc.get("source"),
                 "stats": doc.get("stats")}]
        ops = [{**op, "service": single} for op in doc.get("operations") or [] if isinstance(op, dict)]
    else:
        ops = [op for op in doc.get("operations") or [] if isinstance(op, dict)]
    kept = []
    for row in rows:
        if not isinstance(row, dict) or row.get("service") == replaced:
            continue
        name = row.get("service")
        kept.append({**row, "_operations": [op for op in ops if op.get("service") == name]})
    return kept


def write(
    config: Optional[Config] = None,
    *,
    root: Optional[Union[str, Path]] = None,
    service: Optional[str] = None,
    scope: Optional[Scope] = None,
    notes: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Взять спецификации из `confpartest.py` и положить артефакт. Возвращает содержание.

    Без *service* — все службы `swagger_files`, артефакт пишется заново. С *service* —
    только она: её операции и запись в `services` заменяются, остальные службы из прежнего
    артефакта той же области сохраняются. *root* — каталог `confpartest.py`; по умолчанию —
    каталог `atlas.yaml` (или текущий). *scope* — область, если не та, что в окружении.
    *notes* — сюда дописываются заметки (прежний файл не понят и т. п.).
    """
    cfg = config or load()
    where = scope or cfg.scope
    path = where.path(cfg.artifacts_dir, KIND)
    sink: List[str] = notes if notes is not None else []
    fresh = [_service_entry(x) for x in load_from_confpartest(root or cfg.root, service=service)]
    if service is None:
        services = fresh
    else:
        kept = _kept_services(path, where, service, sink)
        services = kept + fresh
    payload = assemble(services)
    artifacts.write(path, KIND, payload, scope=where, collector="partest-atlas/openapi")
    return payload
