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
`confpartest.swagger_files` — того же места, откуда покрытие считает partest. Файл или
адрес, объявленный проектом, читается в окружении проекта; сам пакет никуда не ходит.
В артефакт попадает, **откуда** взят документ (:func:`source_of`): адрес — без учётных
данных, без строки запроса и через `partest.redact`.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple, Union
from urllib.parse import unquote, urlsplit, urlunsplit

from partest_atlas import artifacts
from partest_atlas.artifacts import Scope
from partest_atlas.config import Config, load
from partest_atlas.lang import t

KIND = "contract.openapi"

#: Виды источника в `swagger_files`, как их принимает partest (`validate_confpartest`).
LOCAL_KINDS = ("local", "file", "path")
URL_KIND = "url"

#: Сколько ждать документ по адресу. Как у partest (`resolve_swagger`).
FETCH_TIMEOUT_SEC = 60.0

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


def _redact_text(text: str) -> str:
    from partest.redact import redact_text  # noqa: PLC0415 — partest нужен только здесь

    return redact_text(text)


def redact_url(url: str) -> str:
    """Адрес для артефакта и сообщений: без `user:password@`, строки запроса и якоря.

    Учётные данные и токены в адресе спецификации бывают (`?access_token=…`), а артефакт
    читает чужая программа. Что осталось — ещё и через `partest.redact.redact_text`.
    """
    try:
        parts = urlsplit(url)
        netloc = parts.netloc.rpartition("@")[2]
        clean = urlunsplit((parts.scheme, netloc, parts.path, "", ""))
    except ValueError:
        clean = url.split("?", 1)[0].split("#", 1)[0].rpartition("@")[2]
    return _redact_text(clean)


def source_of(service: str, kind: str, location: str) -> Dict[str, Any]:
    """Запись `source` артефакта: служба из `swagger_files`, вид и место — без секретов.

    Локальный путь пишется **как объявлен** в `confpartest.py`, а не разрешённым: полный
    путь на машине сборки — чужой каталог пользователя, карте он ничего не говорит.
    """
    if kind == URL_KIND:
        return {"service": service, "type": URL_KIND, "location": redact_url(location)}
    return {"service": service, "type": "local", "location": _redact_text(location)}


@dataclass(frozen=True)
class Loaded:
    """Документ и то, откуда он взят."""

    document: Dict[str, Any]
    source: Dict[str, Any]


def _parse_text(text: str, shown: str) -> Dict[str, Any]:
    text = text.lstrip("\ufeff")
    try:
        data: Any = json.loads(text)
    except ValueError:
        try:
            import yaml  # noqa: PLC0415

            data = yaml.safe_load(text)
        except Exception as exc:
            raise SourceError(t(
                f"{shown}: не JSON и не YAML — {type(exc).__name__}",
                f"{shown}: neither JSON nor YAML — {type(exc).__name__}",
            )) from None
    if not isinstance(data, dict):
        raise SourceError(t(
            f"{shown}: ожидался объект OpenAPI, получено {type(data).__name__}",
            f"{shown}: expected an OpenAPI object, got {type(data).__name__}",
        ))
    if "openapi" not in data and "swagger" not in data:
        raise SourceError(t(
            f"{shown}: не похоже на OpenAPI — нет ключа openapi или swagger",
            f"{shown}: does not look like OpenAPI — no openapi or swagger key",
        ))
    return data


def _entries(conf: Any) -> Dict[str, Tuple[str, str]]:
    """`swagger_files` в виде «служба → (вид, место)», с громкими ошибками формы."""
    swagger = getattr(conf, "swagger_files", None)
    if not isinstance(swagger, Mapping) or not swagger:
        raise SourceError(t(
            "в confpartest.py нет swagger_files или он пуст — "
            "ожидалось {'имя': ['local'|'url', путь_или_адрес]}",
            "confpartest.py has no swagger_files or it is empty — "
            "expected {'name': ['local'|'url', path_or_url]}",
        ))
    out: Dict[str, Tuple[str, str]] = {}
    for name, entry in swagger.items():
        if not isinstance(entry, (list, tuple)) or len(entry) < 2:
            raise SourceError(t(
                f"swagger_files[{name!r}]: ожидалось [вид, путь_или_адрес]",
                f"swagger_files[{name!r}]: expected [kind, path_or_url]",
            ))
        kind = str(entry[0]).strip().lower()
        if kind not in (*LOCAL_KINDS, URL_KIND):
            raise SourceError(t(
                f"swagger_files[{name!r}]: вид {entry[0]!r} — ожидалось local или url",
                f"swagger_files[{name!r}]: kind {entry[0]!r} — expected local or url",
            ))
        out[str(name)] = (kind, str(entry[1]))
    return out


def _fetch(location: str, shown: str) -> str:
    """Документ по адресу — клиентом partest, под его политикой TLS."""
    from partest import httpx_client  # noqa: PLC0415
    from partest.tls import VERIFY_CONF_ATTR, VERIFY_ENV, is_certificate_error  # noqa: PLC0415

    parts = urlsplit(location)
    if parts.scheme not in ("http", "https"):
        raise SourceError(t(
            f"{shown}: адрес должен начинаться с http:// или https://",
            f"{shown}: the URL must start with http:// or https://",
        ))
    auth = None
    netloc = parts.netloc
    if "@" in netloc:
        # Учётные данные — в `auth=`, а не в адрес: адрес попадает в журналы httpx.
        userinfo, netloc = netloc.rsplit("@", 1)
        user, _, password = userinfo.partition(":")
        auth = (unquote(user), unquote(password))
    url = urlunsplit(parts._replace(netloc=netloc))
    try:
        with httpx_client(timeout=FETCH_TIMEOUT_SEC, follow_redirects=True) as client:
            response = client.get(url, auth=auth)
    except Exception as exc:
        if is_certificate_error(exc):
            raise SourceError(t(
                f"{shown}: сертификат сервера не принят — доверенный CA или выключение "
                f"проверки: {VERIFY_ENV} или confpartest.{VERIFY_CONF_ATTR}",
                f"{shown}: the server certificate was not accepted — a trusted CA or "
                f"turning verification off: {VERIFY_ENV} or confpartest.{VERIFY_CONF_ATTR}",
            )) from None
        # Текст исключения httpx может нести адрес целиком, со строкой запроса, — его не
        # печатаем; имя класса говорит, что случилось: нет имени, нет маршрута, тайм-аут.
        raise SourceError(t(
            f"{shown}: не получен — {type(exc).__name__}",
            f"{shown}: not fetched — {type(exc).__name__}",
        )) from None
    if response.status_code >= 400:
        raise SourceError(t(
            f"{shown}: сервер ответил {response.status_code}",
            f"{shown}: the server answered {response.status_code}",
        ))
    return response.text


def load_from_confpartest(
    root: Union[str, Path], *, service: Optional[str] = None
) -> Loaded:
    """Спецификация, объявленная в `confpartest.swagger_files` проекта в *root*.

    `confpartest.py` загружает partest (`partest.load_confpartest`) — это **импорт кода
    проекта**, как и при прогоне pytest. Локальный путь — от каталога `confpartest.py`,
    адрес — клиентом partest под политикой TLS проекта (`tls_verify`).

    Без *service* спецификация должна быть одна: выбрать первую из нескольких молча
    значило бы снять не тот контракт и не сказать об этом.
    """
    from partest import load_confpartest  # noqa: PLC0415 — partest нужен только здесь

    expected = (Path(root) / "confpartest.py").resolve()
    if not expected.is_file():
        raise SourceError(t(
            f"в {Path(root)} нет confpartest.py — укажите каталог проекта ключом --root",
            f"there is no confpartest.py in {Path(root)} — name the project directory with --root",
        ))
    try:
        conf = load_confpartest(project_root=root)
    except Exception as exc:
        raise SourceError(t(
            f"confpartest.py не загружен из {root}: {_redact_text(str(exc))}",
            f"confpartest.py was not loaded from {root}: {_redact_text(str(exc))}",
        )) from None
    loaded_from = getattr(conf, "__file__", None)
    if not loaded_from or Path(loaded_from).resolve() != expected:
        # partest берёт `confpartest`, уже импортированный или первый найденный в `sys.path`;
        # чужой модуль с тем же именем дал бы чужой контракт без единого предупреждения.
        raise SourceError(t(
            f"загружен не тот confpartest: {loaded_from} вместо {expected}",
            f"the wrong confpartest was loaded: {loaded_from} instead of {expected}",
        ))

    entries = _entries(conf)
    if service is None:
        if len(entries) > 1:
            raise SourceError(t(
                f"в swagger_files несколько спецификаций ({', '.join(entries)}) — "
                "выберите одну ключом --service",
                f"swagger_files declares several specifications ({', '.join(entries)}) — "
                "choose one with --service",
            ))
        service = next(iter(entries))
    elif service not in entries:
        raise SourceError(t(
            f"службы {service!r} нет в swagger_files; есть: {', '.join(entries)}",
            f"service {service!r} is not in swagger_files; known: {', '.join(entries)}",
        ))

    kind, location = entries[service]
    source = source_of(service, kind, location)
    shown = source["location"]
    if kind == URL_KIND:
        return Loaded(_parse_text(_fetch(location, shown), shown), source)

    path = Path(location)
    if not path.is_absolute():
        base = Path(getattr(conf, "__file__", None) or Path(root) / "confpartest.py").parent
        path = base / path
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SourceError(t(
            f"{shown}: файл спецификации не читается ({type(exc).__name__}) — путь берётся "
            "от каталога confpartest.py",
            f"{shown}: the specification file cannot be read ({type(exc).__name__}) — the "
            "path is taken from the directory of confpartest.py",
        )) from None
    return Loaded(_parse_text(text, shown), source)


def write(
    config: Optional[Config] = None,
    *,
    root: Optional[Union[str, Path]] = None,
    service: Optional[str] = None,
    scope: Optional[Scope] = None,
) -> Dict[str, Any]:
    """Взять спецификацию из `confpartest.py` и положить артефакт. Возвращает содержание.

    *root* — каталог с `confpartest.py`; по умолчанию — каталог `atlas.yaml` (или текущий,
    если файла нет). *scope* — область, если не та, что в `ATLAS_SYSTEM`/`ATLAS_ENV`.
    """
    cfg = config or load()
    where = scope or cfg.scope
    loaded = load_from_confpartest(root or cfg.root, service=service)
    payload = build(loaded.document, source=loaded.source)
    artifacts.write(
        where.path(cfg.artifacts_dir, KIND),
        KIND,
        payload,
        scope=where,
        collector="partest-atlas/openapi",
    )
    return payload
