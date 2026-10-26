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

**Только чтение держит база, а не соглашение.** Все запросы идут в транзакции, которую
:func:`partest_atlas.adapters.postgres.read_only` перевела в режим только чтения и
проверила; каждый запрос до отправки проходит
`partest.sideeffects.assert_read_only_sql` — одиночный `SELECT` или отказ.

**Пустая схема — громкая ошибка.** После переезда таблиц в другую схему сборщик молча
отдавал пустой артефакт, и карта уверенно показывала «таблиц нет». Теперь «в выбранных
схемах нет ни одной таблицы» — :class:`NoTables`; `schemas: auto` берёт все несистемные
схемы, какие видит база.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Sequence

from partest_atlas import artifacts
from partest_atlas.adapters.postgres import connect, discover_schemas, read_only, schemas_from
from partest_atlas.artifacts import Scope
from partest_atlas.config import Config, load
from partest_atlas.lang import t

KIND = "db.schema"

#: Ключ базы в артефакте, если `database.key` не задан. Так было до появления ключа.
DEFAULT_KEY = "primary"


class NoTables(RuntimeError):
    """В выбранных схемах нет ни одной таблицы — почти всегда схемы выбраны не те."""

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

#: Внешние ключи с целью. Каждое соединение — по имени **и схеме** ограничения: имена
#: ограничений уникальны только внутри схемы, и при `fk_user` в двух схемах соединение по
#: одному имени давало чужую цель, а на N одинаковых схемах — N² строк на ключ. Цель — из
#: `key_column_usage` уникального ограничения по позиции столбца, а не из
#: `constraint_column_usage`: у составного ключа та давала произведение столбцов.
FOREIGN_KEYS_SQL = """
SELECT tc.table_schema, tc.table_name, kcu.column_name,
       rkcu.table_schema AS ref_schema, rkcu.table_name AS ref_table,
       rkcu.column_name AS ref_column, rc.delete_rule
  FROM information_schema.table_constraints tc
  JOIN information_schema.key_column_usage kcu
    ON kcu.constraint_name = tc.constraint_name
   AND kcu.constraint_schema = tc.constraint_schema
  JOIN information_schema.referential_constraints rc
    ON rc.constraint_name = tc.constraint_name
   AND rc.constraint_schema = tc.constraint_schema
  JOIN information_schema.key_column_usage rkcu
    ON rkcu.constraint_name = rc.unique_constraint_name
   AND rkcu.constraint_schema = rc.unique_constraint_schema
   AND rkcu.ordinal_position = kcu.position_in_unique_constraint
 WHERE tc.constraint_type = 'FOREIGN KEY'
   AND tc.table_schema = ANY(%(schemas)s)
"""

#: Оценка из статистики планировщика, а не `count(*)`. Точное число строк стоит полного
#: прохода по таблице, а карте нужен порядок величины — «сто» против «миллион».
APPROX_ROWS_SQL = """
SELECT schemaname AS table_schema, relname AS table_name, n_live_tup AS rows
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
    key: str = DEFAULT_KEY,
) -> Dict[str, Any]:
    """Слепить содержание артефакта из прочитанных строк. Базы здесь нет и быть не должно.

    Таблица — пара (схема, имя): в разных схемах бывают одноимённые таблицы, и число строк
    или исключение по одному имени достались бы чужой. `exclude` принимает имя (во всех
    схемах) или `схема.имя`, без учёта регистра. У внешнего ключа — схема цели (`schema`).
    """
    excluded = {name.lower() for name in exclude}
    pk_set = {(r["table_schema"], r["table_name"], r["column_name"]) for r in primary_keys}
    fk_map = {}
    for r in foreign_keys:
        target = {"table": r["ref_table"], "column": r["ref_column"], "onDelete": r.get("delete_rule")}
        if r.get("ref_schema"):
            target["schema"] = r["ref_schema"]
        fk_map[(r["table_schema"], r["table_name"], r["column_name"])] = target
    # Строки без схемы (вызывающий, собравший их сам по-старому) — по имени.
    rows_by_table = {(r.get("table_schema"), r["table_name"]): r["rows"] for r in row_counts}

    tables: Dict[str, Dict[str, Any]] = {}
    for column in columns:
        table_name = column["table_name"]
        schema_name = column["table_schema"]
        if table_name.lower() in excluded or f"{schema_name}.{table_name}".lower() in excluded:
            continue
        table_key = f'{column["table_schema"]}.{table_name}'
        table = tables.setdefault(
            table_key,
            {
                "schema": column["table_schema"],
                "name": table_name,
                "rows": rows_by_table.get((schema_name, table_name), rows_by_table.get((None, table_name))),
                "columns": [],
            },
        )
        ident = (column["table_schema"], table_name, column["column_name"])
        table["columns"].append(
            {
                "name": column["column_name"],
                "type": short_type(column),
                "nullable": column["is_nullable"] == "YES",
                # У колонки с секретным именем (`password`, `api_key`…) значение по
                # умолчанию не пишется вовсе — только тип: литерал там и бывает секретом.
                "default": None if artifacts.is_secret_key(column["column_name"])
                else (column.get("column_default") or "")[:DEFAULT_CLIP] or None,
                "pk": ident in pk_set,
                "fk": fk_map.get(ident),
            }
        )

    ordered = sorted(tables.values(), key=lambda t: (t["schema"], t["name"]))
    return {
        "databases": [
            {
                "key": key,
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


def _select(cur: Any, sql: str, params: Mapping[str, Any]) -> List[Any]:
    """Одиночный `SELECT` или отказ до отправки — проверкой из partest."""
    from partest.sideeffects import assert_read_only_sql  # noqa: PLC0415 — только при опросе

    cur.execute(assert_read_only_sql(sql), params)
    return cur.fetchall()


def key_from(section: Mapping[str, Any]) -> str:
    """Ключ базы в артефакте: `database.key`, по умолчанию `primary`."""
    raw = section.get("key")
    if raw is None:
        return DEFAULT_KEY
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError(t(
            "database.key: ожидалась непустая строка",
            "database.key: expected a non-empty string",
        ))
    return raw.strip()


def collect(config: Optional[Config] = None, *, count_rows: bool = True) -> Dict[str, Any]:
    """Опросить базу и слепить содержание артефакта.

    :class:`NoTables`, если в выбранных схемах нет ни одной таблицы;
    :class:`~partest_atlas.adapters.postgres.NotReadOnly`, если база не подтвердила режим
    только чтения; ``ValueError`` — кривые `database.schemas` или `database.key`.
    """
    cfg = config or load()
    section = cfg.section("database")
    chosen = schemas_from(section)
    key = key_from(section)

    with connect(section) as conn:
        schemas: List[str] = chosen if chosen is not None else discover_schemas(conn)
        if not schemas:
            raise NoTables(t(
                "schemas: auto — у базы нет ни одной несистемной схемы с таблицами",
                "schemas: auto — the database has no non-system schema with tables",
            ))
        with read_only(conn), conn.cursor() as cur:
            columns = _select(cur, COLUMNS_SQL, {"schemas": schemas})
            if not columns:
                raise NoTables(t(
                    f"в выбранных схемах нет ни одной таблицы: {', '.join(schemas)} — "
                    "проверьте database.schemas или задайте schemas: auto",
                    f"no table in the selected schemas: {', '.join(schemas)} — "
                    "check database.schemas or set schemas: auto",
                ))
            primary_keys = _select(cur, PRIMARY_KEYS_SQL, {"schemas": schemas})
            foreign_keys = _select(cur, FOREIGN_KEYS_SQL, {"schemas": schemas})
            row_counts: Sequence[Mapping[str, Any]] = ()
            if count_rows and section.get("count_rows", True):
                row_counts = _select(cur, APPROX_ROWS_SQL, {"schemas": schemas})

    return build(
        columns,
        primary_keys,
        foreign_keys,
        row_counts,
        schemas=schemas,
        exclude=section.get("exclude") or (),
        label=section.get("label") or "PostgreSQL",
        key=key,
    )


def write(
    config: Optional[Config] = None, *, count_rows: bool = True, scope: Optional[Scope] = None
) -> Dict[str, Any]:
    """Снять схему и положить артефакт. Возвращает содержание — для вызова из кода.

    *scope* — область, если не та, что в `ATLAS_SYSTEM`/`ATLAS_ENV` (ключи команды).
    """
    cfg = config or load()
    payload = collect(cfg, count_rows=count_rows)
    where = scope or cfg.scope
    return artifacts.write(
        where.path(cfg.artifacts_dir, KIND),
        KIND,
        payload,
        scope=where,
        collector="partest-atlas/db_schema",
    )
