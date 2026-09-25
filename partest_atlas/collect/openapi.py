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
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

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


def build(document: Dict[str, Any]) -> Dict[str, Any]:
    operations = parse(document)
    return {"operations": operations, "stats": summarize(operations)}
