"""Схема базы → артефакт `db.schema`. Строго на чтение.

Только `SELECT` из `information_schema` и `pg_stat_user_tables`. Ни одной записи: сбор
наблюдений не имеет права менять систему, за которой наблюдает, — иначе карта перестаёт
описывать то, что было без неё.

**Опрос отделён от лепки документа**, и это главная правка переноса. В прототипе они были
одним куском, поэтому проверить форму артефакта можно было только на живой базе — то есть
на практике не проверять. :func:`build` принимает уже прочитанные строки и не знает, откуда
они: его проверяет обычный тест без всякой базы, а :func:`collect` остаётся тонкой
обвязкой, в которой ошибаться почти негде.

Подключение приходит из `atlas.yaml`, а не из кода. В прототипе путь к функции соединения
конкретного проекта был вписан в сборщик константой — ровно та протечка проектного знания
в пакет, которую пакет запрещает.
"""

from __future__ import annotations

import importlib
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Mapping, Optional, Sequence

from partest_atlas import artifacts
from partest_atlas.config import Config, env, load

KIND = "db.schema"

COLUMNS_SQL = """
SELECT c.table_schema, c.table_name, c.column_name, c.ordinal_position,
       c.data_type, c.is_nullable, c.column_default, c.character_maximum_length
  FROM information_schema.columns c
 WHERE c.table_schema = ANY(%(schemas)s)
 ORDER BY c.table_schema, c.table_name, c.ordinal_position
"""

PRIMARY_KEYS_SQL = """
SELECT tc.table_schema, tc.table_name, kcu.column_name
  FROM information_schema.table_constraints tc
  JOIN information_schema.key_column_usage kcu
    ON kcu.constraint_name = tc.constraint_name
   AND kcu.table_schema = tc.table_schema
 WHERE tc.constraint_type = 'PRIMARY KEY'
   AND tc.table_schema = ANY(%(schemas)s)
"""

FOREIGN_KEYS_SQL = """
SELECT tc.table_schema, tc.table_name, kcu.column_name,
       ccu.table_name AS ref_table, ccu.column_name AS ref_column,
       rc.delete_rule
  FROM information_schema.table_constraints tc
  JOIN information_schema.key_column_usage kcu
    ON kcu.constraint_name = tc.constraint_name
   AND kcu.table_schema = tc.table_schema
  JOIN information_schema.constraint_column_usage ccu
    ON ccu.constraint_name = tc.constraint_name
  JOIN information_schema.referential_constraints rc
    ON rc.constraint_name = tc.constraint_name
 WHERE tc.constraint_type = 'FOREIGN KEY'
   AND tc.table_schema = ANY(%(schemas)s)
"""

#: Оценка из статистики планировщика, а не `count(*)`. Точное число строк стоит полного
#: прохода по таблице, а карте нужен порядок величины — «сто» против «миллион».
APPROX_ROWS_SQL = """
SELECT relname AS table_name, n_live_tup AS rows
  FROM pg_stat_user_tables
 WHERE schemaname = ANY(%(schemas)s)
"""

#: Длинные имена типов Postgres в короткие. Карта показывает их в ячейке таблицы, и
#: `timestamp without time zone` вытесняет оттуда всё остальное.
TYPE_ALIASES = {
    "character varying": "varchar",
    "timestamp without time zone": "timestamp",
    "timestamp with time zone": "timestamptz",
    "double precision": "float8",
    "integer": "int4",
    "bigint": "int8",
    "boolean": "bool",
}

#: Сколько символов значения по умолчанию оставляем. Целиком оно бывает в килобайт
#: (длинные выражения), и в карте такое всё равно не читают.
DEFAULT_CLIP = 60


def short_type(column: Mapping[str, Any]) -> str:
    name = TYPE_ALIASES.get(column["data_type"], column["data_type"])
    length = column.get("character_maximum_length")
    return f"{name}({length})" if length else name


def build(
    columns: Sequence[Mapping[str, Any]],
    primary_keys: Sequence[Mapping[str, Any]] = (),
    foreign_keys: Sequence[Mapping[str, Any]] = (),
    row_counts: Sequence[Mapping[str, Any]] = (),
    *,
    schemas: Sequence[str] = ("public",),
    exclude: Sequence[str] = (),
    label: str = "PostgreSQL",
) -> Dict[str, Any]:
    """Слепить содержание артефакта из прочитанных строк. Базы здесь нет и быть не должно."""
    excluded = {name.lower() for name in exclude}
    pk_set = {(r["table_schema"], r["table_name"], r["column_name"]) for r in primary_keys}
    fk_map = {
        (r["table_schema"], r["table_name"], r["column_name"]): {
            "table": r["ref_table"],
            "column": r["ref_column"],
            "onDelete": r.get("delete_rule"),
        }
        for r in foreign_keys
    }
    rows_by_table = {r["table_name"]: r["rows"] for r in row_counts}

    tables: Dict[str, Dict[str, Any]] = {}
    for column in columns:
        table_name = column["table_name"]
        if table_name.lower() in excluded:
            continue
        key = f'{column["table_schema"]}.{table_name}'
        table = tables.setdefault(
            key,
            {
                "schema": column["table_schema"],
                "name": table_name,
                "rows": rows_by_table.get(table_name),
                "columns": [],
            },
        )
        ident = (column["table_schema"], table_name, column["column_name"])
        table["columns"].append(
            {
                "name": column["column_name"],
                "type": short_type(column),
                "nullable": column["is_nullable"] == "YES",
                "default": (column.get("column_default") or "")[:DEFAULT_CLIP] or None,
                "pk": ident in pk_set,
                "fk": fk_map.get(ident),
            }
        )

    ordered = sorted(tables.values(), key=lambda t: (t["schema"], t["name"]))
    return {
        "databases": [
            {
                "key": "primary",
                "label": label,
                "schemas": list(schemas),
                "tables": ordered,
            }
        ],
        "stats": {
            "tables": len(ordered),
            "columns": sum(len(t["columns"]) for t in ordered),
            "foreignKeys": len(fk_map),
        },
    }


@contextmanager
def _connect(section: Mapping[str, Any]) -> Iterator[Any]:
    """Соединение так, как назвал проект.

    Два пути, в порядке предпочтения:

    ``connect: "модуль:функция"`` — у проекта своя функция-контекстменеджер. Она знает
    про туннели, про права и про всё прочее, чего пакету знать не положено.

    ``dsn_env: ИМЯ`` — строка подключения в переменной окружения; поднимаем psycopg сами.
    """
    target = section.get("connect")
    if target:
        module_name, _, attr = str(target).partition(":")
        if not attr:
            raise RuntimeError(
                f"database.connect={target!r}: ожидалось 'модуль:функция'"
            )
        try:
            factory = getattr(importlib.import_module(module_name), attr)
        except Exception as exc:
            raise RuntimeError(
                f"database.connect={target!r} не импортируется: {exc}"
            ) from exc
        with factory() as conn:
            yield conn
        return

    dsn_env = section.get("dsn_env") or "DATABASE_URL"
    dsn = env(str(dsn_env))
    if not dsn:
        raise RuntimeError(
            f"database.connect не задан и переменная {dsn_env} пуста — опрашивать нечего"
        )
    import psycopg  # noqa: PLC0415 — драйвер в extras, не у всех установлен
    from psycopg.rows import dict_row  # noqa: PLC0415

    conn = psycopg.connect(dsn, row_factory=dict_row)
    try:
        yield conn
    finally:
        conn.close()


def collect(config: Optional[Config] = None, *, count_rows: bool = True) -> Dict[str, Any]:
    """Опросить базу и слепить содержание артефакта."""
    cfg = config or load()
    section = cfg.section("database")
    schemas: List[str] = list(section.get("schemas") or ["public"])

    with _connect(section) as conn:
        with conn.cursor() as cur:
            cur.execute(COLUMNS_SQL, {"schemas": schemas})
            columns = cur.fetchall()
            cur.execute(PRIMARY_KEYS_SQL, {"schemas": schemas})
            primary_keys = cur.fetchall()
            cur.execute(FOREIGN_KEYS_SQL, {"schemas": schemas})
            foreign_keys = cur.fetchall()
            row_counts: Sequence[Mapping[str, Any]] = ()
            if count_rows and section.get("count_rows", True):
                cur.execute(APPROX_ROWS_SQL, {"schemas": schemas})
                row_counts = cur.fetchall()

    return build(
        columns,
        primary_keys,
        foreign_keys,
        row_counts,
        schemas=schemas,
        exclude=section.get("exclude") or (),
        label=section.get("label") or "PostgreSQL",
    )


def write(config: Optional[Config] = None, *, count_rows: bool = True) -> Dict[str, Any]:
    """Снять схему и положить артефакт. Возвращает содержание — для вызова из кода."""
    cfg = config or load()
    payload = collect(cfg, count_rows=count_rows)
    artifacts.write(
        cfg.path_for(KIND),
        KIND,
        payload,
        scope=cfg.scope,
        collector="partest-atlas/db_schema",
    )
    return payload
